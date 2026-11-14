"""Domain enrichment service for company brand information.

This module provides services for enriching company domain data
with brand information from multiple plugin-based providers.
"""

import itertools
import logging
from datetime import datetime, timezone
from typing import Any, cast

from core.models import Company
from core.services.classification_feedback import carry_over_feedback
from core.services.logo_storage import get_logo_storage_service
from core.utils import is_timestamp_fresh
from django.conf import settings
from django.core.cache import cache
from plugins import PluginRegistry, PluginType
from plugins.enrichment import BaseEnrichmentPlugin
from plugins.enrichment.base_llm import BaseLLMEnrichmentPlugin

logger = logging.getLogger(__name__)

# LLM modules, in the order their data is preferred when blending. Only one
# runs per domain (round-robin), so the order rarely matters in practice.
LLM_SOURCES: list[str] = ["openai", "anthropic", "ollama"]


class DataBlender:
    """Blend enrichment data from multiple sources.

    Combines data from multiple enrichment plugins, using field-specific
    priorities to determine which source provides the best data for each field.
    """

    # Field priority: which source to prefer for each field
    # Order matters - first available source wins
    FIELD_PRIORITIES: dict[str, list[str]] = {
        # Brandfetch's name is canonical; the LLM fills it in when missing.
        "name": ["brandfetch", "clearbit", *LLM_SOURCES],
        "logo_url": ["brandfetch", "clearbit"],  # LLM can't provide logos
        # The LLM rewrites Brandfetch's description into a short, sales-ready
        # summary (it receives Brandfetch's text as input).
        "description": [*LLM_SOURCES, "brandfetch", "clearbit"],
        # LLM classification is grounded in Brandfetch data and maps to a
        # fixed taxonomy, so it is preferred for ICP matching.
        "industry": [*LLM_SOURCES, "brandfetch", "clearbit"],
        "industry_classification": LLM_SOURCES,
        # Firmographic facts: trust the data provider, LLM fills the gaps.
        "year_founded": ["brandfetch", "clearbit", *LLM_SOURCES],
        "employee_count": ["clearbit", "brandfetch", *LLM_SOURCES],
        "headquarters": ["brandfetch", "clearbit", *LLM_SOURCES],
        "colors": ["brandfetch"],
        "links": ["brandfetch", "clearbit"],
    }

    def blend(self, source_data: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Blend data from multiple sources into canonical fields.

        Args:
            source_data: Dict of {provider_name: raw_data}

        Returns:
            Blended data with canonical fields and _sources metadata.
        """
        blended: dict[str, Any] = {}

        # Extract canonical fields using priority order
        for field, priorities in self.FIELD_PRIORITIES.items():
            # Try priority sources first
            for source in priorities:
                if source in source_data:
                    value = self._extract_field(source_data[source], field)
                    if value:
                        blended[field] = value
                        break
            else:
                # Fall back to any source that has this field
                for _source_name, data in source_data.items():
                    value = self._extract_field(data, field)
                    if value:
                        blended[field] = value
                        break

        # Also extract fields not in FIELD_PRIORITIES from any source
        all_fields = {"name", "logo_url", "description", "industry", "year_founded"}
        for field in all_fields - set(self.FIELD_PRIORITIES.keys()):
            for _source_name, data in source_data.items():
                value = self._extract_field(data, field)
                if value:
                    blended[field] = value
                    break

        # Preserve source data for debugging/re-blending
        now = datetime.now(timezone.utc).isoformat()
        blended["_sources"] = {
            name: {"fetched_at": now, "raw": data} for name, data in source_data.items()
        }
        blended["_blended_at"] = now
        blended["_blend_version"] = 1

        return blended

    def _extract_field(self, data: dict[str, Any], field: str) -> Any:
        """Extract a field from provider data.

        Handles nested brand_info structure from providers.

        Args:
            data: Provider data dictionary.
            field: Field name to extract.

        Returns:
            Field value or None if not found.
        """
        # Check top-level first
        if field in data and data[field]:
            return data[field]

        # Check nested brand_info
        brand_info = data.get("brand_info", {})
        if isinstance(brand_info, dict) and field in brand_info:
            return brand_info[field]

        return None


class DomainEnrichmentService:
    """Service for enriching company domain data.

    Uses the plugin registry to discover and manage enrichment providers.
    Collects data from all available plugins and blends results.

    Attributes:
        registry: Plugin registry for managing providers.
        blender: DataBlender for combining multi-source data.
        plugins: List of enabled plugin instances.
    """

    # Company facts and their classification barely change, so enrichment is
    # kept forever (None). Use refresh_enrichment() to force an update; human
    # feedback on the classification survives a refresh.
    CACHE_DURATION_DAYS: int | None = None

    # Every provider response is also cached (Redis in production) so
    # backfills and re-enrichment never pay twice for the same answer.
    # Responses with data are kept forever (None); LLM answers are keyed by a
    # hash of their exact input, so changed Brandfetch data or a new model
    # still gets a fresh answer. Domains with no data are cached briefly so
    # they are retried later rather than on every webhook.
    RESPONSE_CACHE_PREFIX = "enrichment:response:v1:"
    RESPONSE_CACHE_SECONDS: int | None = None
    LLM_RESPONSE_CACHE_SECONDS: int | None = None
    EMPTY_RESPONSE_CACHE_SECONDS = 6 * 3600

    # Cache key for the cross-process round-robin counter over LLM modules.
    LLM_ROUND_ROBIN_KEY = "enrichment:llm_round_robin"
    # Process-local fallback when the cache backend cannot count.
    _local_counter = itertools.count()

    def __init__(self) -> None:
        """Initialize the enrichment service with plugin registry."""
        self.registry = PluginRegistry.instance()
        self.blender = DataBlender()
        self._plugins: list[BaseEnrichmentPlugin] = []
        self._llm_plugins: list[BaseLLMEnrichmentPlugin] = []
        self._initialize()

    def _initialize(self) -> None:
        """Initialize plugins from registry."""
        # Auto-discover plugins if enabled
        if getattr(settings, "PLUGIN_AUTODISCOVER", True):
            self.registry.discover()

        # Get enabled and available enrichment plugins
        # Registry returns BasePlugin but ENRICHMENT type returns BaseEnrichmentPlugin
        enabled = cast(
            list[BaseEnrichmentPlugin],
            self.registry.get_enabled(PluginType.ENRICHMENT),
        )
        # LLM modules are not called like regular providers: they run after
        # them, take their output as context, and share one call per domain.
        self._llm_plugins = [
            p for p in enabled if isinstance(p, BaseLLMEnrichmentPlugin)
        ]
        self._plugins = [
            p for p in enabled if not isinstance(p, BaseLLMEnrichmentPlugin)
        ]

        all_plugins = [*self._plugins, *self._llm_plugins]
        if all_plugins:
            plugin_names = [p.get_plugin_name() for p in all_plugins]
            logger.info(f"Initialized enrichment service with plugins: {plugin_names}")
        else:
            logger.warning("No enrichment plugins available")

    def enrich_domain(self, domain: str, use_cache: bool = True) -> Company | None:
        """Enrich a domain with company information from all available plugins.

        Args:
            domain: Domain name to enrich.
            use_cache: Read cached provider responses. False forces fresh
                provider calls (the new responses are still cached).

        Returns:
            Company instance with enrichment data, or None on failure.
        """
        if not domain:
            logger.warning("Empty domain provided for enrichment")
            return None

        try:
            # Get or create company record
            company, created = Company.objects.get_or_create(
                domain=domain, defaults={"name": "", "logo_url": "", "brand_info": {}}
            )

            if not self._plugins and not self._llm_plugins:
                logger.warning("No plugins available for domain enrichment")
                return company

            # Check if we already have enrichment data
            if not created and self._has_enrichment(company):
                # Companies enriched before an LLM module was configured get
                # their profile once, from the stored Brandfetch data.
                if self._llm_plugins and not (company.brand_info or {}).get(
                    "industry_classification"
                ):
                    self.classify_company(company)
                logger.debug(f"Company {domain} already has enrichment data, skipping")
                return company

            # Collect data from all plugins
            source_data = self._collect_from_plugins(domain, use_cache=use_cache)

            if source_data:
                # Blend data from all sources
                blended = self.blender.blend(source_data)
                self._update_company(company, blended)
                logger.info(
                    f"Enriched {domain} from {len(source_data)} sources: "
                    f"{list(source_data.keys())}"
                )
            else:
                logger.warning(f"No enrichment data found for {domain}")

            return company

        except Exception as e:
            logger.error(f"Error enriching domain {domain}: {e!s}", exc_info=True)
            return None

    def _has_enrichment(self, company: Company) -> bool:
        """Check if company has fresh enrichment data.

        Enrichment is considered valid only when a ``_blended_at`` timestamp
        exists and is newer than ``CACHE_DURATION_DAYS``. Older (or missing)
        data is treated as stale so it gets refreshed on next access.

        Args:
            company: Company model instance.

        Returns:
            True if company has valid, non-stale enrichment data.
        """
        if not company.brand_info:
            return False

        blended_at = company.brand_info.get("_blended_at")
        return bool(is_timestamp_fresh(blended_at, self.CACHE_DURATION_DAYS))

    def _cached_enrich(
        self,
        plugin: BaseEnrichmentPlugin,
        domain: str,
        context: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Call a plugin, serving and storing its response through the cache.

        Errors are not cached; empty responses are cached briefly.

        Args:
            plugin: Plugin to call.
            domain: Domain to enrich.
            context: Grounding data for LLM modules.
            use_cache: Whether a cached response may be returned.

        Returns:
            The plugin's data (possibly from cache), or empty dict.
        """
        is_llm = isinstance(plugin, BaseLLMEnrichmentPlugin)
        key = f"{self.RESPONSE_CACHE_PREFIX}{plugin.cache_key(domain, context)}"

        if use_cache:
            try:
                cached = cache.get(key)
            except Exception as e:
                logger.debug(f"Enrichment cache read failed: {e}")
                cached = None
            if isinstance(cached, dict):
                logger.debug(f"Enrichment cache hit for {key}")
                return cached

        if is_llm:
            data = cast(BaseLLMEnrichmentPlugin, plugin).enrich_domain(
                domain, context=context
            )
        else:
            data = plugin.enrich_domain(domain)
        data = data or {}

        if data:
            ttl = (
                self.LLM_RESPONSE_CACHE_SECONDS
                if is_llm
                else self.RESPONSE_CACHE_SECONDS
            )
        else:
            ttl = self.EMPTY_RESPONSE_CACHE_SECONDS
        try:
            cache.set(key, data, timeout=ttl)
        except Exception as e:
            logger.debug(f"Enrichment cache write failed: {e}")
        return data

    def _collect_from_plugins(
        self, domain: str, use_cache: bool = True
    ) -> dict[str, dict[str, Any]]:
        """Collect enrichment data from all available plugins.

        Regular providers run first. Then one LLM module generates the
        company profile and industry, using their data as input.

        Args:
            domain: Domain to enrich.
            use_cache: Whether cached provider responses may be used.

        Returns:
            Dict mapping plugin names to their enrichment data.
        """
        source_data: dict[str, dict[str, Any]] = {}

        for plugin in self._plugins:
            plugin_name = plugin.get_plugin_name()
            try:
                data = self._cached_enrich(plugin, domain, use_cache=use_cache)
                if data:
                    source_data[plugin_name] = data
                    logger.debug(f"Got data from plugin '{plugin_name}' for {domain}")
            except Exception as e:
                logger.warning(f"Plugin '{plugin_name}' failed for {domain}: {e}")

        llm_data = self._classify_with_llm(domain, source_data, use_cache=use_cache)
        if llm_data:
            source_data.update(llm_data)

        return source_data

    def _llm_rotation(self) -> list[BaseLLMEnrichmentPlugin]:
        """Return LLM modules ordered for this call (round-robin).

        The starting module advances on every call. A shared cache counter
        spreads load across worker processes; if the cache cannot count
        (e.g. the dummy backend) a process-local counter is used.

        Returns:
            All LLM modules, rotated so the next one in turn comes first.
        """
        plugins = self._llm_plugins
        if len(plugins) <= 1:
            return list(plugins)

        turn: int | None = None
        try:
            cache.add(self.LLM_ROUND_ROBIN_KEY, 0, timeout=None)
            turn = cache.incr(self.LLM_ROUND_ROBIN_KEY)
        except Exception:
            turn = None
        if turn is None:
            turn = next(self._local_counter)

        start = turn % len(plugins)
        return plugins[start:] + plugins[:start]

    def _classify_with_llm(
        self,
        domain: str,
        source_data: dict[str, dict[str, Any]],
        use_cache: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Profile and classify the domain with one LLM module.

        Tries modules in round-robin order and stops at the first that
        returns data, so a failing provider falls over to the next one.

        Args:
            domain: Domain to classify. Only the domain is sent, never an
                email address.
            source_data: Data from regular providers, used as context.
            use_cache: Whether a cached answer may be used.

        Returns:
            ``{plugin_name: data}`` for the module that succeeded, or empty.
        """
        if not self._llm_plugins:
            return {}

        context = self._llm_context(source_data)
        for plugin in self._llm_rotation():
            plugin_name = plugin.get_plugin_name()
            try:
                data = self._cached_enrich(
                    plugin, domain, context=context, use_cache=use_cache
                )
            except Exception as e:
                logger.warning(f"LLM module '{plugin_name}' failed for {domain}: {e}")
                continue
            if data:
                logger.debug(f"Classified {domain} with LLM module '{plugin_name}'")
                return {plugin_name: data}
        return {}

    @staticmethod
    def _llm_context(source_data: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """Pick the grounding data for the LLM.

        Brandfetch is preferred; otherwise the first provider with data.
        The LLM module itself only reads the fields in ``CONTEXT_FIELDS``.

        Args:
            source_data: Data from regular providers.

        Returns:
            One provider's data, or None if no provider returned anything.
        """
        if "brandfetch" in source_data:
            return source_data["brandfetch"]
        return next(iter(source_data.values()), None)

    def _update_company(self, company: Company, blended_data: dict[str, Any]) -> None:
        """Update company record with blended enrichment data.

        Args:
            company: Company model instance to update.
            blended_data: Blended data from DataBlender.
        """
        updated_fields: list[str] = []

        # Update basic fields from blended data
        if blended_data.get("name"):
            company.name = blended_data["name"]
            updated_fields.append("name")

        # Store full blended data in brand_info, keeping human feedback
        company.brand_info = carry_over_feedback(company.brand_info, blended_data)
        updated_fields.append("brand_info")

        # Save with specific fields for performance
        company.save(update_fields=updated_fields)

        # Download and store logo if available
        logo_url = blended_data.get("logo_url")
        if logo_url and not company.logo_data:
            logo_service = get_logo_storage_service()
            logo_service.download_and_store(company, logo_url)

        logger.debug(f"Updated company {company.domain} with blended enrichment data")

    @property
    def has_llm_modules(self) -> bool:
        """Whether at least one LLM module is configured."""
        return bool(self._llm_plugins)

    def classify_company(self, company: Company, use_cache: bool = True) -> bool:
        """Add the LLM profile and classification to an enriched company.

        Reuses the provider data stored in ``brand_info["_sources"]`` as
        input, so Brandfetch is not called again, then re-blends. The
        record's freshness timestamps are left alone. Used for backfills.

        Args:
            company: Company to profile.
            use_cache: Whether a cached LLM answer may be used.

        Returns:
            True if a profile was stored.
        """
        if not self._llm_plugins or not company.domain:
            return False

        brand_info = dict(company.brand_info or {})
        sources = brand_info.get("_sources") or {}
        kept_sources = {
            name: entry
            for name, entry in sources.items()
            if name not in LLM_SOURCES and isinstance(entry, dict)
        }
        source_data = {
            name: entry.get("raw") or {} for name, entry in kept_sources.items()
        }
        llm_data = self._classify_with_llm(
            company.domain, source_data, use_cache=use_cache
        )
        if not llm_data:
            return False

        blended = self.blender.blend({**source_data, **llm_data})
        # Keep the original fetch times so cache freshness is unchanged.
        for name, entry in kept_sources.items():
            if entry.get("fetched_at"):
                blended["_sources"][name]["fetched_at"] = entry["fetched_at"]
        if brand_info.get("_blended_at"):
            blended["_blended_at"] = brand_info["_blended_at"]
        carry_over_feedback(brand_info, blended)

        company.brand_info = blended
        update_fields = ["brand_info"]
        if blended.get("name") and blended["name"] != company.name:
            company.name = blended["name"]
            update_fields.append("name")
        company.save(update_fields=update_fields)
        return True

    def get_available_plugins(self) -> list[dict[str, Any]]:
        """Get information about available plugins.

        Returns:
            List of plugin info dictionaries.
        """
        result = self.registry.list_plugins(PluginType.ENRICHMENT)
        return cast("list[dict[str, Any]]", result)

    def refresh_enrichment(self, domain: str) -> Company | None:
        """Force refresh enrichment for a domain.

        Ignores cache and fetches fresh data from all plugins.

        Args:
            domain: Domain to refresh.

        Returns:
            Updated Company instance, or None on failure.
        """
        if not domain:
            return None

        try:
            company = Company.objects.filter(domain=domain).first()
            if not company:
                return self.enrich_domain(domain, use_cache=False)

            # Mark the data stale to force a refresh. The rest of brand_info
            # is kept so human feedback on the classification carries over.
            company.brand_info = {
                **(company.brand_info or {}),
                "_blended_at": None,
            }
            company.save(update_fields=["brand_info"])

            # Re-enrich, bypassing cached provider responses
            return self.enrich_domain(domain, use_cache=False)

        except Exception as e:
            logger.error(f"Error refreshing enrichment for {domain}: {e!s}")
            return None

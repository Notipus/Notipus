"""Base class for enrichment plugins.

Enrichment plugins retrieve company information from external APIs
based on domain names.
"""

import logging
from abc import abstractmethod
from enum import Enum
from typing import Any

from plugins.base import BasePlugin, PluginCapability, PluginMetadata

logger = logging.getLogger(__name__)


class ProviderUnavailableError(Exception):
    """A provider could not answer right now (timeout, rate limit, outage).

    Raised instead of returning an empty result so the enrichment service
    does not cache a transient failure as "no data for this domain".
    """


class EnrichmentCapability(Enum):
    """Specific capabilities for enrichment plugins.

    Maps to PluginCapability values but provides a more focused API
    for enrichment-specific use cases.
    """

    LOGO = "logo"
    DESCRIPTION = "description"
    INDUSTRY = "industry"
    SOCIAL_LINKS = "social_links"
    EMPLOYEE_COUNT = "employee_count"
    FUNDING = "funding"
    COLORS = "colors"
    YEAR_FOUNDED = "year_founded"

    def to_plugin_capability(self) -> PluginCapability:
        """Convert to the base PluginCapability enum."""
        return PluginCapability(self.value)


class BaseEnrichmentPlugin(BasePlugin):
    """Base class for enrichment plugins.

    Enrichment plugins retrieve company/brand information from external APIs
    based on domain names. They can provide various types of data including
    logos, descriptions, industry classification, and more.

    Subclasses must implement:
    - get_metadata(): Return plugin metadata with plugin_type=ENRICHMENT
    - enrich_domain(): Perform the actual domain enrichment

    Subclasses may override:
    - is_available(): Check if plugin can be used (e.g., API key exists)
    - configure(): Configure plugin with settings from Django config

    Example:
        class MyEnrichmentPlugin(BaseEnrichmentPlugin):
            @classmethod
            def get_metadata(cls) -> PluginMetadata:
                return PluginMetadata(
                    name="my_enricher",
                    display_name="My Enricher",
                    version="1.0.0",
                    description="Enriches domains with company data",
                    plugin_type=PluginType.ENRICHMENT,
                    capabilities={PluginCapability.LOGO, PluginCapability.DESCRIPTION},
                    priority=50,
                    config_keys=["api_key"],
                )

            def enrich_domain(self, domain: str) -> dict[str, Any]:
                # Call API and return enrichment data
                return {"name": "Company", "logo_url": "https://..."}
    """

    @classmethod
    @abstractmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Must set plugin_type=PluginType.ENRICHMENT.

        Returns:
            PluginMetadata describing this enrichment plugin.
        """
        pass

    @abstractmethod
    def enrich_domain(self, domain: str) -> dict[str, Any]:
        """Enrich domain and return data.

        Args:
            domain: The domain to enrich (e.g., "example.com").

        Returns:
            Dictionary containing enrichment data. Should include:
            - name: Company name
            - logo_url: URL to company logo (optional)
            - brand_info: Dict with additional data (description, industry, etc.)

        Example return value:
            {
                "name": "Example Corp",
                "logo_url": "https://example.com/logo.png",
                "brand_info": {
                    "description": "A technology company",
                    "industry": "Technology",
                    "year_founded": 2010,
                    "links": [...],
                    "colors": [...],
                }
            }
        """
        pass

    def cache_key(self, domain: str, context: dict[str, Any] | None = None) -> str:
        """Identify this plugin's response for caching.

        Override when the response depends on more than the domain (LLM
        modules include the model and a hash of the prompt).

        Args:
            domain: Domain being enriched.
            context: Optional input from other providers.

        Returns:
            Cache key fragment, unique per plugin and input.
        """
        return f"{self.get_plugin_name()}:{domain.lower()}"

    def get_capabilities(self) -> set[EnrichmentCapability]:
        """Get the enrichment capabilities of this plugin.

        Returns:
            Set of EnrichmentCapability values this plugin supports.
        """
        capabilities = set()
        for cap in self.get_metadata().capabilities:
            try:
                capabilities.add(EnrichmentCapability(cap.value))
            except ValueError:
                # Not an enrichment capability, skip
                pass
        return capabilities

"""Brandfetch API plugin for domain enrichment.

This module provides integration with the Brandfetch API v2 to retrieve
brand information including logos, colors, and company details.
"""

import logging
from typing import Any, cast

import requests
from django.conf import settings
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.enrichment.base import BaseEnrichmentPlugin

logger = logging.getLogger(__name__)

# Brandfetch reports headcount as the lower bound of a band.
_EMPLOYEE_BANDS: dict[int, str] = {
    1: "1",
    2: "2-10",
    11: "11-50",
    51: "51-200",
    201: "201-500",
    501: "501-1000",
    1001: "1001-5000",
    5001: "5001-10000",
    10001: "10001+",
}


class BrandfetchPlugin(BaseEnrichmentPlugin):
    """Plugin for enriching domains with brand data from Brandfetch API.

    This plugin provides:
    - Company logos
    - Company descriptions
    - Industry classification
    - Social links
    - Brand colors
    - Year founded
    """

    # Default configuration
    DEFAULT_BASE_URL = "https://api.brandfetch.io/v2"
    DEFAULT_TIMEOUT = 10

    def __init__(self) -> None:
        """Initialize the Brandfetch plugin.

        Configuration is set via the configure() method.
        """
        self.api_key: str | None = None
        self.base_url: str = self.DEFAULT_BASE_URL
        self.timeout: int = self.DEFAULT_TIMEOUT

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Brandfetch plugin.
        """
        return PluginMetadata(
            name="brandfetch",
            display_name="Brandfetch",
            version="1.0.0",
            description="Brand logos, colors, and company info via Brandfetch API",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={
                PluginCapability.LOGO,
                PluginCapability.DESCRIPTION,
                PluginCapability.INDUSTRY,
                PluginCapability.SOCIAL_LINKS,
                PluginCapability.COLORS,
                PluginCapability.YEAR_FOUNDED,
            },
            priority=100,
            config_keys=["api_key"],
        )

    @classmethod
    def is_available(cls) -> bool:
        """Check if API key is configured.

        Returns:
            True if an API key is available in PLUGINS config.
        """
        plugins_config = getattr(settings, "PLUGINS", {})
        enrichment_config = plugins_config.get("enrichment", {})
        plugin_config = enrichment_config.get("brandfetch", {}).get("config", {})
        return bool(plugin_config.get("api_key"))

    def configure(self, config: dict[str, Any]) -> None:
        """Configure plugin with settings.

        Args:
            config: Configuration dictionary with:
                - api_key: Brandfetch API key (required)
                - base_url: API base URL (optional)
                - timeout: Request timeout in seconds (optional)
        """
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url", self.DEFAULT_BASE_URL)
        self.timeout = config.get("timeout", self.DEFAULT_TIMEOUT)

        if self.api_key:
            logger.debug("Brandfetch plugin configured with API key")
        else:
            logger.warning("Brandfetch plugin configured without API key")

    def enrich_domain(self, domain: str) -> dict[str, Any]:
        """Enrich domain with brand data from Brandfetch API v2.

        The /v2/brands/{domain} endpoint returns all brand data including logos
        in a single response. There is no separate /logos endpoint.

        API Documentation: https://docs.brandfetch.com/reference/brand-api

        Args:
            domain: The domain to enrich (e.g., "example.com").

        Returns:
            Dictionary containing brand name, logo URL, and brand info,
            or empty dict on failure.
        """
        if not self.api_key:
            logger.error("Brandfetch API key is not configured")
            return {}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.get(
                f"{self.base_url}/brands/{domain}",
                headers=headers,
                timeout=self.timeout,
            )

            # Handle 404 specifically - domain not found is expected, not an error
            if response.status_code == 404:
                logger.debug(f"Brandfetch: No brand data found for domain {domain}")
                return {}

            # Handle rate limiting before raise_for_status
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "60")
                logger.warning(
                    f"Brandfetch rate limit exceeded. Retry after {retry_after} seconds"
                )
                return {}

            response.raise_for_status()
            brand_data = response.json()

            # Check quota usage from response headers
            self._log_quota_usage(response.headers)

            # Logos are included in the main brands response under the "logos" array
            # No separate /logos endpoint exists in the Brandfetch API v2
            logos_data = brand_data.get("logos", [])
            company = brand_data.get("company") or {}
            if not isinstance(company, dict):
                company = {}
            industries = self._industry_names(company.get("industries"))
            employees = company.get("employees")

            return {
                "name": brand_data.get("name"),
                "logo_url": self._get_primary_logo(logos_data),
                "brand_info": {
                    "description": brand_data.get("description"),
                    "long_description": brand_data.get("longDescription"),
                    "industry": brand_data.get("industry")
                    or (industries[0] if industries else None),
                    "industries": industries,
                    "year_founded": company.get("foundedYear")
                    or brand_data.get("yearFounded"),
                    "employee_count": _EMPLOYEE_BANDS.get(employees)
                    if isinstance(employees, int)
                    else None,
                    "headquarters": self._format_location(company.get("location")),
                    "company_kind": company.get("kind"),
                    "links": brand_data.get("links", []),
                    "colors": brand_data.get("colors", []),
                },
            }
        except requests.exceptions.Timeout:
            logger.warning(f"Brandfetch API timeout for domain {domain}")
            return {}
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data from Brandfetch: {e!s}")
            return {}

    @staticmethod
    def _industry_names(industries: Any) -> list[str]:
        """Return industry names, best match first, as "Parent › Child".

        Args:
            industries: ``company.industries`` from the API (sorted by score).

        Returns:
            Industry labels; empty when none are present.
        """
        names: list[str] = []
        if not isinstance(industries, list):
            return names
        for item in industries:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            parent = item.get("parent")
            parent_name = parent.get("name") if isinstance(parent, dict) else None
            label = (
                f"{parent_name} › {item['name']}"
                if parent_name and parent_name != item["name"]
                else str(item["name"])
            )
            if label not in names:
                names.append(label)
        return names

    @staticmethod
    def _format_location(location: Any) -> str | None:
        """Format ``company.location`` as "City, Country".

        Args:
            location: Location object from the API.

        Returns:
            Human-readable location, or None if unknown.
        """
        if not isinstance(location, dict):
            return None
        parts = [
            location.get("city"),
            location.get("state") or location.get("region"),
            location.get("country"),
        ]
        seen: list[str] = []
        for part in parts:
            if isinstance(part, str) and part and part not in seen:
                seen.append(part)
        return ", ".join(seen) or None

    def _get_primary_logo(self, logos_data: list[dict[str, Any]]) -> str | None:
        """Retrieve the URL of the main logo.

        Args:
            logos_data: List of logo dictionaries from the API response.

        Returns:
            URL of the primary logo, or None if not found.
        """
        if not logos_data:
            return None

        for logo in logos_data:
            if logo.get("type") == "icon" and logo.get("formats"):
                for fmt in logo["formats"]:
                    if fmt.get("src"):
                        return cast(str, fmt["src"])
        return None

    def _log_quota_usage(self, headers: Any) -> None:
        """Log API quota usage from response headers.

        Args:
            headers: Response headers from the Brandfetch API.
        """
        # Header names: x-api-key-quota, x-api-key-approximate-usage
        # Values are numeric only — convert to int early to avoid logging raw
        # header strings (which CodeQL flags due to "api-key" in header name).
        _HEADER_QUOTA = "x-api-key-quota"  # noqa: N806
        _HEADER_USAGE = "x-api-key-approximate-usage"  # noqa: N806
        try:
            raw_quota = headers.get(_HEADER_QUOTA)
            raw_usage = headers.get(_HEADER_USAGE)

            if (
                raw_quota
                and raw_usage
                and str(raw_quota).isdigit()
                and str(raw_usage).isdigit()
            ):
                quota_int = int(raw_quota)
                usage_int = int(raw_usage)
                usage_pct = (usage_int / quota_int) * 100
                logger.info(
                    "Brandfetch API usage: %d/%d (%.1f%%)",
                    usage_int,
                    quota_int,
                    usage_pct,
                )

                if usage_pct > 80:
                    logger.warning("Brandfetch API usage high: %.1f%%", usage_pct)
            elif raw_quota and str(raw_quota).isdigit():
                logger.debug("Brandfetch API quota: %d", int(raw_quota))
            elif raw_usage and str(raw_usage).isdigit():
                logger.debug("Brandfetch API usage: %d", int(raw_usage))
        except (ValueError, TypeError, ZeroDivisionError) as e:
            logger.debug("Could not parse quota headers: %s", e)

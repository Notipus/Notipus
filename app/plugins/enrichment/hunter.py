"""Hunter.io API plugin for email enrichment.

This module provides integration with the Hunter.io People Find API
to retrieve person information based on email addresses.

API Documentation: https://hunter.io/api-documentation/v2#email-enrichment

Privacy Note: Customer emails are sent to Hunter.io for enrichment.
Hunter.io is GDPR compliant and returns 451 status for people who have
requested data removal. This feature requires Pro or Enterprise plan
and the workspace must provide their own Hunter.io API key.
"""

import logging
from typing import Any

import requests
from core.utils.email_domain import is_enrichable_domain
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.enrichment.base_email import (
    BaseEmailEnrichmentPlugin,
    EmailNotFoundError,
    GDPRClaimedError,
    RateLimitError,
)

logger = logging.getLogger(__name__)


class HunterPlugin(BaseEmailEnrichmentPlugin):
    """Plugin for enriching emails with person data from Hunter.io.

    This plugin provides:
    - Person name (first, last)
    - Job title and seniority level
    - Company domain from employment
    - Social profiles (LinkedIn, Twitter, GitHub)
    - Location information

    Rate limits: 15 requests/second, 500 requests/minute

    Privacy: Customer emails are sent to Hunter.io. Users must consent
    to this in the workspace settings and provide their own API key.
    """

    BASE_URL = "https://api.hunter.io/v2"
    DEFAULT_TIMEOUT = 10

    def __init__(self) -> None:
        """Initialize the Hunter plugin."""
        self.timeout: int = self.DEFAULT_TIMEOUT

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Hunter plugin.
        """
        return PluginMetadata(
            name="hunter",
            display_name="Hunter.io",
            version="1.0.0",
            description="Person data enrichment via Hunter.io email lookup",
            plugin_type=PluginType.EMAIL_ENRICHMENT,
            capabilities={
                PluginCapability.PERSON_NAME,
                PluginCapability.JOB_TITLE,
                PluginCapability.SENIORITY,
                PluginCapability.PERSON_LINKEDIN,
                PluginCapability.PERSON_TWITTER,
                PluginCapability.PERSON_GITHUB,
                PluginCapability.PERSON_LOCATION,
            },
            priority=100,
        )

    @classmethod
    def is_available(cls) -> bool:
        """Check if plugin is available.

        For Hunter.io, availability depends on per-workspace API keys,
        not global configuration. Always return True here; the actual
        check happens in EmailEnrichmentService.

        Returns:
            True (always available as a plugin).
        """
        return True

    def verify_api_key(self, api_key: str) -> tuple[bool, str]:
        """Verify a Hunter.io API key is valid.

        Calls the /account endpoint to check if the API key works.

        Args:
            api_key: The Hunter.io API key to verify.

        Returns:
            Tuple of (is_valid, message). If valid, message contains
            account info. If invalid, message contains error details.
        """
        if not api_key:
            return False, "API key is required"

        try:
            response = requests.get(
                f"{self.BASE_URL}/account",
                params={"api_key": api_key},
                timeout=self.timeout,
            )

            if response.status_code == 401:
                return False, "Invalid API key"

            if response.status_code == 429:
                return False, "Rate limit exceeded. Please try again later."

            response.raise_for_status()
            data = response.json().get("data", {})

            # Return success with account info
            email = data.get("email", "")
            plan = data.get("plan_name", "Unknown")
            requests_remaining = (
                data.get("requests", {}).get("searches", {}).get("available", "N/A")
            )

            return True, (
                f"Connected as {email} "
                f"({plan} plan, {requests_remaining} searches remaining)"
            )

        except requests.exceptions.Timeout:
            return False, "Connection timed out. Please try again."
        except requests.exceptions.RequestException as e:
            logger.error(f"Error verifying Hunter.io API key: {e!s}")
            return False, f"Connection error: {e!s}"

    def enrich_email(self, email: str, api_key: str) -> dict[str, Any]:
        """Enrich email with person data from Hunter.io People Find API.

        API Endpoint: GET https://api.hunter.io/v2/people/find?email=...

        Note: Hunter.io only supports business email domains. Free email
        providers (Gmail, Yahoo, etc.) and disposable domains are filtered
        out before making the API call.

        Args:
            email: The email address to enrich.
            api_key: The workspace's Hunter.io API key.

        Returns:
            Dictionary containing person data with normalized field names.

        Raises:
            EmailNotFoundError: If no data found for the email (404).
            GDPRClaimedError: If person requested data removal (451).
            RateLimitError: If rate limit exceeded (429).
        """
        if not api_key:
            logger.error("Hunter.io API key not provided")
            return {}

        # Skip non-business emails (Gmail, Yahoo, disposable, etc.)
        # Hunter.io only supports business email domains
        if not is_enrichable_domain(email):
            logger.debug(f"Skipping Hunter.io lookup for non-business email: {email}")
            raise EmailNotFoundError(email)

        try:
            response = requests.get(
                f"{self.BASE_URL}/people/find",
                params={"email": email, "api_key": api_key},
                timeout=self.timeout,
            )

            # Handle specific error codes
            if response.status_code == 400:
                # 400 = Bad Request, typically for unsupported email domains
                # (free providers like Gmail, Yahoo, etc.)
                logger.debug(f"Hunter.io: Unsupported email domain for {email}")
                raise EmailNotFoundError(email)

            if response.status_code == 404:
                raise EmailNotFoundError(email)

            if response.status_code == 451:
                raise GDPRClaimedError(email)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                raise RateLimitError(
                    "Hunter.io rate limit exceeded",
                    retry_after=int(retry_after) if retry_after else None,
                )

            response.raise_for_status()
            data = response.json().get("data", {})

            # Log rate limit info if available
            self._log_rate_limit_info(response.headers)

            return self._normalize_response(data, email)

        except (EmailNotFoundError, GDPRClaimedError, RateLimitError):
            # Re-raise domain-specific exceptions for caller to handle
            raise
        except requests.exceptions.Timeout:
            logger.warning(f"Hunter.io API timeout for {email}")
            return {}
        except requests.exceptions.RequestException as e:
            logger.error(f"Error calling Hunter.io API: {e!s}")
            return {}

    def _normalize_response(self, data: dict[str, Any], email: str) -> dict[str, Any]:
        """Normalize Hunter.io API response to standard format.

        Args:
            data: Raw API response data.
            email: The email address that was enriched.

        Returns:
            Normalized dictionary with standard field names.
        """
        # Extract name parts
        name = data.get("name", {}) or {}

        # Extract employment info
        employment = data.get("employment", {}) or {}

        # Build normalized response
        result: dict[str, Any] = {
            "email": email,
            "first_name": name.get("givenName") or data.get("first_name") or "",
            "last_name": name.get("familyName") or data.get("last_name") or "",
            "position": employment.get("title") or data.get("position") or "",
            "seniority": employment.get("seniority") or data.get("seniority") or "",
            "company_domain": employment.get("domain") or "",
            "linkedin_url": self._build_linkedin_url(data.get("linkedin")),
            "twitter_handle": data.get("twitter") or "",
            "github_handle": data.get("github") or "",
            "location": self._build_location_string(data),
            "_raw": data,
        }

        return result

    def _build_linkedin_url(self, linkedin_handle: str | None) -> str:
        """Build full LinkedIn URL from handle.

        Args:
            linkedin_handle: LinkedIn profile handle or URL.

        Returns:
            Full LinkedIn profile URL or empty string.
        """
        if not linkedin_handle:
            return ""

        # Handle case where linkedin is a dict (e.g., {"url": "...", "handle": "..."})
        if isinstance(linkedin_handle, dict):
            url = linkedin_handle.get("url") or linkedin_handle.get("handle")
            if not url:
                return ""
            linkedin_handle = url

        # If it's already a full URL, return as-is
        if linkedin_handle.startswith("http"):
            return linkedin_handle

        # Build URL from handle
        return f"https://linkedin.com/in/{linkedin_handle}"

    def _build_location_string(self, data: dict[str, Any]) -> str:
        """Build location string from API response.

        Args:
            data: API response data containing location/geo info.

        Returns:
            Formatted location string or empty string.
        """
        # Try direct location field first
        if data.get("location"):
            return str(data["location"])

        # Try geo object
        geo = data.get("geo", {}) or {}
        parts = []

        if geo.get("city"):
            parts.append(geo["city"])
        if geo.get("state"):
            parts.append(geo["state"])
        if geo.get("country") and not parts:
            # Only add country if we don't have city/state
            parts.append(geo["country"])

        return ", ".join(parts)

    def _log_rate_limit_info(self, headers: Any) -> None:
        """Log rate limit information from response headers.

        Args:
            headers: Response headers from Hunter.io API.
        """
        # Hunter.io uses X-RateLimit-* headers
        limit = headers.get("X-RateLimit-Limit")
        remaining = headers.get("X-RateLimit-Remaining")

        if limit and remaining:
            try:
                limit_int = int(limit)
                remaining_int = int(remaining)
                usage_pct = ((limit_int - remaining_int) / limit_int) * 100

                if usage_pct > 80:
                    logger.warning(
                        f"Hunter.io rate limit high: {remaining}/{limit} remaining "
                        f"({usage_pct:.1f}% used)"
                    )
                else:
                    logger.debug(f"Hunter.io rate limit: {remaining}/{limit} remaining")
            except (ValueError, ZeroDivisionError):
                pass

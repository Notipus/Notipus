"""Base class for email enrichment plugins.

Email enrichment plugins retrieve person information from external APIs
based on email addresses. Unlike domain enrichment (which returns company data),
email enrichment returns person-specific data like name, job title, and social profiles.

Key differences from domain enrichment:
- Input: email address (not domain)
- Output: person data (not company data)
- Caching: Person model (not Company model)
- Works for ALL emails including free providers (Gmail, etc.)
- Requires per-workspace API keys (not global configuration)
"""

import logging
from abc import abstractmethod
from enum import Enum
from typing import Any

from plugins.base import BasePlugin, PluginCapability, PluginMetadata

logger = logging.getLogger(__name__)


class EmailEnrichmentCapability(Enum):
    """Specific capabilities for email enrichment plugins.

    Maps to PluginCapability values but provides a more focused API
    for email enrichment-specific use cases.
    """

    PERSON_NAME = "person_name"
    JOB_TITLE = "job_title"
    SENIORITY = "seniority"
    LINKEDIN = "person_linkedin"
    TWITTER = "person_twitter"
    GITHUB = "person_github"
    LOCATION = "person_location"

    def to_plugin_capability(self) -> PluginCapability:
        """Convert to the base PluginCapability enum."""
        return PluginCapability(self.value)


class BaseEmailEnrichmentPlugin(BasePlugin):
    """Base class for email enrichment plugins.

    Email enrichment plugins retrieve person information from external APIs
    based on email addresses. They can provide various types of data including
    names, job titles, seniority levels, and social profile links.

    Unlike domain enrichment plugins:
    - They take an email address as input (not a domain)
    - They require a per-workspace API key (not global configuration)
    - They return person data (not company data)
    - They work for ALL emails including free providers

    Subclasses must implement:
    - get_metadata(): Return plugin metadata with plugin_type=EMAIL_ENRICHMENT
    - enrich_email(): Perform the actual email enrichment

    Subclasses may override:
    - is_available(): Check if plugin can be used

    Example:
        class MyEmailEnrichmentPlugin(BaseEmailEnrichmentPlugin):
            @classmethod
            def get_metadata(cls) -> PluginMetadata:
                return PluginMetadata(
                    name="my_enricher",
                    display_name="My Email Enricher",
                    version="1.0.0",
                    description="Enriches emails with person data",
                    plugin_type=PluginType.EMAIL_ENRICHMENT,
                    capabilities={
                        PluginCapability.PERSON_NAME,
                        PluginCapability.JOB_TITLE,
                    },
                    priority=50,
                )

            def enrich_email(self, email: str, api_key: str) -> dict[str, Any]:
                # Call API and return enrichment data
                return {
                    "first_name": "John",
                    "last_name": "Doe",
                    "position": "VP of Engineering",
                }
    """

    @classmethod
    @abstractmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Must set plugin_type=PluginType.EMAIL_ENRICHMENT.

        Returns:
            PluginMetadata describing this email enrichment plugin.
        """
        pass

    @abstractmethod
    def enrich_email(self, email: str, api_key: str) -> dict[str, Any]:
        """Enrich email and return person data.

        Args:
            email: The email address to enrich.
            api_key: The workspace's API key for this enrichment service.

        Returns:
            Dictionary containing person data. Should include:
            - first_name: Person's first/given name
            - last_name: Person's last/family name
            - position: Job title
            - seniority: Seniority level (e.g., "senior", "executive")
            - company_domain: Company domain from employment data
            - linkedin_url: LinkedIn profile URL
            - twitter_handle: Twitter/X handle
            - github_handle: GitHub username
            - location: Location string
            - _raw: Full API response for reference

        Example return value:
            {
                "first_name": "John",
                "last_name": "Doe",
                "position": "VP of Engineering",
                "seniority": "executive",
                "company_domain": "example.com",
                "linkedin_url": "https://linkedin.com/in/johndoe",
                "twitter_handle": "johndoe",
                "github_handle": "johndoe",
                "location": "San Francisco, CA",
                "_raw": {...},
            }

        Raises:
            EmailNotFoundError: If no data found for the email.
            GDPRClaimedError: If the person has requested data removal (GDPR).
            RateLimitError: If API rate limit exceeded.
        """
        pass

    def get_capabilities(self) -> set[EmailEnrichmentCapability]:
        """Get the email enrichment capabilities of this plugin.

        Returns:
            Set of EmailEnrichmentCapability values this plugin supports.
        """
        capabilities = set()
        for cap in self.get_metadata().capabilities:
            try:
                capabilities.add(EmailEnrichmentCapability(cap.value))
            except ValueError:
                # Not an email enrichment capability, skip
                pass
        return capabilities


class EmailEnrichmentError(Exception):
    """Base exception for email enrichment errors."""

    pass


class EmailNotFoundError(EmailEnrichmentError):
    """Raised when no data is found for an email address."""

    def __init__(self, email: str, message: str | None = None) -> None:
        self.email = email
        super().__init__(message or f"No data found for email: {email}")


class GDPRClaimedError(EmailEnrichmentError):
    """Raised when a person has requested data removal under GDPR.

    Hunter.io returns 451 status for these cases.
    We should not retry or cache these.
    """

    def __init__(self, email: str, message: str | None = None) -> None:
        self.email = email
        super().__init__(
            message or f"Person has requested data removal (GDPR): {email}"
        )


class RateLimitError(EmailEnrichmentError):
    """Raised when the API rate limit is exceeded."""

    def __init__(
        self,
        message: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message or "Rate limit exceeded")

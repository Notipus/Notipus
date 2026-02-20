"""Email enrichment service for person/contact data.

This module provides services for enriching email addresses with person
information from Hunter.io. Unlike domain enrichment (which returns company data),
email enrichment returns person-specific data.

Features:
- Works for business email domains only (Gmail, Yahoo, etc. are filtered out)
- Requires Pro or Enterprise plan
- Uses per-workspace API keys (not global configuration)
- Caches results in the Person model

Privacy Note: Customer emails are sent to Hunter.io for enrichment.
This requires user consent (configured in workspace settings) and
the workspace must provide their own Hunter.io API key.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from core.models import Integration, Person
from core.permissions import has_plan_or_higher
from plugins.enrichment.base_email import (
    EmailNotFoundError,
    GDPRClaimedError,
    RateLimitError,
)
from plugins.enrichment.hunter import HunterPlugin

if TYPE_CHECKING:
    from core.models import Workspace

logger = logging.getLogger(__name__)


class EmailEnrichmentService:
    """Service for enriching email addresses with person data.

    Uses Hunter.io to retrieve person information based on email addresses.
    Results are cached in the Person model to avoid redundant API calls.

    Requirements:
    - Workspace must be on Pro or Enterprise plan
    - Workspace must have Hunter.io integration configured with API key

    Attributes:
        ALLOWED_PLANS: Tuple of plans that can use email enrichment.
        CACHE_DURATION_DAYS: Days before cached data is considered stale.
            None means indefinite caching.
    """

    ALLOWED_PLANS = ("pro", "enterprise")
    CACHE_DURATION_DAYS: int | None = None  # Indefinite cache, like domain enrichment

    def __init__(self) -> None:
        """Initialize the email enrichment service."""
        self._hunter_plugin = HunterPlugin()

    def enrich_email(self, email: str, workspace: "Workspace") -> Person | None:
        """Enrich an email address with person data from Hunter.io.

        Args:
            email: The email address to enrich.
            workspace: The workspace making the request (for API key and tier check).

        Returns:
            Person instance with enrichment data, or None if:
            - Workspace is not on Pro/Enterprise plan
            - Workspace has no Hunter.io integration
            - Hunter.io returned no data for the email
            - An error occurred during enrichment
        """
        if not email:
            logger.warning("Empty email provided for enrichment")
            return None

        # Normalize email
        email = email.lower().strip()

        # Check billing tier
        if not self._check_tier(workspace):
            logger.debug(
                f"Workspace {workspace.name} not on Pro/Enterprise, skipping enrichment"
            )
            return None

        # Get Hunter.io API key for this workspace
        api_key = self._get_hunter_api_key(workspace)
        if not api_key:
            logger.debug(
                f"Workspace {workspace.name} has no Hunter.io API key configured"
            )
            return None

        try:
            # Check for cached data
            person = Person.objects.filter(email=email).first()
            if person and self._is_fresh(person):
                logger.debug(f"Using cached person data for {email}")
                return person

            # Call Hunter.io API
            data = self._call_hunter_api(email, api_key)

            if data:
                # Store/update Person record
                person = self._update_person(email, data)
                logger.info(f"Enriched email {email} from Hunter.io")
                return person
            else:
                logger.debug(f"No enrichment data found for {email}")
                return None

        except EmailNotFoundError:
            logger.debug(f"Hunter.io: No data found for {email}")
            return None
        except GDPRClaimedError:
            logger.info(f"Hunter.io: GDPR claimed for {email}, not caching")
            return None
        except RateLimitError as e:
            logger.warning(f"Hunter.io rate limit exceeded: {e}")
            return None
        except Exception as e:
            logger.error(f"Error enriching email {email}: {e!s}", exc_info=True)
            return None

    def _check_tier(self, workspace: "Workspace") -> bool:
        """Check if workspace has the required subscription tier.

        Args:
            workspace: The workspace to check.

        Returns:
            True if workspace is on Pro or Enterprise plan.
        """
        return cast(bool, has_plan_or_higher(workspace, "pro"))

    def _get_hunter_api_key(self, workspace: "Workspace") -> str | None:
        """Get the Hunter.io API key for a workspace.

        Args:
            workspace: The workspace to get the API key for.

        Returns:
            The Hunter.io API key, or None if not configured.
        """
        try:
            integration = Integration.objects.get(
                workspace=workspace,
                integration_type="hunter_enrichment",
                is_active=True,
            )
            return cast("str | None", integration.integration_settings.get("api_key"))
        except Integration.DoesNotExist:
            return None

    def _is_fresh(self, person: Person) -> bool:
        """Check if cached person data is still fresh.

        Currently uses indefinite caching (like domain enrichment).
        Checks for the _enriched_at timestamp in hunter_data.

        Args:
            person: The Person model instance.

        Returns:
            True if the cached data is still valid.
        """
        if not person.hunter_data:
            return False

        # Check if we have enrichment timestamp
        enriched_at = person.hunter_data.get("_enriched_at")
        if not enriched_at:
            return False

        # Currently using indefinite cache
        # If CACHE_DURATION_DAYS is set, implement time-based expiry here
        return True

    def _call_hunter_api(self, email: str, api_key: str) -> dict[str, Any]:
        """Call the Hunter.io API to enrich an email.

        Args:
            email: The email address to enrich.
            api_key: The Hunter.io API key.

        Returns:
            Dictionary containing person data from Hunter.io.

        Raises:
            EmailNotFoundError: If no data found for the email.
            GDPRClaimedError: If person requested data removal.
            RateLimitError: If rate limit exceeded.
        """
        return cast("dict[str, Any]", self._hunter_plugin.enrich_email(email, api_key))

    def _update_person(self, email: str, data: dict[str, Any]) -> Person:
        """Update or create a Person record with enrichment data.

        Args:
            email: The email address.
            data: Normalized data from Hunter.io.

        Returns:
            The updated or created Person instance.
        """
        # Add enrichment timestamp to hunter_data
        raw_data = data.get("_raw", {})
        raw_data["_enriched_at"] = datetime.now(timezone.utc).isoformat()

        person, created = Person.objects.update_or_create(
            email=email,
            defaults={
                "first_name": data.get("first_name", ""),
                "last_name": data.get("last_name", ""),
                "position": data.get("position", ""),
                "seniority": data.get("seniority", ""),
                "company_domain": data.get("company_domain", ""),
                "linkedin_url": data.get("linkedin_url", ""),
                "twitter_handle": data.get("twitter_handle", ""),
                "github_handle": data.get("github_handle", ""),
                "location": data.get("location", ""),
                "hunter_data": raw_data,
            },
        )

        action = "Created" if created else "Updated"
        logger.debug(f"{action} person record for {email}")
        return person

    def refresh_enrichment(self, email: str, workspace: "Workspace") -> Person | None:
        """Force refresh enrichment for an email.

        Ignores cache and fetches fresh data from Hunter.io.

        Args:
            email: The email address to refresh.
            workspace: The workspace making the request.

        Returns:
            Updated Person instance, or None on failure.
        """
        if not email:
            return None

        email = email.lower().strip()

        try:
            # Clear existing enrichment data
            person = Person.objects.filter(email=email).first()
            if person:
                person.hunter_data = {}
                person.save(update_fields=["hunter_data", "updated_at"])

            # Re-enrich
            return self.enrich_email(email, workspace)

        except Exception as e:
            logger.error(f"Error refreshing enrichment for {email}: {e!s}")
            return None

    def get_cached_person(self, email: str) -> Person | None:
        """Get cached person data without calling the API.

        Args:
            email: The email address to look up.

        Returns:
            Person instance if cached, None otherwise.
        """
        if not email:
            return None

        email = email.lower().strip()
        return Person.objects.filter(email=email).first()

    def is_enrichment_available(self, workspace: "Workspace") -> bool:
        """Check if email enrichment is available for a workspace.

        Args:
            workspace: The workspace to check.

        Returns:
            True if workspace can use email enrichment.
        """
        return self._check_tier(workspace) and bool(self._get_hunter_api_key(workspace))


# Singleton instance for convenience
_service_instance: EmailEnrichmentService | None = None


def get_email_enrichment_service() -> EmailEnrichmentService:
    """Get the email enrichment service singleton.

    Returns:
        The EmailEnrichmentService instance.
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = EmailEnrichmentService()
    return _service_instance

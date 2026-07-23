"""Tests for fitting Hunter.io enrichment values into Person columns.

Hunter.io occasionally returns values longer than the Person model's
columns (e.g. a 100+ char name), which used to abort the whole insert
with a DataError. Display-text fields are now truncated to the column
width and link/identifier fields are dropped instead.
"""

from typing import Any
from unittest.mock import patch

import pytest
from core.models import Person, Workspace
from core.services.email_enrichment import EmailEnrichmentService


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create a Pro-plan test workspace."""
    return Workspace.objects.create(
        name="Workspace",
        subscription_plan="pro",
        subscription_status="active",
    )


@pytest.fixture
def enrichment_service() -> EmailEnrichmentService:
    """Create an EmailEnrichmentService for testing."""
    return EmailEnrichmentService()


def _enrich(
    service: EmailEnrichmentService,
    workspace: Workspace,
    api_data: dict[str, Any],
    email: str = "john@example.com",
) -> Person | None:
    """Run enrich_email with tier/API-key checks and the API call mocked.

    Args:
        service: The enrichment service under test.
        workspace: The workspace making the request.
        api_data: The normalized payload the mocked Hunter.io call returns.
        email: The email address to enrich.

    Returns:
        The Person returned by the service, or None.
    """
    with (
        patch.object(service, "_check_tier", return_value=True),
        patch.object(service, "_get_hunter_api_key", return_value="test-key"),
        patch.object(service, "_call_hunter_api", return_value=api_data),
    ):
        return service.enrich_email(email, workspace)


@pytest.mark.django_db
class TestEnrichmentFieldLimits:
    """Tests for overlong Hunter.io values being fitted to the columns."""

    def test_overlong_name_is_truncated_not_fatal(
        self, enrichment_service: EmailEnrichmentService, workspace: Workspace
    ) -> None:
        """A 100+ char name is clipped to the column width instead of erroring."""
        api_data = {
            "first_name": "J" * 250,
            "last_name": "Doe",
            "_raw": {},
        }

        person = _enrich(enrichment_service, workspace, api_data)

        assert person is not None
        assert person.first_name == "J" * 100
        assert person.last_name == "Doe"

    def test_overlong_identifier_fields_are_dropped(
        self, enrichment_service: EmailEnrichmentService, workspace: Workspace
    ) -> None:
        """Overlong URLs/handles are dropped, not clipped into wrong links."""
        api_data = {
            "first_name": "John",
            "linkedin_url": "https://linkedin.com/in/" + "x" * 600,
            "twitter_handle": "t" * 150,
            "github_handle": "g" * 150,
            "company_domain": "d" * 300 + ".com",
            "_raw": {},
        }

        person = _enrich(enrichment_service, workspace, api_data)

        assert person is not None
        assert person.linkedin_url == ""
        assert person.twitter_handle == ""
        assert person.github_handle == ""
        assert person.company_domain == ""

    def test_non_string_values_are_dropped_not_fatal(
        self, enrichment_service: EmailEnrichmentService, workspace: Workspace
    ) -> None:
        """A non-string value is dropped instead of aborting the insert.

        Seen in prod: nested twitter/github objects slipped through
        normalization, and their dict repr overflowed the varchar(100)
        handle columns, so no Person was ever cached.
        """
        api_data = {
            "first_name": "John",
            "twitter_handle": {"handle": None, "id": None, "bio": None},
            "github_handle": {"handle": None, "followers": None},
            "_raw": {},
        }

        person = _enrich(enrichment_service, workspace, api_data)

        assert person is not None
        assert person.first_name == "John"
        assert person.twitter_handle == ""
        assert person.github_handle == ""

    def test_values_within_limits_are_stored_unchanged(
        self, enrichment_service: EmailEnrichmentService, workspace: Workspace
    ) -> None:
        """Normal-length values pass through untouched."""
        api_data = {
            "first_name": "John",
            "last_name": "Doe",
            "position": "VP of Engineering",
            "linkedin_url": "https://linkedin.com/in/johndoe",
            "twitter_handle": "johndoe",
            "_raw": {},
        }

        person = _enrich(enrichment_service, workspace, api_data)

        assert person is not None
        assert person.first_name == "John"
        assert person.position == "VP of Engineering"
        assert person.linkedin_url == "https://linkedin.com/in/johndoe"
        assert person.twitter_handle == "johndoe"

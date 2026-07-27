"""Tests for workspace-scoped Person enrichment caching (GDPR isolation).

Covers:
- Person rows are unique per (workspace, email), not globally per email
- EmailEnrichmentService never serves one workspace's cached PII to another
- get_cached_person is workspace-scoped
- The 0026 data migration deletes unattributable cached Person rows
"""

from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Callable
from unittest.mock import patch

import pytest
from core.models import Person, Workspace
from core.services.email_enrichment import EmailEnrichmentService
from django.apps import apps as django_apps
from django.db import IntegrityError, transaction


def _fresh_hunter_data(**extra: Any) -> dict[str, Any]:
    """Return hunter_data with a fresh enrichment timestamp.

    Args:
        extra: Additional keys to merge into the payload.

    Returns:
        Dictionary suitable for Person.hunter_data.
    """
    return {
        "_enriched_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


@pytest.fixture
def workspace_a(db: None) -> Workspace:
    """Create the first test workspace."""
    return Workspace.objects.create(
        name="Workspace A",
        subscription_plan="pro",
        subscription_status="active",
    )


@pytest.fixture
def workspace_b(db: None) -> Workspace:
    """Create a second, unrelated test workspace."""
    return Workspace.objects.create(
        name="Workspace B",
        subscription_plan="pro",
        subscription_status="active",
    )


@pytest.fixture
def enrichment_service() -> EmailEnrichmentService:
    """Create an EmailEnrichmentService with tier/API-key checks bypassed."""
    service = EmailEnrichmentService()
    return service


def _enabled(service: EmailEnrichmentService) -> Any:
    """Return a context manager that bypasses tier and API-key checks.

    Args:
        service: The enrichment service under test.

    Returns:
        Context manager patching the tier and API-key lookups.
    """
    tier = patch.object(service, "_check_tier", return_value=True)
    key = patch.object(service, "_get_hunter_api_key", return_value="test-key")

    class _Both:
        def __enter__(self) -> None:
            tier.__enter__()
            key.__enter__()

        def __exit__(self, *args: Any) -> None:
            key.__exit__(*args)
            tier.__exit__(*args)

    return _Both()


@pytest.mark.django_db
class TestPersonWorkspaceScoping:
    """Tests for the per-workspace Person uniqueness model."""

    def test_same_email_allowed_in_two_workspaces(
        self, workspace_a: Workspace, workspace_b: Workspace
    ) -> None:
        """The same email can be cached independently by two workspaces."""
        Person.objects.create(workspace=workspace_a, email="john@example.com")
        Person.objects.create(workspace=workspace_b, email="john@example.com")

        assert Person.objects.filter(email="john@example.com").count() == 2
        assert workspace_a.enriched_people.count() == 1
        assert workspace_b.enriched_people.count() == 1

    def test_duplicate_email_in_same_workspace_rejected(
        self, workspace_a: Workspace
    ) -> None:
        """The same email cannot be cached twice within one workspace."""
        Person.objects.create(workspace=workspace_a, email="john@example.com")

        with pytest.raises(IntegrityError), transaction.atomic():
            Person.objects.create(workspace=workspace_a, email="john@example.com")

    def test_deleting_workspace_deletes_its_cached_people(
        self, workspace_a: Workspace, workspace_b: Workspace
    ) -> None:
        """Deleting a workspace cascades to its cached Person rows only."""
        Person.objects.create(workspace=workspace_a, email="john@example.com")
        Person.objects.create(workspace=workspace_b, email="john@example.com")

        workspace_a.delete()

        remaining = Person.objects.filter(email="john@example.com")
        assert remaining.count() == 1
        assert remaining.first().workspace_id == workspace_b.id  # type: ignore[union-attr]


@pytest.mark.django_db
class TestEmailEnrichmentServiceIsolation:
    """Tests that the enrichment service never crosses workspace boundaries."""

    def test_cached_person_served_to_owning_workspace(
        self,
        enrichment_service: EmailEnrichmentService,
        workspace_a: Workspace,
    ) -> None:
        """A fresh cached Person is returned without calling the API."""
        cached = Person.objects.create(
            workspace=workspace_a,
            email="john@example.com",
            first_name="John",
            hunter_data=_fresh_hunter_data(),
        )

        with (
            _enabled(enrichment_service),
            patch.object(enrichment_service, "_call_hunter_api") as mock_api,
        ):
            result = enrichment_service.enrich_email("john@example.com", workspace_a)

        assert result is not None
        assert result.id == cached.id
        mock_api.assert_not_called()

    def test_other_workspace_does_not_see_cached_person(
        self,
        enrichment_service: EmailEnrichmentService,
        workspace_a: Workspace,
        workspace_b: Workspace,
    ) -> None:
        """Workspace B triggers its own API call despite A's cached row."""
        Person.objects.create(
            workspace=workspace_a,
            email="john@example.com",
            first_name="CachedByA",
            hunter_data=_fresh_hunter_data(),
        )

        api_data = {"first_name": "FreshForB", "_raw": {}}
        with (
            _enabled(enrichment_service),
            patch.object(
                enrichment_service, "_call_hunter_api", return_value=api_data
            ) as mock_api,
        ):
            result = enrichment_service.enrich_email("john@example.com", workspace_b)

        mock_api.assert_called_once()
        assert result is not None
        assert result.workspace_id == workspace_b.id
        assert result.first_name == "FreshForB"

        # Workspace A's cached row is untouched; two independent rows exist.
        row_a = Person.objects.get(workspace=workspace_a, email="john@example.com")
        assert row_a.first_name == "CachedByA"
        assert Person.objects.filter(email="john@example.com").count() == 2

    def test_enrichment_creates_row_scoped_to_requesting_workspace(
        self,
        enrichment_service: EmailEnrichmentService,
        workspace_a: Workspace,
    ) -> None:
        """A cache miss stores the fetched data under the requesting workspace."""
        api_data = {"first_name": "John", "last_name": "Doe", "_raw": {}}
        with (
            _enabled(enrichment_service),
            patch.object(enrichment_service, "_call_hunter_api", return_value=api_data),
        ):
            result = enrichment_service.enrich_email("john@example.com", workspace_a)

        assert result is not None
        assert result.workspace_id == workspace_a.id
        assert (
            Person.objects.get(
                workspace=workspace_a, email="john@example.com"
            ).last_name
            == "Doe"
        )

    def test_get_cached_person_is_workspace_scoped(
        self,
        enrichment_service: EmailEnrichmentService,
        workspace_a: Workspace,
        workspace_b: Workspace,
    ) -> None:
        """get_cached_person only returns rows owned by the given workspace."""
        Person.objects.create(
            workspace=workspace_a,
            email="john@example.com",
            hunter_data=_fresh_hunter_data(),
        )

        assert (
            enrichment_service.get_cached_person("john@example.com", workspace_a)
            is not None
        )
        assert (
            enrichment_service.get_cached_person("john@example.com", workspace_b)
            is None
        )


def _get_person_purge() -> Callable[..., None]:
    """Import the row-deletion function from the 0026 data migration."""
    module = import_module("core.migrations.0026_delete_unattributable_person_rows")
    return module.delete_unattributable_person_rows  # type: ignore[no-any-return]


@pytest.mark.django_db
class TestDeleteUnattributablePersonRowsMigration:
    """Tests for the 0026 data migration purging the old global cache."""

    def test_deletes_all_person_rows(
        self, workspace_a: Workspace, workspace_b: Workspace
    ) -> None:
        """All cached Person rows are deleted (the cache re-fetches later)."""
        Person.objects.create(workspace=workspace_a, email="a@example.com")
        Person.objects.create(workspace=workspace_b, email="b@example.com")

        _get_person_purge()(django_apps, None)

        assert Person.objects.count() == 0

    def test_noop_on_empty_table(self) -> None:
        """Running the purge on an empty table is a no-op."""
        _get_person_purge()(django_apps, None)
        assert Person.objects.count() == 0

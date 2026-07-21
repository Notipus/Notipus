"""Tests for enrichment cache time-to-live (TTL) behavior.

Verifies that both domain and email enrichment honor ``CACHE_DURATION_DAYS``:
data older than the configured window is treated as stale (needs refresh),
while recent data is treated as fresh. Also covers the missing-timestamp and
indefinite-cache edge cases in the shared ``is_timestamp_fresh`` helper.
"""

from datetime import datetime, timedelta, timezone

import pytest
from core.models import Company, Person
from core.services.email_enrichment import EmailEnrichmentService
from core.services.enrichment import DomainEnrichmentService
from core.utils import is_timestamp_fresh


def _iso_days_ago(days: float) -> str:
    """Return an ISO 8601 UTC timestamp ``days`` in the past.

    Args:
        days: Number of days before now.

    Returns:
        ISO 8601 timestamp string.
    """
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestIsTimestampFresh:
    """Tests for the shared ``is_timestamp_fresh`` helper."""

    def test_recent_timestamp_is_fresh(self) -> None:
        """A timestamp within the window is fresh."""
        assert is_timestamp_fresh(_iso_days_ago(1), 30) is True

    def test_old_timestamp_is_stale(self) -> None:
        """A timestamp older than the window is stale."""
        assert is_timestamp_fresh(_iso_days_ago(31), 30) is False

    def test_missing_timestamp_is_stale(self) -> None:
        """A missing timestamp is treated as stale (needs refresh)."""
        assert is_timestamp_fresh(None, 30) is False
        assert is_timestamp_fresh("", 30) is False

    def test_unparseable_timestamp_is_stale(self) -> None:
        """An unparseable timestamp is treated as stale."""
        assert is_timestamp_fresh("not-a-date", 30) is False

    def test_non_string_timestamp_is_stale(self) -> None:
        """A non-string timestamp (legacy/corrupt JSON) is stale, not an error."""
        assert is_timestamp_fresh(12345, 30) is False
        assert is_timestamp_fresh(12345, None) is False
        assert is_timestamp_fresh(["2024-01-01"], 30) is False

    def test_none_duration_is_indefinite(self) -> None:
        """A None duration caches indefinitely for any valid timestamp."""
        assert is_timestamp_fresh(_iso_days_ago(3650), None) is True

    def test_none_duration_still_requires_parseable_timestamp(self) -> None:
        """A malformed timestamp is stale even under an indefinite cache."""
        assert is_timestamp_fresh("not-a-date", None) is False

    def test_zulu_suffix_timestamp_is_fresh(self) -> None:
        """A recent timestamp using the ``Z`` UTC suffix parses as fresh."""
        recent_zulu = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            microsecond=0, tzinfo=None
        ).isoformat() + "Z"
        assert is_timestamp_fresh(recent_zulu, 30) is True

    def test_old_zulu_suffix_timestamp_is_stale(self) -> None:
        """An old timestamp using the ``Z`` UTC suffix parses as stale."""
        assert is_timestamp_fresh("2024-01-01T00:00:00Z", 30) is False

    def test_naive_timestamp_treated_as_utc(self) -> None:
        """A naive timestamp is compared as if it were UTC."""
        naive = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
        assert is_timestamp_fresh(naive.isoformat(), 30) is True


@pytest.mark.django_db
class TestDomainEnrichmentCacheTTL:
    """Tests that DomainEnrichmentService honors CACHE_DURATION_DAYS."""

    def test_fresh_enrichment_is_not_stale(self) -> None:
        """Recently blended company data is considered enriched."""
        service = DomainEnrichmentService()
        ttl = service.CACHE_DURATION_DAYS
        assert ttl is not None
        company = Company(
            domain="fresh.com",
            brand_info={"name": "Fresh", "_blended_at": _iso_days_ago(ttl - 1)},
        )
        assert service._has_enrichment(company) is True

    def test_stale_enrichment_needs_refresh(self) -> None:
        """Company data older than the TTL is treated as needing refresh."""
        service = DomainEnrichmentService()
        ttl = service.CACHE_DURATION_DAYS
        assert ttl is not None
        company = Company(
            domain="stale.com",
            brand_info={"name": "Stale", "_blended_at": _iso_days_ago(ttl + 1)},
        )
        assert service._has_enrichment(company) is False

    def test_missing_timestamp_needs_refresh(self) -> None:
        """Company data without a blended timestamp needs refresh."""
        service = DomainEnrichmentService()
        company = Company(domain="notime.com", brand_info={"name": "NoTime"})
        assert service._has_enrichment(company) is False


@pytest.mark.django_db
class TestEmailEnrichmentCacheTTL:
    """Tests that EmailEnrichmentService honors CACHE_DURATION_DAYS."""

    def test_fresh_person_is_fresh(self) -> None:
        """A recently enriched person is considered fresh."""
        service = EmailEnrichmentService()
        ttl = service.CACHE_DURATION_DAYS
        assert ttl is not None
        person = Person(
            email="fresh@example.com",
            hunter_data={"_enriched_at": _iso_days_ago(ttl - 1)},
        )
        assert service._is_fresh(person) is True

    def test_stale_person_needs_refresh(self) -> None:
        """A person enriched longer ago than the TTL is treated as stale."""
        service = EmailEnrichmentService()
        ttl = service.CACHE_DURATION_DAYS
        assert ttl is not None
        person = Person(
            email="stale@example.com",
            hunter_data={"_enriched_at": _iso_days_ago(ttl + 1)},
        )
        assert service._is_fresh(person) is False

    def test_missing_timestamp_needs_refresh(self) -> None:
        """A person without an enrichment timestamp is stale."""
        service = EmailEnrichmentService()
        person = Person(email="notime@example.com", hunter_data={"first_name": "X"})
        assert service._is_fresh(person) is False

"""Robustness tests for the rate limiter's cache-failure handling.

These tests cover two deliberate robustness properties:

1. When the cache path fails, the failure is logged at ERROR (so outages
   are visible) and is recorded in the returned info as an unhealthy cache
   rather than silently being treated as "under limit". By default the
   limiter fails OPEN for availability, but ``RATE_LIMIT_FAIL_CLOSED`` lets
   operators opt into fail-closed behaviour.
2. The per-process in-memory fallback is bounded: it cannot grow without
   limit even when many distinct keys are written during a Redis outage.
"""

import logging
from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from webhooks.services.rate_limiter import RateLimiter


def _make_org(plan: str = "free", uuid: str = "org-abc") -> Mock:
    """Build a minimal organization stub for rate-limit checks.

    Args:
        plan: Subscription plan name.
        uuid: Organization UUID string.

    Returns:
        A mock exposing ``uuid`` and ``subscription_plan`` attributes.
    """
    return Mock(uuid=uuid, subscription_plan=plan)


class TestCacheFailureVisibility:
    """The cache path failing must be loud and must not read as under-limit."""

    def test_cache_failure_logs_error_and_marks_unhealthy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cache GET failure logs at ERROR and records an unhealthy cache.

        The degraded count must be surfaced via ``cache_healthy=False`` rather
        than silently being trusted as an authoritative "under limit" reading.
        """
        limiter = RateLimiter()
        org = _make_org()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            caplog.at_level(logging.ERROR, logger="webhooks.services.rate_limiter"),
        ):
            mock_cache.get.side_effect = Exception("redis down")
            is_allowed, info = limiter.check_rate_limit(org)

        # Fails open by default (availability), but the degradation is recorded.
        assert is_allowed is True
        assert info["cache_healthy"] is False

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "cache failure must be logged at ERROR"
        assert any("cache" in r.getMessage().lower() for r in error_records)

    @override_settings(RATE_LIMIT_FAIL_CLOSED=True)
    def test_fail_closed_setting_denies_on_cache_failure(self) -> None:
        """With RATE_LIMIT_FAIL_CLOSED, a cache failure denies the request.

        This proves the degraded reading is not silently treated as
        under-limit: operators can opt into rejecting traffic under
        uncertainty.
        """
        limiter = RateLimiter()
        org = _make_org()

        with patch("webhooks.services.rate_limiter.cache") as mock_cache:
            mock_cache.get.side_effect = Exception("redis down")
            is_allowed, info = limiter.check_rate_limit(org)

        assert is_allowed is False
        assert info["cache_healthy"] is False

    def test_healthy_cache_reports_cache_healthy(self) -> None:
        """A working cache reports ``cache_healthy=True`` on the happy path."""
        limiter = RateLimiter()
        org = _make_org()

        with patch("webhooks.services.rate_limiter.cache") as mock_cache:
            mock_cache.get.return_value = 0
            is_allowed, info = limiter.check_rate_limit(org)

        assert is_allowed is True
        assert info["cache_healthy"] is True


class TestInMemoryFallbackBounded:
    """The in-memory fallback must never grow without limit."""

    def test_fallback_does_not_exceed_cap(self) -> None:
        """Writing many distinct keys never exceeds the configured cap."""
        limiter = RateLimiter()
        limiter._max_fallback_keys = 50

        for i in range(500):
            limiter._set_to_fallback(f"key-{i}", i)
            assert len(limiter._in_memory_fallback) <= limiter._max_fallback_keys

        assert len(limiter._in_memory_fallback) == limiter._max_fallback_keys

    def test_fallback_eviction_keeps_newest_keys(self) -> None:
        """Eviction drops the oldest entries, retaining the most recent keys."""
        limiter = RateLimiter()
        limiter._max_fallback_keys = 10

        for i in range(100):
            limiter._set_to_fallback(f"key-{i}", i)

        # The most recently written key must survive eviction.
        assert "key-99" in limiter._in_memory_fallback
        # The oldest key must have been evicted.
        assert "key-0" not in limiter._in_memory_fallback

    def test_fallback_eviction_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exceeding the cap logs a WARNING signalling a likely outage."""
        limiter = RateLimiter()
        limiter._max_fallback_keys = 5

        with caplog.at_level(logging.WARNING, logger="webhooks.services.rate_limiter"):
            for i in range(20):
                limiter._set_to_fallback(f"key-{i}", i)

        assert any(
            "fallback exceeded" in r.getMessage().lower() for r in caplog.records
        )

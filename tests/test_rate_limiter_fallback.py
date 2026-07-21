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
from webhooks.services.rate_limiter import RateLimiter, RateLimitException


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

    def test_cache_failure_log_captures_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The cache-failure ERROR log includes the exception traceback.

        Using ``exc_info=True`` ensures the underlying error is not dropped
        from an ERROR-level log.
        """
        limiter = RateLimiter()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            caplog.at_level(logging.ERROR, logger="webhooks.services.rate_limiter"),
        ):
            mock_cache.get.side_effect = Exception("redis down")
            limiter._cache_get_with_health("some-key", 0)

        get_failures = [
            r for r in caplog.records if "cache get failed" in r.getMessage().lower()
        ]
        assert get_failures, "cache GET failure must be logged"
        assert any(r.exc_info is not None for r in get_failures), (
            "traceback must be captured via exc_info"
        )

    @override_settings(RATE_LIMIT_FAIL_CLOSED=True)
    def test_fail_closed_setting_denies_on_cache_failure(self) -> None:
        """With RATE_LIMIT_FAIL_CLOSED, a cache failure denies the request.

        This proves the degraded reading is not silently treated as
        under-limit: operators can opt into rejecting traffic under
        uncertainty. The denial must carry a ``cache_unavailable`` reason so
        it is distinguishable from a real quota breach.
        """
        limiter = RateLimiter()
        org = _make_org()

        with patch("webhooks.services.rate_limiter.cache") as mock_cache:
            mock_cache.get.side_effect = Exception("redis down")
            is_allowed, info = limiter.check_rate_limit(org)

        assert is_allowed is False
        assert info["cache_healthy"] is False
        assert info["reason"] == "cache_unavailable"

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
        limiter._fallback_low_water = 45

        for i in range(500):
            limiter._set_to_fallback(f"key-{i}", i)
            assert len(limiter._in_memory_fallback) <= limiter._max_fallback_keys

        # After batch eviction the size settles between the low-water mark and
        # the cap, never exceeding the cap.
        assert len(limiter._in_memory_fallback) <= limiter._max_fallback_keys
        assert len(limiter._in_memory_fallback) >= limiter._fallback_low_water

    def test_fallback_eviction_keeps_newest_keys(self) -> None:
        """Eviction drops the oldest entries, retaining the most recent keys."""
        limiter = RateLimiter()
        limiter._max_fallback_keys = 10
        limiter._fallback_low_water = 8

        for i in range(100):
            limiter._set_to_fallback(f"key-{i}", i)

        # The most recently written key must survive eviction.
        assert "key-99" in limiter._in_memory_fallback
        # The oldest key must have been evicted.
        assert "key-0" not in limiter._in_memory_fallback

    def test_eviction_is_batched_not_per_write(self) -> None:
        """Over-cap writes evict a batch, so the sort does not run every write.

        After the fallback first fills to the cap, a single eviction drops it
        to the low-water mark, leaving headroom so subsequent writes stay under
        the cap without triggering another eviction until the headroom is used.
        """
        limiter = RateLimiter()
        limiter._max_fallback_keys = 10
        limiter._fallback_low_water = 8

        # Fill exactly to the cap; no eviction yet.
        for i in range(10):
            limiter._set_to_fallback(f"key-{i}", i)
        assert len(limiter._in_memory_fallback) == 10

        # One more write exceeds the cap and triggers a batch eviction down to
        # the low-water mark (8), not a single-entry trim back to 10.
        limiter._set_to_fallback("key-10", 10)
        assert len(limiter._in_memory_fallback) == limiter._fallback_low_water

        # The next write stays within headroom and does not evict again.
        limiter._set_to_fallback("key-11", 11)
        assert len(limiter._in_memory_fallback) == limiter._fallback_low_water + 1

    def test_fallback_eviction_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exceeding the cap logs a WARNING signalling a likely outage."""
        limiter = RateLimiter()
        limiter._max_fallback_keys = 5
        limiter._fallback_low_water = 4

        with caplog.at_level(logging.WARNING, logger="webhooks.services.rate_limiter"):
            for i in range(20):
                limiter._set_to_fallback(f"key-{i}", i)

        assert any(
            "fallback exceeded" in r.getMessage().lower() for r in caplog.records
        )

    def test_eviction_warning_is_throttled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A sustained outage warns at most once per throttle interval.

        Writing far more keys than the cap must not emit a WARNING per
        over-cap write; the throttle keeps it to a single message within the
        interval.
        """
        limiter = RateLimiter()
        limiter._max_fallback_keys = 5
        limiter._fallback_low_water = 4
        limiter._eviction_warn_interval = 3600.0  # effectively "once" in test

        with caplog.at_level(logging.WARNING, logger="webhooks.services.rate_limiter"):
            for i in range(200):
                limiter._set_to_fallback(f"key-{i}", i)

        eviction_warnings = [
            r for r in caplog.records if "fallback exceeded" in r.getMessage().lower()
        ]
        assert len(eviction_warnings) == 1


class TestEnforceDenyReason:
    """enforce_rate_limit must distinguish cache outages from quota breaches."""

    @override_settings(RATE_LIMIT_FAIL_CLOSED=True)
    def test_cache_outage_raises_distinct_message(self) -> None:
        """A fail-closed cache outage raises a cache-specific message.

        The rejection must not be misattributed to the customer's usage.
        """
        limiter = RateLimiter()
        org = _make_org()

        with patch("webhooks.services.rate_limiter.cache") as mock_cache:
            mock_cache.get.side_effect = Exception("redis down")
            with pytest.raises(RateLimitException) as exc_info:
                limiter.enforce_rate_limit(org)

        message = str(exc_info.value).lower()
        assert "cache" in message
        assert "rate limit exceeded" not in message

    def test_actual_quota_breach_raises_exceeded_message(self) -> None:
        """A real quota breach still raises the standard exceeded message."""
        limiter = RateLimiter()
        org = _make_org(plan="free")  # free plan limit is 20

        with patch("webhooks.services.rate_limiter.cache") as mock_cache:
            mock_cache.get.return_value = 20  # at/over the limit, cache healthy
            with pytest.raises(RateLimitException) as exc_info:
                limiter.enforce_rate_limit(org)

        assert "rate limit exceeded" in str(exc_info.value).lower()

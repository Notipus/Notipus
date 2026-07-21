"""Rate limiter for webhook notifications.

This module provides rate limiting functionality based on subscription
plans, with Redis-backed storage and circuit breaker pattern for
graceful degradation.
"""

import logging
import time
from datetime import datetime
from typing import Any, ClassVar, cast

from django.conf import settings
from django.core.cache import cache
from django.core.cache.backends.base import InvalidCacheBackendError
from django.utils import timezone

logger = logging.getLogger(__name__)


class RedisCircuitBreaker:
    """Circuit breaker pattern for Redis operations.

    Handles cache failures gracefully by tracking failures and
    temporarily bypassing Redis when it's unavailable.

    Attributes:
        failure_threshold: Number of failures before opening circuit.
        recovery_timeout: Seconds to wait before trying again.
        failure_count: Current count of consecutive failures.
        last_failure_time: Timestamp of most recent failure.
        state: Current circuit state (CLOSED, OPEN, HALF_OPEN).
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60) -> None:
        """Initialize the circuit breaker.

        Args:
            failure_threshold: Number of failures before opening.
            recovery_timeout: Seconds before trying recovery.
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    def is_circuit_open(self) -> bool:
        """Check if circuit is open (Redis is considered down).

        Returns:
            True if circuit is open, False otherwise.
        """
        if self.state == "OPEN":
            if (
                self.last_failure_time
                and time.time() - self.last_failure_time > self.recovery_timeout
            ):
                self.state = "HALF_OPEN"
                logger.info("Redis circuit breaker entering HALF_OPEN state")
                return False
            return True
        return False

    def record_success(self) -> None:
        """Record successful Redis operation."""
        if self.state == "HALF_OPEN":
            self.state = "CLOSED"
            self.failure_count = 0
            self.last_failure_time = None
            logger.info("Redis circuit breaker returned to CLOSED state")

    def record_failure(self) -> None:
        """Record failed Redis operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(
                f"Redis circuit breaker opened after {self.failure_count} failures"
            )

    def call_with_circuit_breaker(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute function with circuit breaker protection.

        Args:
            func: Function to execute.
            *args: Positional arguments for the function.
            **kwargs: Keyword arguments for the function.

        Returns:
            Result of the function call.

        Raises:
            RedisUnavailableError: If circuit is open or operation fails.
        """
        if self.is_circuit_open():
            raise RedisUnavailableError("Redis circuit breaker is OPEN")

        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure()
            raise RedisUnavailableError(f"Redis operation failed: {e!s}") from e


class RedisUnavailableError(Exception):
    """Raised when Redis is unavailable."""


class RateLimitException(Exception):
    """Raised when rate limit is exceeded.

    Attributes:
        message: Description of the rate limit violation.
        limit: The rate limit that was exceeded.
        current_usage: Current usage count.
        reset_time: When the rate limit will reset.
    """

    def __init__(
        self, message: str, limit: int, current_usage: int, reset_time: datetime
    ) -> None:
        """Initialize the rate limit exception.

        Args:
            message: Description of the rate limit violation.
            limit: The rate limit that was exceeded.
            current_usage: Current usage count.
            reset_time: When the rate limit will reset.
        """
        self.message = message
        self.limit = limit
        self.current_usage = current_usage
        self.reset_time = reset_time
        super().__init__(message)


class RateLimiter:
    """Rate limiter for webhook notifications based on subscription plans.

    Uses Redis/Django cache for efficient counting with fallback
    mechanisms for graceful degradation.

    Attributes:
        PLAN_LIMITS: Mapping of plan names to monthly event limits.
    """

    PLAN_LIMITS: ClassVar[dict[str, int]] = {
        "free": 20,
        "basic": 10000,
        "pro": 100000,
        "enterprise": 1000000,  # 1 million events/month
    }

    def __init__(self) -> None:
        """Initialize the rate limiter."""
        self.cache_timeout = 60 * 60 * 24 * 31  # 31 days for monthly limits
        self.circuit_breaker = RedisCircuitBreaker()
        self._in_memory_fallback: dict[str, tuple[int, float]] = {}
        self._fallback_timeout = 300  # 5 minutes for in-memory cache
        # Hard cap on the number of keys tracked by the in-memory fallback.
        # The fallback is per-process, so during a prolonged Redis outage it
        # would otherwise grow one entry per organization/month without bound.
        self._max_fallback_keys = 10_000
        # When over the cap, evict in a batch down to this low-water mark so
        # pruning (and its sort) is amortized across many writes instead of
        # firing on every write once at capacity.
        self._fallback_low_water = 9_000
        # Throttle the eviction WARNING so a sustained outage cannot spam logs.
        self._eviction_warn_interval = 60.0  # seconds
        self._last_eviction_warn = 0.0

    def get_cache_key(self, organization_uuid: str, month: str) -> str:
        """Generate cache key for organization monthly usage.

        Args:
            organization_uuid: UUID of the organization.
            month: Month string in YYYY-MM format.

        Returns:
            Formatted cache key string.
        """
        return f"webhook_usage:{organization_uuid}:{month}"

    def get_current_month_key(self) -> str:
        """Get current month key in YYYY-MM format.

        Returns:
            Current month string.
        """
        return timezone.now().strftime("%Y-%m")

    def get_organization_limit(self, organization: Any) -> int:
        """Get the webhook limit for an organization based on their plan.

        Args:
            organization: Organization model instance.

        Returns:
            Monthly event limit for the organization's plan.
        """
        plan = organization.subscription_plan
        return self.PLAN_LIMITS.get(plan, 20)  # Default to free plan limit

    def _cache_get_with_health(self, key: str, default: int = 0) -> tuple[int, bool]:
        """Get a value from cache, reporting whether the cache path succeeded.

        Args:
            key: Cache key.
            default: Default value if not found.

        Returns:
            Tuple of (value, cache_healthy). When ``cache_healthy`` is False
            the value came from the bounded in-memory fallback and callers
            must treat the count as unreliable rather than authoritative --
            in particular it must not be assumed to mean "under limit".
        """
        try:
            value = cast(
                int,
                self.circuit_breaker.call_with_circuit_breaker(cache.get, key, default),
            )
            return value, True
        except (RedisUnavailableError, InvalidCacheBackendError, Exception) as e:
            # Log at ERROR (not WARNING/silent) so cache outages that degrade
            # quota enforcement are visible in monitoring instead of silently
            # disabling rate limiting.
            logger.error(
                "Cache GET failed for key %s; rate-limit enforcement degraded "
                "to in-memory fallback: %s",
                key,
                e,
                exc_info=True,
            )
            return self._get_from_fallback(key, default), False

    def _safe_cache_get(self, key: str, default: int = 0) -> int:
        """Get value from cache with fallback to in-memory storage.

        Args:
            key: Cache key.
            default: Default value if not found.

        Returns:
            Cached value or default.
        """
        value, _ = self._cache_get_with_health(key, default)
        return value

    def _safe_cache_set(self, key: str, value: int, timeout: int | None = None) -> bool:
        """Set value in cache with fallback to in-memory storage.

        Args:
            key: Cache key.
            value: Value to store.
            timeout: Optional timeout in seconds.

        Returns:
            True if cache set succeeded, False if using fallback.
        """
        try:
            self.circuit_breaker.call_with_circuit_breaker(
                cache.set, key, value, timeout or self.cache_timeout
            )
            # Also update fallback in case Redis goes down later
            self._set_to_fallback(key, value)
            return True
        except (RedisUnavailableError, InvalidCacheBackendError, Exception) as e:
            logger.error(
                f"Cache SET failed for key {key}, using fallback: {e!s}",
                exc_info=True,
            )
            self._set_to_fallback(key, value)
            return False

    def _safe_cache_incr(self, key: str) -> int:
        """Atomically increment a counter with in-memory fallback.

        Uses cache.incr (Redis INCR) so concurrent webhooks can't lose
        counts the way a read-then-set sequence does. A missing key is
        initialized with cache.add, which is atomic: only one racer wins
        the add, and the loser falls through to incr.

        Args:
            key: Cache key.

        Returns:
            New counter value after incrementing.
        """

        def _incr() -> int:
            try:
                return cast(int, cache.incr(key))
            except ValueError:
                # Key doesn't exist yet this month. add() is atomic; if a
                # concurrent request created it first, add() returns False
                # and incr() now succeeds.
                if cache.add(key, 1, self.cache_timeout):
                    return 1
                return cast(int, cache.incr(key))

        try:
            new_count = cast(int, self.circuit_breaker.call_with_circuit_breaker(_incr))
            # Mirror into the fallback in case Redis goes down later
            self._set_to_fallback(key, new_count)
            return new_count
        except (RedisUnavailableError, InvalidCacheBackendError, Exception) as e:
            logger.error(
                f"Cache INCR failed for key {key}, using fallback: {e!s}",
                exc_info=True,
            )
            new_count = self._get_from_fallback(key, 0) + 1
            self._set_to_fallback(key, new_count)
            return new_count

    def _get_from_fallback(self, key: str, default: int = 0) -> int:
        """Get value from in-memory fallback with expiration.

        Args:
            key: Cache key.
            default: Default value if not found.

        Returns:
            Cached value or default.
        """
        if key in self._in_memory_fallback:
            value, timestamp = self._in_memory_fallback[key]
            if time.time() - timestamp < self._fallback_timeout:
                return value
            else:
                # Expired, remove from fallback
                del self._in_memory_fallback[key]
        return default

    def _set_to_fallback(self, key: str, value: int) -> None:
        """Set value in in-memory fallback with timestamp.

        Args:
            key: Cache key.
            value: Value to store.
        """
        self._in_memory_fallback[key] = (value, time.time())
        self._prune_fallback()

    def _prune_fallback(self) -> None:
        """Bound the in-memory fallback so it cannot grow without limit.

        First drops expired entries. If the fallback still exceeds the hard
        cap, evicts the oldest entries in a single batch down to the
        low-water mark. Evicting in batches (rather than one entry per write
        once at capacity) means the O(n log n) sort is amortized across many
        writes, and the eviction WARNING is throttled so a sustained Redis
        outage cannot spam the log.
        """
        current_time = time.time()

        # Clean up expired entries to prevent memory leaks.
        expired_keys = [
            k
            for k, (_, timestamp) in self._in_memory_fallback.items()
            if current_time - timestamp >= self._fallback_timeout
        ]
        for expired_key in expired_keys:
            del self._in_memory_fallback[expired_key]

        # Only sort/evict when actually over the cap. Evicting down to the
        # low-water mark leaves headroom so the next prune (and its sort) is
        # deferred for many subsequent writes instead of firing per write.
        if len(self._in_memory_fallback) <= self._max_fallback_keys:
            return

        evict_count = len(self._in_memory_fallback) - self._fallback_low_water
        oldest = sorted(self._in_memory_fallback.items(), key=lambda item: item[1][1])[
            :evict_count
        ]
        for stale_key, _ in oldest:
            del self._in_memory_fallback[stale_key]

        # Reaching this branch means the fallback is under sustained pressure
        # (Redis is very likely unavailable), so surface it -- but throttle so
        # we warn at most once per interval.
        if current_time - self._last_eviction_warn >= self._eviction_warn_interval:
            self._last_eviction_warn = current_time
            logger.warning(
                "In-memory rate-limit fallback exceeded %s keys; evicted %s "
                "oldest entries down to %s. Redis is likely unavailable.",
                self._max_fallback_keys,
                evict_count,
                self._fallback_low_water,
            )

    def check_rate_limit(self, organization: Any) -> tuple[bool, dict[str, Any]]:
        """Check if organization is within rate limits.

        Args:
            organization: Organization model instance.

        Returns:
            Tuple of (is_allowed, rate_limit_info).
        """
        try:
            organization_uuid = str(organization.uuid)
            current_month = self.get_current_month_key()
            cache_key = self.get_cache_key(organization_uuid, current_month)

            # Get current usage, tracking whether the cache path is healthy.
            current_usage, cache_healthy = self._cache_get_with_health(cache_key, 0)
            limit = self.get_organization_limit(organization)

            # Check if within limits
            is_allowed = current_usage < limit
            deny_reason: str | None = None

            if not cache_healthy:
                # Deliberate availability tradeoff. The cache backing the quota
                # counter is unavailable, so ``current_usage`` came from the
                # bounded in-memory fallback and cannot be trusted -- we must
                # not silently treat it as "under limit". By default we still
                # fail OPEN (allow the webhook): a fail-closed default would
                # reject every legitimate webhook during a Redis outage, which
                # is worse than briefly under-enforcing quota. Operators who
                # prefer to reject traffic under uncertainty can opt into
                # fail-closed with RATE_LIMIT_FAIL_CLOSED = True.
                fail_closed = getattr(settings, "RATE_LIMIT_FAIL_CLOSED", False)
                is_allowed = not fail_closed
                # Distinguish a cache-outage denial from an actual quota breach
                # so enforce_rate_limit can raise a truthful message.
                if not is_allowed:
                    deny_reason = "cache_unavailable"
                logger.error(
                    "Rate-limit cache unavailable for org %s; enforcing %s "
                    "(RATE_LIMIT_FAIL_CLOSED=%s). Usage count is unreliable.",
                    organization_uuid,
                    "fail-closed (deny)" if fail_closed else "fail-open (allow)",
                    fail_closed,
                )

            # Calculate reset time (first day of next month)
            now = timezone.now()
            if now.month == 12:
                reset_time = now.replace(
                    year=now.year + 1,
                    month=1,
                    day=1,
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            else:
                reset_time = now.replace(
                    month=now.month + 1,
                    day=1,
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )

            # When the cache is unhealthy the usage count is unreliable, so the
            # true remaining quota is unknown. Report 0 (a safe sentinel) rather
            # than a computed value that would mislead consumers into thinking
            # quota remains.
            remaining = max(0, limit - current_usage) if cache_healthy else 0

            rate_limit_info: dict[str, Any] = {
                "limit": limit,
                "current_usage": current_usage,
                "remaining": remaining,
                "reset_time": reset_time,
                "plan": organization.subscription_plan,
                "fallback_mode": self.circuit_breaker.state != "CLOSED",
                "cache_healthy": cache_healthy,
                "reason": deny_reason,
            }

            return is_allowed, rate_limit_info

        except Exception as e:
            logger.error(
                f"Error checking rate limit for organization {organization.uuid}: {e!s}"
            )
            # Availability tradeoff (see check above): rate limiting failed
            # completely, so default to fail-open to avoid rejecting all
            # legitimate webhooks during an outage. Operators can opt into
            # fail-closed via RATE_LIMIT_FAIL_CLOSED = True.
            fail_closed = getattr(settings, "RATE_LIMIT_FAIL_CLOSED", False)
            return not fail_closed, {
                "limit": self.get_organization_limit(organization),
                "current_usage": 0,
                # Cache is unhealthy here too: remaining quota is unknown, so
                # use the safe sentinel (0) rather than the full plan limit.
                "remaining": 0,
                "reset_time": timezone.now(),
                "plan": organization.subscription_plan,
                "fallback_mode": True,
                "cache_healthy": False,
                "reason": "cache_unavailable" if fail_closed else None,
                "error": str(e),
            }

    def increment_usage(self, organization: Any) -> int:
        """Increment usage counter for organization and return new count.

        Args:
            organization: Organization model instance.

        Returns:
            New usage count.
        """
        try:
            organization_uuid = str(organization.uuid)
            current_month = self.get_current_month_key()
            cache_key = self.get_cache_key(organization_uuid, current_month)

            # Atomic increment: get-then-set loses counts under concurrency
            new_count = self._safe_cache_incr(cache_key)

            logger.info(
                f"Incremented webhook usage for org {organization_uuid} to {new_count} "
                f"(fallback_mode: {self.circuit_breaker.state != 'CLOSED'})"
            )
            return new_count

        except Exception as e:
            logger.error(
                f"Error incrementing usage for organization {organization.uuid}: {e!s}"
            )
            # Return 1 to indicate at least this request was processed
            return 1

    def enforce_rate_limit(self, organization: Any) -> dict[str, Any]:
        """Check rate limit and raise exception if exceeded.

        If within limits, increments usage counter.

        Args:
            organization: Organization model instance.

        Returns:
            Rate limit information dictionary.

        Raises:
            RateLimitException: If rate limit is exceeded.
        """
        is_allowed, rate_limit_info = self.check_rate_limit(organization)

        if not is_allowed:
            if rate_limit_info.get("reason") == "cache_unavailable":
                # The denial is a fail-closed reaction to a cache outage, not
                # an actual quota breach. Raise a truthful message so the
                # rejection is not misattributed to the customer's usage.
                raise RateLimitException(
                    "Rate limiting is failing closed because the cache backend "
                    "is unavailable (RATE_LIMIT_FAIL_CLOSED is enabled); "
                    "rejecting the request until the cache recovers.",
                    limit=rate_limit_info["limit"],
                    current_usage=rate_limit_info["current_usage"],
                    reset_time=rate_limit_info["reset_time"],
                )
            raise RateLimitException(
                f"Rate limit exceeded for plan '{organization.subscription_plan}'. "
                f"Limit: {rate_limit_info['limit']}, "
                f"Current usage: {rate_limit_info['current_usage']}",
                limit=rate_limit_info["limit"],
                current_usage=rate_limit_info["current_usage"],
                reset_time=rate_limit_info["reset_time"],
            )

        # Increment usage if allowed
        new_usage = self.increment_usage(organization)
        rate_limit_info["current_usage"] = new_usage
        # Only recompute remaining when the count is trustworthy; on a cache
        # outage the sentinel (0) set by check_rate_limit must be preserved so
        # consumers are not misled about how much quota is left.
        if rate_limit_info.get("cache_healthy", True):
            rate_limit_info["remaining"] = max(0, rate_limit_info["limit"] - new_usage)

        return rate_limit_info

    def get_rate_limit_headers(self, rate_limit_info: dict[str, Any]) -> dict[str, str]:
        """Generate HTTP headers for rate limiting information.

        Args:
            rate_limit_info: Rate limit information dictionary.

        Returns:
            Dictionary of HTTP header names and values.
        """
        headers = {
            "X-RateLimit-Limit": str(rate_limit_info.get("limit", 0)),
            "X-RateLimit-Remaining": str(rate_limit_info.get("remaining", 0)),
            "X-RateLimit-Used": str(rate_limit_info.get("current_usage", 0)),
            "X-RateLimit-Reset": str(
                int(rate_limit_info.get("reset_time", timezone.now()).timestamp())
            ),
            "X-RateLimit-Plan": rate_limit_info.get("plan", "unknown"),
        }

        return headers

    def get_usage_stats(self, organization: Any, months: int = 6) -> dict[str, int]:
        """Get usage statistics for an organization over the last N months.

        Args:
            organization: Organization model instance.
            months: Number of months to retrieve.

        Returns:
            Dictionary mapping month keys to usage counts.
        """
        stats: dict[str, int] = {}
        current_date = timezone.now()
        organization_uuid = str(organization.uuid)

        for i in range(months):
            # Calculate month
            if current_date.month - i <= 0:
                month = current_date.month - i + 12
                year = current_date.year - 1
            else:
                month = current_date.month - i
                year = current_date.year

            month_key = f"{year:04d}-{month:02d}"
            cache_key = self.get_cache_key(organization_uuid, month_key)
            usage = self._safe_cache_get(cache_key, 0)
            stats[month_key] = usage

        return stats


# Global rate limiter instance
rate_limiter = RateLimiter()

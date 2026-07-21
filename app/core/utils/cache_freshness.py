"""Cache freshness helpers for enrichment services.

Provides a shared, timezone-aware staleness check used by both the domain
and email enrichment services so cached data is refreshed once it grows
older than the configured cache duration.
"""

from datetime import datetime, timedelta, timezone


def is_timestamp_fresh(
    timestamp: str | None,
    max_age_days: int | None,
) -> bool:
    """Check whether an ISO 8601 timestamp is still within the cache window.

    Args:
        timestamp: ISO 8601 timestamp string (e.g. from ``datetime.isoformat``),
            or None when no enrichment has happened yet.
        max_age_days: Maximum age in days before the cached data is considered
            stale. ``None`` means the cache never expires (indefinite).

    Returns:
        True if the cached data should be treated as fresh. A missing or
        unparseable timestamp is always treated as stale (needs refresh).
    """
    if not timestamp:
        return False

    # Always validate the timestamp first so a malformed value is treated as
    # stale, even under an indefinite cache. Normalize a trailing "Z" (UTC
    # designator) which older datetime.fromisoformat() implementations reject.
    normalized = timestamp.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        enriched_at = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return False

    # Indefinite cache: any successfully parsed timestamp is fresh.
    if max_age_days is None:
        return True

    # Treat naive timestamps as UTC so comparisons stay timezone-aware.
    if enriched_at.tzinfo is None:
        enriched_at = enriched_at.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - enriched_at
    return age < timedelta(days=max_age_days)

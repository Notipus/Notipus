"""Raw Redis client access for the default Django cache.

Coordination primitives that the public cache API cannot express (SCAN
for key discovery, redis-py distributed locks) need the raw client
behind the default cache. This helper probes the two supported backend
shapes: django-redis (``cache.client.get_client()``) and Django's
built-in ``RedisCache`` (``cache._cache.get_client(None, write=True)``).

Reads and writes must still go through the cache API so key prefixing
and serialization stay consistent; use the raw client only for
operations the cache API does not expose (SCAN, locks).
"""

import logging
from typing import Any

from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_raw_redis_client() -> Any | None:
    """Return the raw Redis client backing the default cache, if any.

    Returns:
        A redis-py client, or None when the default cache is not backed
        by Redis (e.g. LocMemCache/DummyCache in tests) so callers can
        degrade gracefully.
    """
    try:
        return cache.client.get_client()  # type: ignore[attr-defined]
    except AttributeError:
        pass  # Not django-redis; try Django's built-in backend
    except Exception:
        logger.warning("Cannot access raw Redis client", exc_info=True)
        return None

    try:
        return cache._cache.get_client(None, write=True)  # type: ignore[attr-defined]
    except AttributeError:
        # Not a Redis-backed cache at all (LocMemCache/DummyCache in
        # dev/tests): an expected configuration, not a failure to log.
        return None
    except Exception:
        logger.warning("Cannot access raw Redis client", exc_info=True)
        return None

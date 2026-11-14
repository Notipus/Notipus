"""Tests for the per-workspace raw-webhook index.

The index used to be one global per-day JSON list, rewritten with a
non-atomic read-modify-write on every webhook, with record keys unique
only to the millisecond. Concurrent webhooks lost index entries and
same-millisecond webhooks overwrote each other's records.
"""

import fnmatch
import json
from typing import Any, Generator
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import RequestFactory, override_settings
from django.utils import timezone
from webhooks.services.webhook_storage import WebhookStorageService


class FakeRedis:
    """The handful of redis-py list commands the index uses."""

    def __init__(self) -> None:
        """Start with no keys."""
        self.lists: dict[str, list[bytes]] = {}
        self.expiries: dict[str, int] = {}

    def pipeline(self) -> "FakeRedis":
        """Return self; commands apply immediately.

        Returns:
            This client.
        """
        return self

    def execute(self) -> list[Any]:
        """No-op: commands were applied as issued.

        Returns:
            An empty result list.
        """
        return []

    def rpush(self, key: str, value: str) -> int:
        """Append to a list.

        Args:
            key: List key.
            value: Value to append.

        Returns:
            New list length.
        """
        self.lists.setdefault(key, []).append(value.encode())
        return len(self.lists[key])

    def ltrim(self, key: str, start: int, end: int) -> None:
        """Keep only the trailing ``-start`` items (the only form used).

        Args:
            key: List key.
            start: Negative start index.
            end: Must be -1.
        """
        self.lists[key] = self.lists.get(key, [])[start:]

    def expire(self, key: str, seconds: int) -> None:
        """Record an expiry.

        Args:
            key: Key.
            seconds: TTL.
        """
        self.expiries[key] = seconds

    def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        """Return a whole list.

        Args:
            key: List key.
            start: Must be 0.
            end: Must be -1.

        Returns:
            The list contents.
        """
        return list(self.lists.get(key, []))

    def scan_iter(self, match: str, count: int = 100) -> list[bytes]:
        """Return keys matching a glob.

        Args:
            match: Glob pattern.
            count: Ignored.

        Returns:
            Matching keys.
        """
        return [k.encode() for k in self.lists if fnmatch.fnmatch(k, match)]


@pytest.fixture
def locmem_cache() -> Generator[None, None, None]:
    """Use a real in-memory cache for the records.

    Yields:
        Nothing; the cache is cleared around the test.
    """
    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        cache.clear()
        yield
        cache.clear()


def _request(n: int) -> Any:
    """Build a webhook request with a distinguishable body.

    Args:
        n: Sequence number put in the body.

    Returns:
        The request.
    """
    return RequestFactory().post(
        "/webhook/", data=json.dumps({"id": n}), content_type="application/json"
    )


def test_same_millisecond_webhooks_are_all_kept(locmem_cache: None) -> None:
    """Records in one millisecond get distinct keys and are all indexed."""
    service = WebhookStorageService()
    frozen = timezone.now()
    with patch("webhooks.services.webhook_storage.timezone.now", return_value=frozen):
        for n in range(5):
            assert service.store_webhook(_request(n), "shopify", "ws-a")

    records = service.get_webhooks_by_date(
        frozen.strftime("%Y-%m-%d"), workspace_uuid="ws-a"
    )
    assert sorted(json.loads(r["body"])["id"] for r in records) == [0, 1, 2, 3, 4]


def test_index_is_a_per_workspace_redis_list(locmem_cache: None) -> None:
    """With raw Redis, each workspace gets its own RPUSH list with a TTL."""
    service = WebhookStorageService()
    redis = FakeRedis()
    with patch(
        "webhooks.services.webhook_storage.get_raw_redis_client", return_value=redis
    ):
        for n in range(3):
            service.store_webhook(_request(n), "shopify", "ws-a")
        service.store_webhook(_request(9), "stripe", "ws-b")

        today = timezone.now().strftime("%Y-%m-%d")
        ws_a = service.get_webhooks_by_date(today, workspace_uuid="ws-a")
        everything = service.get_webhooks_by_date(today)
        count = service.get_webhook_count_by_date(today)

    index_a = cache.make_key(f"webhook_raw_index:ws-a:{today}")
    index_b = cache.make_key(f"webhook_raw_index:ws-b:{today}")
    assert len(redis.lists[index_a]) == 3
    assert len(redis.lists[index_b]) == 1
    assert redis.expiries[index_a] == service.ttl_seconds
    assert len(ws_a) == 3
    assert len(everything) == 4
    assert count == 4
    # Nothing is written to the legacy global JSON index any more.
    assert cache.get(f"webhook_raw_index:{today}") is None


def test_record_carries_signed_body_hash(locmem_cache: None) -> None:
    """Stored records keep the SHA-256 of the exact signed bytes."""
    import hashlib

    service = WebhookStorageService()
    request = _request(1)
    service.store_webhook(request, "shopify", "ws-a")
    today = timezone.now().strftime("%Y-%m-%d")
    (record,) = service.get_webhooks_by_date(today, workspace_uuid="ws-a")
    assert record["body_sha256"] == hashlib.sha256(request.body).hexdigest()

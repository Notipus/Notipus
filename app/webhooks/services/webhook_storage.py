"""Webhook storage service for raw webhook payloads.

This module provides services for storing and retrieving raw webhook
payloads in Redis with 7-day TTL-based expiration for debugging and analysis.

Raw payloads are the most PII-dense data in the system (full customer
emails, names, addresses, order contents), so records are encrypted at
rest via core.encrypted_cache. The index stays plaintext: it holds only
cache key names.

The index is one Redis list per workspace per day, appended with RPUSH
(atomic, O(1)) and bounded with LTRIM. It used to be a single global
per-day JSON list rewritten with a read-modify-write on every webhook,
which lost entries under concurrency and grew without bound. Without a
raw Redis client (LocMemCache in dev) a per-workspace JSON list guarded
by a cache.add lock is used instead; whole-fleet listing (no workspace
given) then only sees the legacy global index.
"""

import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from core.encrypted_cache import decrypt_cache_value, encrypt_cache_value
from django.core.cache import cache
from django.http import HttpRequest
from django.utils import timezone

from .redis_client import get_raw_redis_client

logger = logging.getLogger(__name__)

# Upper bound on index entries kept per workspace per day. Records past
# this are still stored (and expire on their TTL) but are not listed.
MAX_INDEX_ENTRIES_PER_DAY = 10_000

# Fallback (non-Redis) index lock: short, because it guards a
# millisecond read-modify-write.
INDEX_LOCK_TTL_SECONDS = 5
INDEX_LOCK_ATTEMPTS = 20
INDEX_LOCK_RETRY_DELAY_SECONDS = 0.01


class WebhookStorageService:
    """Service for storing raw webhook payloads in Redis.

    Stores complete webhook request data including headers and body
    with automatic expiration for debugging and analysis purposes.

    Attributes:
        ttl_seconds: Time-to-live for webhook records (7 days).
    """

    # 7 days TTL for webhook storage
    TTL_SECONDS = 60 * 60 * 24 * 7  # 604800 seconds

    def __init__(self) -> None:
        """Initialize the webhook storage service."""
        self.ttl_seconds = self.TTL_SECONDS

    def _get_webhook_key(
        self,
        provider: str,
        workspace_uuid: str,
        timestamp_ms: int,
        unique: str | None = None,
    ) -> str:
        """Generate Redis key for raw webhook record.

        Args:
            provider: Webhook provider name (stripe, shopify, chargify).
            workspace_uuid: Workspace UUID or 'global' for billing webhooks.
            timestamp_ms: Timestamp in milliseconds, for ordering.
            unique: Random suffix. Two webhooks in the same millisecond
                would otherwise share a key and overwrite each other.

        Returns:
            Formatted Redis key string.
        """
        key = f"webhook_raw:{provider}:{workspace_uuid}:{timestamp_ms}"
        return f"{key}:{unique}" if unique else key

    def _get_index_key(self, date_str: str, workspace_uuid: str | None = None) -> str:
        """Generate Redis key for a daily webhook index.

        Args:
            date_str: Date string in YYYY-MM-DD format.
            workspace_uuid: Workspace UUID (or 'global'). None returns the
                legacy fleet-wide index, which is still read until its
                entries expire.

        Returns:
            Formatted Redis key for the daily index.
        """
        if workspace_uuid is None:
            return f"webhook_raw_index:{date_str}"
        return f"webhook_raw_index:{workspace_uuid}:{date_str}"

    def _extract_safe_headers(self, request: HttpRequest) -> dict[str, str | None]:
        """Extract relevant headers from request, masking sensitive values.

        Args:
            request: The HTTP request object.

        Returns:
            Dictionary of safe header values.
        """
        relevant_headers: dict[str, str | None] = {
            "Content-Type": request.headers.get("Content-Type"),
            "Content-Length": request.headers.get("Content-Length"),
            "User-Agent": request.headers.get("User-Agent"),
            "X-Forwarded-For": request.headers.get("X-Forwarded-For"),
            # Shopify webhook topic for debugging
            "X-Shopify-Topic": request.headers.get("X-Shopify-Topic"),
            # The sending shop, which app-level endpoints route on, and
            # the payload version, which explains schema differences.
            "X-Shopify-Shop-Domain": request.headers.get("X-Shopify-Shop-Domain"),
            "X-Shopify-API-Version": request.headers.get("X-Shopify-API-Version"),
            # Decides whether the event is processed at all, so its
            # absence from a capture makes a skipped webhook impossible
            # to explain.
            "X-Shopify-Test": request.headers.get("X-Shopify-Test"),
        }

        # Add provider-specific signature headers (masked for security)
        signature_headers = [
            "X-Shopify-Hmac-SHA256",
            "Stripe-Signature",
            "X-Chargify-Webhook-Signature-Hmac-Sha-256",
        ]
        for header in signature_headers:
            if header in request.headers:
                relevant_headers[header] = "[PRESENT]"

        return relevant_headers

    def store_webhook(
        self,
        request: HttpRequest,
        provider_name: str,
        workspace_uuid: str | None = None,
    ) -> bool:
        """Store a raw webhook payload in Redis with TTL.

        Args:
            request: The HTTP request containing the webhook.
            provider_name: Name of the webhook provider.
            workspace_uuid: Optional workspace UUID (None for global webhooks).

        Returns:
            True if storage was successful, False otherwise.
        """
        try:
            now = timezone.now()
            timestamp_ms = int(now.timestamp() * 1000)
            workspace_id = workspace_uuid or "global"

            # Get raw body
            try:
                raw_body = request.body.decode("utf-8")
            except UnicodeDecodeError:
                raw_body = request.body.decode("latin-1")

            # Try to parse as JSON for cleaner storage
            try:
                body_data = json.loads(raw_body)
                body_str = json.dumps(body_data, default=str)
            except (json.JSONDecodeError, TypeError):
                body_str = raw_body

            # Create webhook record. body_sha256 is the hash of the exact
            # signed bytes, which the live dedup key is built from; the
            # stored body is re-serialized, so it cannot be re-derived.
            webhook_record = {
                "provider": provider_name,
                "workspace_uuid": workspace_id,
                "timestamp": now.isoformat(),
                "timestamp_ms": timestamp_ms,
                "method": request.method,
                "path": request.path,
                "headers": self._extract_safe_headers(request),
                "body": body_str,
                "body_size": len(raw_body),
                "body_sha256": hashlib.sha256(request.body).hexdigest(),
            }

            # Store in Redis with TTL, encrypted (raw payloads carry the
            # full customer PII of the original webhook)
            webhook_key = self._get_webhook_key(
                provider_name, workspace_id, timestamp_ms, uuid.uuid4().hex[:12]
            )
            cache.set(
                webhook_key,
                encrypt_cache_value(webhook_record),
                timeout=self.ttl_seconds,
            )

            # Add to the workspace's daily index
            date_str = now.strftime("%Y-%m-%d")
            self._add_to_index(date_str, webhook_key, workspace_id)

            logger.debug(
                f"Stored raw webhook in Redis: {provider_name} "
                f"workspace={workspace_id} key={webhook_key}"
            )
            return True

        except Exception as e:
            # Don't let storage errors break webhook processing
            logger.warning(f"Failed to store webhook in Redis: {e}")
            return False

    def _add_to_index(
        self, date_str: str, webhook_key: str, workspace_id: str = "global"
    ) -> None:
        """Append a webhook key to the workspace's daily index.

        Args:
            date_str: Date string in YYYY-MM-DD format.
            webhook_key: Redis key for the webhook record.
            workspace_id: Workspace UUID or 'global'.
        """
        index_key = self._get_index_key(date_str, workspace_id)
        try:
            redis_client = get_raw_redis_client()
            if redis_client is not None:
                full_key = cache.make_key(index_key)
                pipe = redis_client.pipeline()
                pipe.rpush(full_key, webhook_key)
                pipe.ltrim(full_key, -MAX_INDEX_ENTRIES_PER_DAY, -1)
                pipe.expire(full_key, self.ttl_seconds)
                pipe.execute()
                return
            self._locked_json_append(index_key, webhook_key)
        except Exception as e:
            logger.warning(f"Failed to update webhook index: {e}")

    def _locked_json_append(self, index_key: str, webhook_key: str) -> None:
        """Append to a JSON-list index under a cache.add lock (no raw Redis).

        Args:
            index_key: Cache key of the index.
            webhook_key: Key to append.
        """
        lock_key = f"lock:{index_key}"
        token = uuid.uuid4().hex
        for _ in range(INDEX_LOCK_ATTEMPTS):
            if cache.add(lock_key, token, timeout=INDEX_LOCK_TTL_SECONDS):
                break
            time.sleep(INDEX_LOCK_RETRY_DELAY_SECONDS)
        else:
            logger.warning(f"Could not lock webhook index {index_key}; not indexed")
            return
        try:
            current = self._decode_json_index(cache.get(index_key))
            current.append(webhook_key)
            current = current[-MAX_INDEX_ENTRIES_PER_DAY:]
            cache.set(index_key, json.dumps(current), timeout=self.ttl_seconds)
        finally:
            if cache.get(lock_key) == token:
                cache.delete(lock_key)

    def _decode_json_index(self, value: Any) -> list[str]:
        """Decode a JSON-list index value read through the cache API.

        Args:
            value: The cached value (JSON string, list, or None).

        Returns:
            The list of webhook keys.
        """
        if value is None:
            return []
        if isinstance(value, str):
            value = json.loads(value)
        return [str(key) for key in value] if isinstance(value, list) else []

    def _read_index(self, index_key: str, redis_client: Any | None) -> list[str]:
        """Read one index, whichever form it was written in.

        Args:
            index_key: Logical cache key of the index.
            redis_client: Raw Redis client, or None.

        Returns:
            The webhook keys in the index.
        """
        if redis_client is not None:
            try:
                raw_keys = redis_client.lrange(cache.make_key(index_key), 0, -1)
                return [
                    key.decode("utf-8") if isinstance(key, bytes) else str(key)
                    for key in raw_keys
                ]
            except Exception:
                # WRONGTYPE: a JSON index written through the cache API
                # (the legacy global one, or the non-Redis fallback).
                pass
        return self._decode_json_index(cache.get(index_key))

    def _index_keys_for_date(
        self, date_str: str, workspace_uuid: str | None, redis_client: Any | None
    ) -> list[str]:
        """List the index keys to read for a date.

        Args:
            date_str: Date string in YYYY-MM-DD format.
            workspace_uuid: Restrict to one workspace, or None for all.
            redis_client: Raw Redis client, or None.

        Returns:
            Logical index keys, always including the legacy global index.
        """
        keys = [self._get_index_key(date_str)]
        if workspace_uuid is not None:
            keys.append(self._get_index_key(date_str, workspace_uuid))
            return keys
        if redis_client is None:
            return keys
        prefix = cache.make_key("")
        pattern = f"{prefix}webhook_raw_index:*:{date_str}"
        for full_key in redis_client.scan_iter(match=pattern, count=100):
            if isinstance(full_key, bytes):
                full_key = full_key.decode("utf-8")
            if full_key.startswith(prefix):
                full_key = full_key[len(prefix) :]
            keys.append(full_key)
        return keys

    def _webhook_keys_for_date(
        self, date_str: str, workspace_uuid: str | None
    ) -> list[str]:
        """Collect the webhook record keys indexed for a date.

        Args:
            date_str: Date string in YYYY-MM-DD format.
            workspace_uuid: Restrict to one workspace, or None for all.

        Returns:
            Unique webhook keys, in index order.
        """
        redis_client = get_raw_redis_client()
        seen: dict[str, None] = {}
        for index_key in self._index_keys_for_date(
            date_str, workspace_uuid, redis_client
        ):
            for key in self._read_index(index_key, redis_client):
                seen.setdefault(key, None)
        return list(seen)

    def get_webhooks_by_date(
        self,
        date_str: str,
        provider: str | None = None,
        workspace_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get webhooks for a specific date with optional filtering.

        Args:
            date_str: Date string in YYYY-MM-DD format.
            provider: Optional provider name to filter by.
            workspace_uuid: Optional workspace UUID to filter by.

        Returns:
            List of webhook records matching the criteria.
        """
        try:
            webhook_keys = self._webhook_keys_for_date(date_str, workspace_uuid)

            results: list[dict[str, Any]] = []
            for key in webhook_keys:
                # Decrypting; legacy plaintext entries written before
                # encryption come back as JSON strings and take the
                # json.loads branch
                webhook_data = decrypt_cache_value(cache.get(key))
                if webhook_data:
                    if isinstance(webhook_data, str):
                        webhook_data = json.loads(webhook_data)

                    # Apply filters
                    if provider and webhook_data.get("provider") != provider:
                        continue
                    if (
                        workspace_uuid
                        and webhook_data.get("workspace_uuid") != workspace_uuid
                    ):
                        continue

                    results.append(webhook_data)

            # Sort by timestamp (most recent first)
            results.sort(key=lambda x: x.get("timestamp_ms", 0), reverse=True)
            return results

        except Exception as e:
            logger.error(f"Error retrieving webhooks by date: {e}", exc_info=True)
            return []

    def get_recent_webhooks(
        self,
        days: int = 7,
        limit: int = 100,
        provider: str | None = None,
        workspace_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent webhooks from the last N days.

        Args:
            days: Number of days to look back (default 7).
            limit: Maximum number of records to return (default 100).
            provider: Optional provider name to filter by.
            workspace_uuid: Optional workspace UUID to filter by.

        Returns:
            List of webhook records, sorted by timestamp (most recent first).
        """
        try:
            all_webhooks: list[dict[str, Any]] = []

            # Iterate through each day
            for i in range(days):
                date = timezone.now() - timedelta(days=i)
                date_str = date.strftime("%Y-%m-%d")

                day_webhooks = self.get_webhooks_by_date(
                    date_str, provider=provider, workspace_uuid=workspace_uuid
                )
                all_webhooks.extend(day_webhooks)

                # Early exit if we have enough
                if len(all_webhooks) >= limit:
                    break

            # Sort by timestamp (most recent first) and limit
            all_webhooks.sort(key=lambda x: x.get("timestamp_ms", 0), reverse=True)
            return all_webhooks[:limit]

        except Exception as e:
            logger.error(f"Error retrieving recent webhooks: {e}", exc_info=True)
            return []

    def get_webhook_count_by_date(self, date_str: str) -> int:
        """Get count of webhooks for a specific date.

        Args:
            date_str: Date string in YYYY-MM-DD format.

        Returns:
            Count of webhooks stored for that date.
        """
        try:
            return len(self._webhook_keys_for_date(date_str, None))

        except Exception as e:
            logger.error(f"Error getting webhook count: {e}", exc_info=True)
            return 0


# Module-level singleton instance
webhook_storage_service = WebhookStorageService()

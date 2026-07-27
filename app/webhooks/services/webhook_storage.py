"""Webhook storage service for raw webhook payloads.

This module provides services for storing and retrieving raw webhook
payloads in Redis with 7-day TTL-based expiration for debugging and analysis.

Raw payloads are the most PII-dense data in the system (full customer
emails, names, addresses, order contents), so records are encrypted at
rest via core.encrypted_cache. The daily index stays plaintext: it holds
only cache key names.
"""

import json
import logging
from datetime import timedelta
from typing import Any

from core.encrypted_cache import decrypt_cache_value, encrypt_cache_value
from django.core.cache import cache
from django.http import HttpRequest
from django.utils import timezone

logger = logging.getLogger(__name__)


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
        self, provider: str, workspace_uuid: str, timestamp_ms: int
    ) -> str:
        """Generate Redis key for raw webhook record.

        Args:
            provider: Webhook provider name (stripe, shopify, chargify).
            workspace_uuid: Workspace UUID or 'global' for billing webhooks.
            timestamp_ms: Timestamp in milliseconds for uniqueness.

        Returns:
            Formatted Redis key string.
        """
        return f"webhook_raw:{provider}:{workspace_uuid}:{timestamp_ms}"

    def _get_index_key(self, date_str: str) -> str:
        """Generate Redis key for daily webhook index.

        Args:
            date_str: Date string in YYYY-MM-DD format.

        Returns:
            Formatted Redis key for daily index.
        """
        return f"webhook_raw_index:{date_str}"

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

            # Create webhook record
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
            }

            # Store in Redis with TTL, encrypted (raw payloads carry the
            # full customer PII of the original webhook)
            webhook_key = self._get_webhook_key(
                provider_name, workspace_id, timestamp_ms
            )
            cache.set(
                webhook_key,
                encrypt_cache_value(webhook_record),
                timeout=self.ttl_seconds,
            )

            # Add to daily index
            date_str = now.strftime("%Y-%m-%d")
            self._add_to_index(date_str, webhook_key)

            logger.debug(
                f"Stored raw webhook in Redis: {provider_name} "
                f"workspace={workspace_id} key={webhook_key}"
            )
            return True

        except Exception as e:
            # Don't let storage errors break webhook processing
            logger.warning(f"Failed to store webhook in Redis: {e}")
            return False

    def _add_to_index(self, date_str: str, webhook_key: str) -> None:
        """Add webhook key to daily index.

        Args:
            date_str: Date string in YYYY-MM-DD format.
            webhook_key: Redis key for the webhook record.
        """
        try:
            index_key = self._get_index_key(date_str)

            # Get current index
            current_index = cache.get(index_key)
            if current_index is None:
                current_index = []
            elif isinstance(current_index, str):
                current_index = json.loads(current_index)

            # Append new key
            current_index.append(webhook_key)

            # Store updated index with same TTL
            cache.set(index_key, json.dumps(current_index), timeout=self.ttl_seconds)

        except Exception as e:
            logger.warning(f"Failed to update webhook index: {e}")

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
            index_key = self._get_index_key(date_str)
            webhook_keys = cache.get(index_key)

            if webhook_keys is None:
                return []
            if isinstance(webhook_keys, str):
                webhook_keys = json.loads(webhook_keys)

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
            index_key = self._get_index_key(date_str)
            webhook_keys = cache.get(index_key)

            if webhook_keys is None:
                return 0
            if isinstance(webhook_keys, str):
                webhook_keys = json.loads(webhook_keys)

            return len(webhook_keys)

        except Exception as e:
            logger.error(f"Error getting webhook count: {e}", exc_info=True)
            return 0


# Module-level singleton instance
webhook_storage_service = WebhookStorageService()

"""Tests for the WebhookStorageService.

This module tests the Redis-based storage of raw webhook payloads
for debugging and analysis purposes.
"""

import json
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.encrypted_cache import decrypt_cache_value
from django.http import HttpRequest
from django.test.client import RequestFactory
from django.utils import timezone
from webhooks.services.webhook_storage import (
    WebhookStorageService,
    webhook_storage_service,
)


@pytest.fixture
def storage_service() -> WebhookStorageService:
    """Create a fresh WebhookStorageService instance.

    Returns:
        WebhookStorageService instance for testing.
    """
    return WebhookStorageService()


@pytest.fixture
def request_factory() -> RequestFactory:
    """Create a Django RequestFactory.

    Returns:
        RequestFactory instance for creating mock requests.
    """
    return RequestFactory()


@pytest.fixture
def mock_webhook_request(request_factory: RequestFactory) -> HttpRequest:
    """Create a mock webhook request for testing.

    Args:
        request_factory: Django RequestFactory instance.

    Returns:
        Mock HttpRequest simulating a Stripe webhook.
    """
    data = {
        "id": "evt_test123",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_test456",
                "amount": 2000,
                "currency": "usd",
            }
        },
    }
    request = request_factory.post(
        "/webhook/customer/test-uuid/stripe/",
        data=json.dumps(data),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="test_signature",
        HTTP_USER_AGENT="Stripe/1.0",
    )
    return request


@pytest.fixture
def mock_cache() -> MagicMock:
    """Create a mock cache object.

    Returns:
        MagicMock simulating Django's cache.
    """
    return MagicMock()


class TestWebhookStorageService:
    """Tests for WebhookStorageService class."""

    def test_ttl_is_7_days(self, storage_service: WebhookStorageService) -> None:
        """Test that TTL is correctly set to 7 days."""
        expected_ttl = 60 * 60 * 24 * 7  # 7 days in seconds
        assert storage_service.ttl_seconds == expected_ttl
        assert WebhookStorageService.TTL_SECONDS == expected_ttl

    def test_get_webhook_key_format(
        self, storage_service: WebhookStorageService
    ) -> None:
        """Test webhook key generation format."""
        key = storage_service._get_webhook_key("stripe", "workspace123", 1706234567890)
        assert key == "webhook_raw:stripe:workspace123:1706234567890"

    def test_get_index_key_format(self, storage_service: WebhookStorageService) -> None:
        """Test index key generation format."""
        key = storage_service._get_index_key("2026-01-25")
        assert key == "webhook_raw_index:2026-01-25"

    def test_extract_safe_headers(
        self,
        storage_service: WebhookStorageService,
        mock_webhook_request: HttpRequest,
    ) -> None:
        """Test that headers are extracted safely with signatures masked."""
        headers = storage_service._extract_safe_headers(mock_webhook_request)

        assert headers["Content-Type"] == "application/json"
        assert headers["User-Agent"] == "Stripe/1.0"
        assert headers["Stripe-Signature"] == "[PRESENT]"

    @patch("webhooks.services.webhook_storage.cache")
    @patch("webhooks.services.webhook_storage.timezone")
    def test_store_webhook_success(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
        mock_webhook_request: HttpRequest,
    ) -> None:
        """Test successful webhook storage."""
        # Setup mock timezone
        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        mock_cache.get.return_value = None

        result = storage_service.store_webhook(
            mock_webhook_request, "stripe", "workspace123"
        )

        assert result is True
        # Verify cache.set was called for the webhook record
        assert mock_cache.set.call_count >= 1

        # Check the webhook record structure (stored encrypted - raw
        # payloads are the most PII-dense data in the system)
        first_call_args = mock_cache.set.call_args_list[0]
        webhook_key = first_call_args[0][0]
        stored_value = first_call_args[0][1]
        assert stored_value.startswith("pqc1:")
        webhook_data = decrypt_cache_value(stored_value)

        assert webhook_key.startswith("webhook_raw:stripe:workspace123:")
        assert webhook_data["provider"] == "stripe"
        assert webhook_data["workspace_uuid"] == "workspace123"
        assert "body" in webhook_data
        assert "headers" in webhook_data
        assert webhook_data["method"] == "POST"

    @patch("webhooks.services.webhook_storage.cache")
    @patch("webhooks.services.webhook_storage.timezone")
    def test_store_webhook_global_workspace(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
        mock_webhook_request: HttpRequest,
    ) -> None:
        """Test webhook storage with no workspace (global)."""
        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        mock_cache.get.return_value = None

        result = storage_service.store_webhook(mock_webhook_request, "stripe", None)

        assert result is True
        first_call_args = mock_cache.set.call_args_list[0]
        webhook_key = first_call_args[0][0]
        webhook_data = decrypt_cache_value(first_call_args[0][1])

        assert "global" in webhook_key
        assert webhook_data["workspace_uuid"] == "global"

    @patch("webhooks.services.webhook_storage.cache")
    def test_store_webhook_handles_exception(
        self,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
        mock_webhook_request: HttpRequest,
    ) -> None:
        """Test that storage exceptions don't propagate."""
        mock_cache.set.side_effect = Exception("Redis connection failed")

        result = storage_service.store_webhook(
            mock_webhook_request, "stripe", "workspace123"
        )

        # Should return False but not raise
        assert result is False

    @patch("webhooks.services.webhook_storage.cache")
    def test_get_webhooks_by_date_empty(
        self,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test getting webhooks when none exist for date."""
        mock_cache.get.return_value = None

        result = storage_service.get_webhooks_by_date("2026-01-25")

        assert result == []

    @patch("webhooks.services.webhook_storage.cache")
    def test_get_webhooks_by_date_with_results(
        self,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test getting webhooks with existing records."""
        webhook_record = {
            "provider": "stripe",
            "workspace_uuid": "workspace123",
            "timestamp_ms": 1706234567890,
            "body": "{}",
        }

        def cache_get_side_effect(key: str) -> Any:
            if key == "webhook_raw_index:2026-01-25":
                return json.dumps(["webhook_raw:stripe:workspace123:1706234567890"])
            if key == "webhook_raw:stripe:workspace123:1706234567890":
                return json.dumps(webhook_record)
            return None

        mock_cache.get.side_effect = cache_get_side_effect

        result = storage_service.get_webhooks_by_date("2026-01-25")

        assert len(result) == 1
        assert result[0]["provider"] == "stripe"

    @patch("webhooks.services.webhook_storage.cache")
    def test_get_webhooks_by_date_with_provider_filter(
        self,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test filtering webhooks by provider."""
        stripe_record = {
            "provider": "stripe",
            "workspace_uuid": "workspace123",
            "timestamp_ms": 1706234567890,
        }
        shopify_record = {
            "provider": "shopify",
            "workspace_uuid": "workspace123",
            "timestamp_ms": 1706234567891,
        }

        def cache_get_side_effect(key: str) -> Any:
            if key == "webhook_raw_index:2026-01-25":
                return json.dumps(
                    [
                        "webhook_raw:stripe:workspace123:1706234567890",
                        "webhook_raw:shopify:workspace123:1706234567891",
                    ]
                )
            if key == "webhook_raw:stripe:workspace123:1706234567890":
                return json.dumps(stripe_record)
            if key == "webhook_raw:shopify:workspace123:1706234567891":
                return json.dumps(shopify_record)
            return None

        mock_cache.get.side_effect = cache_get_side_effect

        result = storage_service.get_webhooks_by_date("2026-01-25", provider="stripe")

        assert len(result) == 1
        assert result[0]["provider"] == "stripe"

    @patch("webhooks.services.webhook_storage.cache")
    def test_get_webhooks_by_date_with_workspace_filter(
        self,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test filtering webhooks by workspace."""
        record1 = {
            "provider": "stripe",
            "workspace_uuid": "workspace123",
            "timestamp_ms": 1706234567890,
        }
        record2 = {
            "provider": "stripe",
            "workspace_uuid": "workspace456",
            "timestamp_ms": 1706234567891,
        }

        def cache_get_side_effect(key: str) -> Any:
            if key == "webhook_raw_index:2026-01-25":
                return json.dumps(
                    [
                        "webhook_raw:stripe:workspace123:1706234567890",
                        "webhook_raw:stripe:workspace456:1706234567891",
                    ]
                )
            if key == "webhook_raw:stripe:workspace123:1706234567890":
                return json.dumps(record1)
            if key == "webhook_raw:stripe:workspace456:1706234567891":
                return json.dumps(record2)
            return None

        mock_cache.get.side_effect = cache_get_side_effect

        result = storage_service.get_webhooks_by_date(
            "2026-01-25", workspace_uuid="workspace123"
        )

        assert len(result) == 1
        assert result[0]["workspace_uuid"] == "workspace123"

    @patch("webhooks.services.webhook_storage.cache")
    @patch("webhooks.services.webhook_storage.timezone")
    def test_get_recent_webhooks(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test getting recent webhooks across multiple days."""
        now = timezone.now()
        mock_timezone.now.return_value = now

        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        today_record = {
            "provider": "stripe",
            "workspace_uuid": "workspace123",
            "timestamp_ms": 1706234567890,
        }
        yesterday_record = {
            "provider": "stripe",
            "workspace_uuid": "workspace123",
            "timestamp_ms": 1706148167890,
        }

        def cache_get_side_effect(key: str) -> Any:
            if key == f"webhook_raw_index:{today}":
                return json.dumps(["webhook_raw:stripe:workspace123:1706234567890"])
            if key == f"webhook_raw_index:{yesterday}":
                return json.dumps(["webhook_raw:stripe:workspace123:1706148167890"])
            if key == "webhook_raw:stripe:workspace123:1706234567890":
                return json.dumps(today_record)
            if key == "webhook_raw:stripe:workspace123:1706148167890":
                return json.dumps(yesterday_record)
            return None

        mock_cache.get.side_effect = cache_get_side_effect

        result = storage_service.get_recent_webhooks(days=2)

        assert len(result) == 2
        # Should be sorted by timestamp, most recent first
        assert result[0]["timestamp_ms"] > result[1]["timestamp_ms"]

    @patch("webhooks.services.webhook_storage.cache")
    @patch("webhooks.services.webhook_storage.timezone")
    def test_get_recent_webhooks_with_limit(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test that limit is respected."""
        now = timezone.now()
        mock_timezone.now.return_value = now
        today = now.strftime("%Y-%m-%d")

        # Create 5 records
        records = [
            {
                "provider": "stripe",
                "workspace_uuid": "workspace123",
                "timestamp_ms": 1706234567890 + i,
            }
            for i in range(5)
        ]
        keys = [
            f"webhook_raw:stripe:workspace123:{1706234567890 + i}" for i in range(5)
        ]

        def cache_get_side_effect(key: str) -> Any:
            if key == f"webhook_raw_index:{today}":
                return json.dumps(keys)
            for i, k in enumerate(keys):
                if key == k:
                    return json.dumps(records[i])
            return None

        mock_cache.get.side_effect = cache_get_side_effect

        result = storage_service.get_recent_webhooks(days=1, limit=3)

        assert len(result) == 3

    @patch("webhooks.services.webhook_storage.cache")
    def test_get_webhook_count_by_date(
        self,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test counting webhooks for a date."""
        mock_cache.get.return_value = json.dumps(
            [
                "webhook_raw:stripe:workspace123:1706234567890",
                "webhook_raw:stripe:workspace123:1706234567891",
                "webhook_raw:shopify:workspace123:1706234567892",
            ]
        )

        count = storage_service.get_webhook_count_by_date("2026-01-25")

        assert count == 3

    @patch("webhooks.services.webhook_storage.cache")
    def test_get_webhook_count_by_date_empty(
        self,
        mock_cache: MagicMock,
        storage_service: WebhookStorageService,
    ) -> None:
        """Test counting webhooks when none exist."""
        mock_cache.get.return_value = None

        count = storage_service.get_webhook_count_by_date("2026-01-25")

        assert count == 0


class TestWebhookStorageServiceSingleton:
    """Tests for the module-level singleton instance."""

    def test_singleton_exists(self) -> None:
        """Test that the singleton instance is available."""
        assert webhook_storage_service is not None
        assert isinstance(webhook_storage_service, WebhookStorageService)

    def test_singleton_has_correct_ttl(self) -> None:
        """Test singleton has 7-day TTL."""
        assert webhook_storage_service.ttl_seconds == 60 * 60 * 24 * 7


class TestWebhookStorageIntegration:
    """Integration tests for webhook storage with webhook router."""

    @patch("webhooks.webhook_router.webhook_storage_service")
    @patch("webhooks.webhook_router.settings")
    def test_log_webhook_payload_stores_to_redis(
        self,
        mock_settings: MagicMock,
        mock_storage: MagicMock,
        request_factory: RequestFactory,
    ) -> None:
        """Test that _log_webhook_payload calls storage service when enabled."""
        from webhooks.webhook_router import _log_webhook_payload

        mock_settings.LOG_WEBHOOKS = True

        request = request_factory.post(
            "/webhook/customer/test-uuid/stripe/",
            data=json.dumps({"test": "data"}),
            content_type="application/json",
        )

        _log_webhook_payload(request, "stripe", "test-uuid")

        mock_storage.store_webhook.assert_called_once_with(
            request, "stripe", "test-uuid"
        )

    @patch("webhooks.webhook_router.webhook_storage_service")
    @patch("webhooks.webhook_router.settings")
    def test_log_webhook_payload_skips_when_disabled(
        self,
        mock_settings: MagicMock,
        mock_storage: MagicMock,
        request_factory: RequestFactory,
    ) -> None:
        """Test that storage is skipped when LOG_WEBHOOKS is False."""
        from webhooks.webhook_router import _log_webhook_payload

        mock_settings.LOG_WEBHOOKS = False

        request = request_factory.post(
            "/webhook/customer/test-uuid/stripe/",
            data=json.dumps({"test": "data"}),
            content_type="application/json",
        )

        _log_webhook_payload(request, "stripe", "test-uuid")

        mock_storage.store_webhook.assert_not_called()

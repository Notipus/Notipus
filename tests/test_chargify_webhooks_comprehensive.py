"""Comprehensive tests for Chargify webhook implementation.

This module tests webhook signature validation, data parsing,
deduplication, timestamp handling, and error scenarios for the
Chargify provider.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory
from plugins.sources.base import InvalidDataError
from plugins.sources.chargify import ChargifySourcePlugin


def _mock_chargify_request(
    form_data: dict[str, str], webhook_id: str = "webhook_123"
) -> MagicMock:
    """Build a mock Chargify webhook request.

    Args:
        form_data: Form-encoded webhook payload.
        webhook_id: Value for the X-Chargify-Webhook-Id header.

    Returns:
        MagicMock mimicking a Django HttpRequest.
    """
    mock_request = MagicMock()
    mock_request.content_type = "application/x-www-form-urlencoded"
    mock_request.headers = {"X-Chargify-Webhook-Id": webhook_id}
    mock_request.POST.dict.return_value = form_data
    return mock_request


class TestChargifyWebhookValidation:
    """Test Chargify webhook signature validation."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret_key")

    @pytest.fixture
    def request_factory(self) -> RequestFactory:
        """Create a Django request factory."""
        return RequestFactory()

    def test_sha256_signature_validation(self, provider, request_factory):
        """Test SHA-256 signature validation"""
        body = b"event=payment_success&payload[subscription][id]=12345"
        expected_signature = "a1b2c3d4e5f6"  # Mock signature

        request = request_factory.post(
            "/webhook/chargify/",
            data=body,
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_123",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256=expected_signature,
        )

        with patch("hmac.compare_digest", return_value=True):
            assert provider.validate_webhook(request) is True

    def test_md5_signature_fallback(self, provider, request_factory):
        """Test MD5 signature fallback when SHA-256 not available"""
        body = b"event=payment_success&payload[subscription][id]=12345"
        md5_signature = "legacy_md5_signature"

        request = request_factory.post(
            "/webhook/chargify/",
            data=body,
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_123",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE=md5_signature,
        )

        with patch("hmac.compare_digest", return_value=True):
            assert provider.validate_webhook(request) is True

    def test_missing_webhook_id_rejected(self, provider, request_factory):
        """Test webhook rejection when webhook ID is missing"""
        request = request_factory.post(
            "/webhook/chargify/",
            data="event=payment_success",
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256="signature",
        )

        assert provider.validate_webhook(request) is False

    def test_missing_signature_rejected(self, provider, request_factory):
        """Test webhook rejection when signature is missing"""
        request = request_factory.post(
            "/webhook/chargify/",
            data="event=payment_success",
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_123",
        )

        assert provider.validate_webhook(request) is False

    def test_invalid_signature_rejected(self, provider, request_factory):
        """Test webhook rejection with invalid signature"""
        request = request_factory.post(
            "/webhook/chargify/",
            data="event=payment_success",
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_123",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256="invalid_signature",
        )

        with patch("hmac.compare_digest", return_value=False):
            assert provider.validate_webhook(request) is False

    def test_no_secret_configured_rejects_even_in_debug(
        self, request_factory, settings
    ):
        """Test that missing webhook secret rejects even in DEBUG mode.

        There is no development bypass: an empty secret means the signature
        cannot be verified, so the webhook must be rejected.
        """
        provider = ChargifySourcePlugin(webhook_secret="")

        request = request_factory.post(
            "/webhook/chargify/",
            data="event=payment_success",
            content_type="application/x-www-form-urlencoded",
        )

        # Even in DEBUG mode, empty secret must reject
        settings.DEBUG = True
        assert provider.validate_webhook(request) is False

    def test_no_secret_configured_rejects_in_production(
        self, request_factory, settings
    ):
        """Test that missing webhook secret rejects in production (DEBUG=False)"""
        provider = ChargifySourcePlugin(webhook_secret="")

        request = request_factory.post(
            "/webhook/chargify/",
            data="event=payment_success",
            content_type="application/x-www-form-urlencoded",
        )

        # In production mode, empty secret should reject
        settings.DEBUG = False
        assert provider.validate_webhook(request) is False


class TestChargifyWebhookParsing:
    """Test Chargify webhook data parsing."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    @pytest.fixture
    def request_factory(self) -> RequestFactory:
        """Create a Django request factory."""
        return RequestFactory()

    def test_payment_success_parsing(self, provider, request_factory):
        """Test parsing payment_success webhook"""
        form_data = {
            "event": "payment_success",
            "payload[subscription][id]": "sub_12345",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][first_name]": "John",
            "payload[subscription][customer][last_name]": "Doe",
            "payload[subscription][customer][organization]": "Acme Corp",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[transaction][id]": "txn_789",
            "payload[transaction][amount_in_cents]": "2999",
            "payload[transaction][memo]": "Payment for Shopify Order 12345",
            "created_at": "2024-01-15T10:30:00Z",
        }

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        result = provider.parse_webhook(mock_request)

        assert result["type"] == "payment_success"
        assert result["customer_id"] == "cust_456"
        assert result["amount"] == 29.99
        assert result["currency"] == "USD"
        assert result["status"] == "success"
        assert result["metadata"]["subscription_id"] == "sub_12345"
        assert result["metadata"]["transaction_id"] == "txn_789"
        assert result["metadata"]["shopify_order_ref"] == "12345"

    def test_payment_failure_parsing(self, provider, request_factory):
        """Test parsing payment_failure webhook"""
        form_data = {
            "event": "payment_failure",
            "payload[subscription][id]": "sub_12345",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][organization]": "Acme Corp",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[transaction][id]": "txn_789",
            "payload[transaction][amount_in_cents]": "2999",
            "payload[transaction][failure_message]": "Insufficient funds",
            "created_at": "2024-01-15T10:30:00Z",
        }

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        result = provider.parse_webhook(mock_request)

        assert result["type"] == "payment_failure"
        assert result["status"] == "failed"
        assert result["metadata"]["failure_reason"] == "Insufficient funds"

    def test_subscription_state_change_parsing(self, provider, request_factory):
        """Test parsing subscription_state_change webhook"""
        form_data = {
            "event": "subscription_state_change",
            "payload[subscription][id]": "sub_12345",
            "payload[subscription][state]": "canceled",
            "payload[subscription][previous_state]": "active",
            "payload[subscription][cancel_at_end_of_period]": "true",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][organization]": "Acme Corp",
            "payload[subscription][product][name]": "Premium Plan",
            "created_at": "2024-01-15T10:30:00Z",
        }

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        result = provider.parse_webhook(mock_request)

        # A change into a cancellation-like state is normalized to the
        # subscription_canceled type the event processor understands,
        # with the raw Chargify state preserved in status/metadata.
        assert result["type"] == "subscription_canceled"
        assert result["status"] == "canceled"
        assert result["provider"] == "chargify"
        assert result["metadata"]["new_state"] == "canceled"
        assert result["metadata"]["previous_state"] == "active"
        assert result["metadata"]["cancel_at_period_end"] is True
        assert result["metadata"]["chargify_event"] == "subscription_state_change"

    def test_invalid_content_type_rejected(self, provider, request_factory):
        """Test rejection of invalid content type"""
        request = request_factory.post(
            "/webhook/chargify/",
            data='{"event": "payment_success"}',
            content_type="application/json",
        )

        with pytest.raises(InvalidDataError, match="Invalid content type"):
            provider.parse_webhook(request)

    def test_missing_event_type_rejected(self, provider, request_factory):
        """Test rejection when event type is missing"""
        request = request_factory.post(
            "/webhook/chargify/",
            data={"payload[subscription][id]": "sub_12345"},
            content_type="application/x-www-form-urlencoded",
        )

        with pytest.raises(InvalidDataError, match="Missing event type"):
            provider.parse_webhook(request)

    def test_missing_customer_id_rejected(self, provider, request_factory):
        """Test rejection when customer ID is missing"""
        form_data = {"event": "payment_success"}

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        with pytest.raises(InvalidDataError, match="Missing customer ID"):
            provider.parse_webhook(mock_request)

    def test_unknown_event_type_logged_and_skipped(
        self, provider: ChargifySourcePlugin, request_factory: RequestFactory
    ) -> None:
        """Test that unknown event types are logged and skipped, not rejected.

        A 400 for a legitimate Chargify event would make Chargify retry
        the webhook forever, so unknown events must be acknowledged.
        """
        form_data = {
            "event": "unsupported_event",
            "payload[subscription][customer][id]": "cust_456",
        }

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        assert provider.parse_webhook(mock_request) is None


class TestChargifyWebhookDeduplication:
    """Test Chargify webhook deduplication key handling.

    Deduplication itself happens at the router level via the event
    consolidation service; the plugin's job is to surface a stable
    dedup key (the webhook id) and reject webhooks without one.
    """

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    @pytest.fixture
    def form_data(self) -> dict[str, str]:
        """Valid payment_success form data."""
        return {
            "event": "payment_success",
            "payload[subscription][id]": "sub_12345",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[transaction][id]": "txn_789",
            "payload[transaction][amount_in_cents]": "2999",
            "created_at": "2024-01-15T10:30:00Z",
        }

    def test_webhook_id_surfaced_as_event_id(
        self, provider: ChargifySourcePlugin, form_data: dict[str, str]
    ) -> None:
        """Test that the webhook id is surfaced for router-level dedup."""
        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {"X-Chargify-Webhook-Id": "webhook_12345"}
        mock_request.POST.dict.return_value = form_data

        result = provider.parse_webhook(mock_request)

        assert result is not None
        assert result["event_id"] == "webhook_12345"

    def test_missing_webhook_id_rejected_in_parse(
        self, provider: ChargifySourcePlugin, form_data: dict[str, str]
    ) -> None:
        """Test that a missing X-Chargify-Webhook-Id fails validation.

        Without the webhook id there is no stable dedup key, so the
        webhook must be rejected (400) rather than bypassing dedup.
        """
        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {}
        mock_request.POST.dict.return_value = form_data

        with pytest.raises(InvalidDataError, match="X-Chargify-Webhook-Id"):
            provider.parse_webhook(mock_request)


class TestChargifyWebhookTimestampValidation:
    """Test Chargify webhook timestamp validation."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    @pytest.fixture
    def request_factory(self) -> RequestFactory:
        """Create a Django request factory."""
        return RequestFactory()

    def test_valid_timestamp_accepted(self, provider, request_factory):
        """Test that valid recent timestamp is accepted"""
        # Current timestamp
        current_time = datetime.now(timezone.utc)
        timestamp = current_time.isoformat().replace("+00:00", "Z")

        mock_request = MagicMock()
        mock_request.headers = {"X-Chargify-Webhook-Timestamp": timestamp}

        assert provider._validate_webhook_timestamp(mock_request) is True

    def test_old_timestamp_rejected(self, provider, request_factory):
        """Test that old timestamp is rejected"""
        # Timestamp from 10 minutes ago (beyond tolerance)
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        timestamp = old_time.isoformat().replace("+00:00", "Z")

        mock_request = MagicMock()
        mock_request.headers = {"X-Chargify-Webhook-Timestamp": timestamp}

        assert provider._validate_webhook_timestamp(mock_request) is False

    def test_future_timestamp_rejected(self, provider, request_factory):
        """Test that future timestamp is rejected"""
        # Timestamp from 10 minutes in the future (beyond tolerance)
        future_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        timestamp = future_time.isoformat().replace("+00:00", "Z")

        mock_request = MagicMock()
        mock_request.headers = {"X-Chargify-Webhook-Timestamp": timestamp}

        assert provider._validate_webhook_timestamp(mock_request) is False

    def test_slightly_future_timestamp_rejected(
        self, provider: ChargifySourcePlugin, request_factory: RequestFactory
    ) -> None:
        """Test that a timestamp 4 minutes in the future is rejected.

        The tolerance window is one-sided: 4 minutes would be accepted
        for a webhook from the past, but future-dated webhooks only get
        a small clock-skew allowance.
        """
        future_time = datetime.now(timezone.utc) + timedelta(minutes=4)
        timestamp = future_time.isoformat().replace("+00:00", "Z")

        mock_request = MagicMock()
        mock_request.headers = {"X-Chargify-Webhook-Timestamp": timestamp}

        assert provider._validate_webhook_timestamp(mock_request) is False

    def test_clock_skew_future_timestamp_accepted(
        self, provider: ChargifySourcePlugin, request_factory: RequestFactory
    ) -> None:
        """Test that a timestamp within the clock-skew allowance is accepted."""
        future_time = datetime.now(timezone.utc) + timedelta(seconds=30)
        timestamp = future_time.isoformat().replace("+00:00", "Z")

        mock_request = MagicMock()
        mock_request.headers = {"X-Chargify-Webhook-Timestamp": timestamp}

        assert provider._validate_webhook_timestamp(mock_request) is True

    def test_past_timestamp_within_tolerance_accepted(
        self, provider: ChargifySourcePlugin, request_factory: RequestFactory
    ) -> None:
        """Test that a 4-minute-old timestamp is still accepted."""
        past_time = datetime.now(timezone.utc) - timedelta(minutes=4)
        timestamp = past_time.isoformat().replace("+00:00", "Z")

        mock_request = MagicMock()
        mock_request.headers = {"X-Chargify-Webhook-Timestamp": timestamp}

        assert provider._validate_webhook_timestamp(mock_request) is True

    def test_missing_timestamp_accepted(self, provider, request_factory):
        """Test that missing timestamp is accepted (optional field)"""
        mock_request = MagicMock()
        mock_request.headers = {}

        assert provider._validate_webhook_timestamp(mock_request) is True

    def test_invalid_timestamp_format_rejected(self, provider, request_factory):
        """Test that invalid timestamp format is rejected"""
        mock_request = MagicMock()
        mock_request.headers = {"X-Chargify-Webhook-Timestamp": "invalid-timestamp"}

        assert provider._validate_webhook_timestamp(mock_request) is False

    def test_timestamp_validation_in_webhook_validation(
        self, provider, request_factory
    ):
        """Test that timestamp validation is called during webhook validation"""
        # Test that webhook validation includes timestamp check
        mock_request = MagicMock()
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
            "X-Chargify-Webhook-Signature-Hmac-Sha-256": "signature",
            "X-Chargify-Webhook-Timestamp": "invalid-timestamp",
        }
        mock_request.body = b"test_body"

        # Should fail due to invalid timestamp
        with patch.object(provider, "webhook_secret", "test_secret"):
            assert provider.validate_webhook(mock_request) is False


class TestChargifyShopifyOrderMatching:
    """Test Shopify order reference extraction from Chargify memos."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    def test_explicit_shopify_order_extraction(self, provider):
        """Test extraction of explicit Shopify order references"""
        test_cases = [
            ("Payment for Shopify Order 12345", "12345"),
            ("Shopify Order #67890 payment", "67890"),
            ("shopify order: 54321", "54321"),
            ("SHOPIFY ORDER 98765", "98765"),
        ]

        for memo, expected in test_cases:
            result = provider._parse_shopify_order_ref(memo)
            assert result == expected, f"Failed for memo: {memo}"

    def test_allocated_order_extraction(self, provider):
        """Test extraction from allocation text"""
        memo = "$29.99 allocated to order 12345"
        result = provider._parse_shopify_order_ref(memo)
        assert result == "12345"

    def test_generic_order_extraction(self, provider):
        """Test extraction from generic order mentions"""
        memo = "Customer payment for order 54321"
        result = provider._parse_shopify_order_ref(memo)
        assert result == "54321"

    def test_no_order_reference_returns_none(self, provider):
        """Test that memos without order references return None"""
        test_cases = [
            "",
            "Regular subscription payment",
            "Monthly charge",
            "No order mentioned here",
        ]

        for memo in test_cases:
            result = provider._parse_shopify_order_ref(memo)
            assert result is None, f"Should return None for memo: {memo}"

    @pytest.mark.parametrize(
        "memo",
        [
            "Payment for reorder created 2024-01-15",
            "Preorder deposit received 2024-01-15",
            "Backorder notice sent on 2023-12-01",
        ],
    )
    def test_order_substring_false_positives_rejected(
        self, provider: ChargifySourcePlugin, memo: str
    ) -> None:
        """Test that 'reorder'/'preorder' and dates do not match as order refs.

        Args:
            provider: Chargify plugin under test.
            memo: Memo text containing an 'order' substring but no order ref.
        """
        assert provider._parse_shopify_order_ref(memo) is None

    def test_short_order_number_rejected(self, provider: ChargifySourcePlugin) -> None:
        """Test that generic order refs with fewer than 4 digits are rejected."""
        assert provider._parse_shopify_order_ref("Payment for order 123") is None

    def test_order_with_separator_extracted(
        self, provider: ChargifySourcePlugin
    ) -> None:
        """Test that order refs with '#' or ':' separators still match."""
        assert provider._parse_shopify_order_ref("Charge for order #12345") == "12345"
        assert provider._parse_shopify_order_ref("Charge for order: 6789") == "6789"


class TestChargifyErrorHandling:
    """Test error handling in Chargify webhook processing."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    @pytest.fixture
    def request_factory(self) -> RequestFactory:
        """Create a Django request factory."""
        return RequestFactory()

    def test_malformed_amount_handling(self, provider, request_factory):
        """Test handling of malformed amount values"""
        form_data = {
            "event": "payment_success",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][organization]": "Test Company",
            "payload[subscription][id]": "sub_123",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[transaction][amount_in_cents]": "invalid_amount",
            "created_at": "2024-01-15T10:30:00Z",
        }

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        with pytest.raises(InvalidDataError, match="Invalid amount format"):
            provider.parse_webhook(mock_request)

    def test_missing_transaction_amount(self, provider, request_factory):
        """Test handling when transaction amount is missing"""
        form_data = {
            "event": "payment_success",
            "payload[subscription][customer][id]": "cust_456",
            "created_at": "2024-01-15T10:30:00Z",
        }

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        with pytest.raises(InvalidDataError, match="Missing amount"):
            provider.parse_webhook(mock_request)

    def test_webhook_validation_exception_handling(self, provider, request_factory):
        """Test exception handling in webhook validation"""
        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
            "X-Chargify-Webhook-Signature-Hmac-Sha-256": "signature",
        }
        mock_request.body = b"event=payment_success"

        # Mock an exception in validation
        with patch("hmac.compare_digest", side_effect=Exception("Validation error")):
            assert provider.validate_webhook(mock_request) is False

    def test_large_payload_handling(self, provider, request_factory):
        """Test handling of unusually large webhook payloads"""
        # Create a large payload
        large_memo = "x" * 10000  # 10KB memo
        form_data = {
            "event": "payment_success",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][organization]": "Test Company",
            "payload[subscription][id]": "sub_123",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[transaction][amount_in_cents]": "2999",
            "payload[transaction][memo]": large_memo,
            "created_at": "2024-01-15T10:30:00Z",
        }

        mock_request = MagicMock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {
            "X-Chargify-Webhook-Id": "webhook_123",
        }
        mock_request.POST.dict.return_value = form_data

        # Should handle large payloads gracefully
        result = provider.parse_webhook(mock_request)
        assert result["metadata"]["memo"] == large_memo


class TestChargifySourcePluginIntegration:
    """Integration tests for Chargify provider."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    def test_customer_data_extraction(self, provider):
        """Test customer data extraction from webhook data"""
        webhook_data = {
            "payload[subscription][customer][id]": "cust_123",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][first_name]": "John",
            "payload[subscription][customer][last_name]": "Doe",
            "payload[subscription][customer][organization]": "Acme Corp",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[subscription][total_revenue_in_cents]": "299900",
            "created_at": "2024-01-15T10:30:00Z",
        }

        provider._current_webhook_data = webhook_data
        customer_data = provider.get_customer_data("cust_123")

        assert customer_data["email"] == "test@example.com"
        assert customer_data["company_name"] == "Acme Corp"
        assert customer_data["first_name"] == "John"
        assert customer_data["last_name"] == "Doe"
        assert customer_data["plan_name"] == "Premium Plan"
        # Lifetime spend surfaces under total_spent - the key the LTV/VIP
        # detectors and the notification builder read
        assert customer_data["total_spent"] == 2999.0

    def test_event_type_mapping(self, provider):
        """Test event type mapping functionality"""
        test_cases = [
            ("payment_success", "payment_success"),
            ("payment_failure", "payment_failure"),
            ("renewal_success", "payment_success"),
            ("renewal_failure", "payment_failure"),
            ("subscription_state_change", "subscription_updated"),
        ]

        for input_event, expected_output in test_cases:
            mapped_event = provider.EVENT_TYPE_MAPPING.get(input_event)
            assert mapped_event == expected_output

    def test_webhook_processing_end_to_end(self, provider):
        """Test complete webhook processing flow"""
        # This would test the entire flow from validation to data extraction
        # Implementation depends on your specific webhook router setup
        pass


class TestChargifyEventRouting:
    """Test that every advertised Chargify event type is routable.

    Previously ~15 advertised events fell through to an InvalidDataError,
    which the router turned into a 400 and Chargify retried forever.
    Now every event either parses into an internal event or is logged
    and skipped (None, acknowledged with a 200 by the router).
    """

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    @pytest.fixture
    def subscription_form_data(self) -> dict[str, str]:
        """Subscription-scoped form data shared by lifecycle events."""
        return {
            "payload[subscription][id]": "sub_12345",
            "payload[subscription][state]": "active",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][organization]": "Acme Corp",
            "payload[subscription][product][name]": "Premium Plan",
            "created_at": "2024-01-15T10:30:00Z",
        }

    @pytest.mark.parametrize(
        ("chargify_event", "internal_type"),
        [
            ("subscription_created", "subscription_created"),
            ("subscription_updated", "subscription_updated"),
            ("subscription_cancelled", "subscription_canceled"),
            ("subscription_expired", "subscription_canceled"),
        ],
    )
    def test_subscription_lifecycle_events_processed(
        self,
        provider: ChargifySourcePlugin,
        subscription_form_data: dict[str, str],
        chargify_event: str,
        internal_type: str,
    ) -> None:
        """Test that subscription lifecycle events parse into internal events.

        Args:
            provider: Chargify plugin under test.
            subscription_form_data: Base subscription payload.
            chargify_event: Chargify event name posted to the webhook.
            internal_type: Expected internal event type.
        """
        form_data = {"event": chargify_event, **subscription_form_data}
        result = provider.parse_webhook(_mock_chargify_request(form_data))

        assert result is not None
        assert result["type"] == internal_type
        assert result["provider"] == "chargify"
        assert result["customer_id"] == "cust_456"
        assert result["metadata"]["subscription_id"] == "sub_12345"
        assert result["metadata"]["plan_name"] == "Premium Plan"
        assert result["metadata"]["chargify_event"] == chargify_event
        assert result["event_id"] == "webhook_123"

    @pytest.mark.parametrize(
        "chargify_event",
        sorted(ChargifySourcePlugin.ACKNOWLEDGED_EVENT_TYPES),
    )
    def test_acknowledged_events_logged_and_skipped(
        self, provider: ChargifySourcePlugin, chargify_event: str
    ) -> None:
        """Test that known-but-unprocessed events return None (200), not 400.

        Args:
            provider: Chargify plugin under test.
            chargify_event: Chargify event name posted to the webhook.
        """
        form_data = {
            "event": chargify_event,
            "payload[subscription][customer][id]": "cust_456",
            "created_at": "2024-01-15T10:30:00Z",
        }

        assert provider.parse_webhook(_mock_chargify_request(form_data)) is None

    def test_every_advertised_event_is_routable(
        self,
        provider: ChargifySourcePlugin,
        subscription_form_data: dict[str, str],
    ) -> None:
        """Test that no EVENT_TYPE_MAPPING key raises an unsupported error.

        Every emitted event type must also be one the event processor
        accepts: an unknown type raises ValueError downstream, which the
        router turns into a 5xx and Chargify retries forever.

        Args:
            provider: Chargify plugin under test.
            subscription_form_data: Base subscription payload.
        """
        from webhooks.services.event_processor import EventProcessor

        # Exercise a cancellation-like state as well as the default path
        for state in ("active", "canceled", "expired"):
            for chargify_event in provider.EVENT_TYPE_MAPPING:
                form_data = {
                    "event": chargify_event,
                    **subscription_form_data,
                    "payload[subscription][state]": state,
                    "payload[transaction][amount_in_cents]": "2999",
                }
                result = provider.parse_webhook(_mock_chargify_request(form_data))
                assert result is not None, f"Event not routed: {chargify_event}"
                assert result["provider"] == "chargify", (
                    f"Missing provider field for: {chargify_event}"
                )
                assert result["type"] in EventProcessor.VALID_EVENT_TYPES, (
                    f"{chargify_event} (state={state}) emitted "
                    f"unsupported type: {result['type']}"
                )

    def test_mapping_values_are_valid_event_types(self) -> None:
        """Test that every EVENT_TYPE_MAPPING value is a supported type."""
        from webhooks.services.event_processor import EventProcessor

        for (
            chargify_event,
            internal_type,
        ) in ChargifySourcePlugin.EVENT_TYPE_MAPPING.items():
            assert internal_type in EventProcessor.VALID_EVENT_TYPES, (
                f"{chargify_event} maps to unsupported type: {internal_type}"
            )

    @pytest.mark.parametrize(
        ("state", "expected_type"),
        [
            ("active", "subscription_updated"),
            ("past_due", "subscription_updated"),
            ("canceled", "subscription_canceled"),
            ("cancelled", "subscription_canceled"),
            ("expired", "subscription_canceled"),
        ],
    )
    def test_state_change_type_normalization(
        self,
        provider: ChargifySourcePlugin,
        subscription_form_data: dict[str, str],
        state: str,
        expected_type: str,
    ) -> None:
        """Test that state changes normalize to supported internal types.

        Args:
            provider: Chargify plugin under test.
            subscription_form_data: Base subscription payload.
            state: New Chargify subscription state.
            expected_type: Expected normalized internal event type.
        """
        form_data = {
            "event": "subscription_state_change",
            **subscription_form_data,
            "payload[subscription][state]": state,
        }

        result = provider.parse_webhook(_mock_chargify_request(form_data))

        assert result is not None
        assert result["type"] == expected_type
        # The raw Chargify state is preserved
        assert result["status"] == state
        assert result["metadata"]["new_state"] == state


class TestChargifySparsePayloads:
    """Test parsers against payloads with legitimately missing fields."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    def test_payment_failure_for_one_off_charge(
        self, provider: ChargifySourcePlugin
    ) -> None:
        """Test payment_failure without subscription id or product name.

        A one-off charge has no subscription, so those fields must
        default instead of raising KeyError (which became a 500 and an
        infinite Chargify retry loop).
        """
        form_data = {
            "event": "payment_failure",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[transaction][id]": "txn_789",
            "payload[transaction][amount_in_cents]": "2999",
            "payload[transaction][failure_message]": "Card declined",
            "created_at": "2024-01-15T10:30:00Z",
        }

        result = provider.parse_webhook(_mock_chargify_request(form_data))

        assert result is not None
        assert result["type"] == "payment_failure"
        assert result["amount"] == 29.99
        assert result["metadata"]["subscription_id"] == ""
        assert result["metadata"]["plan_name"] == ""
        assert result["metadata"]["failure_reason"] == "Card declined"

    def test_payment_failure_invalid_amount_is_clean_error(
        self, provider: ChargifySourcePlugin
    ) -> None:
        """Test that a malformed amount raises InvalidDataError, not ValueError."""
        form_data = {
            "event": "payment_failure",
            "payload[subscription][customer][id]": "cust_456",
            "payload[transaction][amount_in_cents]": "not_a_number",
            "created_at": "2024-01-15T10:30:00Z",
        }

        with pytest.raises(InvalidDataError, match="Invalid amount format"):
            provider.parse_webhook(_mock_chargify_request(form_data))

    def test_billing_date_change_without_product_name(
        self, provider: ChargifySourcePlugin
    ) -> None:
        """Test subscription_billing_date_change without a product name.

        Billing date changes legitimately omit the product name; the
        parser must default it rather than raising KeyError.
        """
        form_data = {
            "event": "subscription_billing_date_change",
            "payload[subscription][id]": "sub_12345",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "created_at": "2024-01-15T10:30:00Z",
        }

        result = provider.parse_webhook(_mock_chargify_request(form_data))

        assert result is not None
        assert result["type"] == "subscription_updated"
        assert result["status"] == "unknown"
        assert result["provider"] == "chargify"
        assert result["metadata"]["plan_name"] == ""
        assert result["metadata"]["subscription_id"] == "sub_12345"
        assert result["metadata"]["chargify_event"] == (
            "subscription_billing_date_change"
        )


class TestChargifyAmountParsing:
    """Test the shared _parse_amount_cents helper."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    def test_valid_amount_returns_decimal_dollars(
        self, provider: ChargifySourcePlugin
    ) -> None:
        """Test that cents are converted to an exact Decimal dollar amount."""
        assert provider._parse_amount_cents("2999", "USD") == Decimal("29.99")

    def test_zero_decimal_currency_amount_not_divided(
        self, provider: ChargifySourcePlugin
    ) -> None:
        """Test that zero-decimal currency amounts stay in whole units."""
        assert provider._parse_amount_cents("1000", "JPY") == Decimal("1000")

    @pytest.mark.parametrize("amount", [None, ""])
    def test_missing_amount_rejected(
        self, provider: ChargifySourcePlugin, amount: Any
    ) -> None:
        """Test that missing amounts raise InvalidDataError.

        Args:
            provider: Chargify plugin under test.
            amount: Missing amount value.
        """
        with pytest.raises(InvalidDataError, match="Missing amount"):
            provider._parse_amount_cents(amount, "USD")

    def test_invalid_amount_rejected(self, provider: ChargifySourcePlugin) -> None:
        """Test that non-numeric amounts raise InvalidDataError."""
        with pytest.raises(InvalidDataError, match="Invalid amount format"):
            provider._parse_amount_cents("12.3.4", "USD")


@pytest.mark.django_db
class TestChargifySkippedEventRouterResponse:
    """Router-level test for acknowledged-but-skipped Chargify events."""

    def test_acknowledged_event_returns_200_with_neutral_message(
        self, client: Any
    ) -> None:
        """Test that a skipped event is acknowledged with a 200 and an
        accurate message (not the misleading test-ping wording).
        """
        from urllib.parse import urlencode

        from core.models import Integration, Workspace

        workspace = Workspace.objects.create(
            name="Test Workspace", shop_domain="test.myshopify.com"
        )
        Integration.objects.create(
            workspace=workspace,
            integration_type="chargify",
            webhook_secret="test-chargify-secret",
            is_active=True,
        )

        with patch(
            "plugins.sources.chargify.ChargifySourcePlugin.validate_webhook",
            return_value=True,
        ):
            response = client.post(
                f"/webhook/customer/{workspace.uuid}/chargify/",
                data=urlencode(
                    {
                        "event": "invoice_paid",
                        "payload[subscription][customer][id]": "cust_1",
                        "created_at": "2024-01-15T10:30:00Z",
                    }
                ),
                content_type="application/x-www-form-urlencoded",
                HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_skip_1",
            )

        assert response.status_code == 200
        assert response.json()["message"] == (
            "Webhook received (no notification required)"
        )

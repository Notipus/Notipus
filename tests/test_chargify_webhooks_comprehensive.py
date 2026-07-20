"""Comprehensive tests for Chargify webhook implementation.

This module tests webhook signature validation, data parsing,
deduplication, timestamp handling, and error scenarios for the
Chargify provider.
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory
from plugins.sources.base import InvalidDataError
from plugins.sources.chargify import ChargifySourcePlugin


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

        assert result["type"] == "subscription_state_change"
        assert result["status"] == "canceled"
        assert result["metadata"]["previous_state"] == "active"
        assert result["metadata"]["cancel_at_period_end"] is True

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

    def test_unsupported_event_type_rejected(self, provider, request_factory):
        """Test rejection of unsupported event types"""
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

        with pytest.raises(InvalidDataError, match="Unsupported event type"):
            provider.parse_webhook(mock_request)


class TestChargifyWebhookDeduplication:
    """Test Chargify webhook deduplication logic."""

    @pytest.fixture
    def provider(self) -> ChargifySourcePlugin:
        """Create a Chargify provider with short dedup window for testing."""

        # Create a custom provider class for testing with shorter dedup window
        class TestChargifySourcePlugin(ChargifySourcePlugin):
            _DEDUP_WINDOW_SECONDS = 60  # 1 minute for testing

        return TestChargifySourcePlugin(webhook_secret="test_secret")

    def test_webhook_deduplication_prevents_duplicates(self, provider):
        """Test that duplicate webhook IDs are rejected"""
        webhook_id = "webhook_12345"

        # First webhook should be allowed
        assert not provider._check_webhook_duplicate(webhook_id)

        # Second webhook with same ID should be rejected
        assert provider._check_webhook_duplicate(webhook_id)

    def test_webhook_deduplication_allows_different_webhook_ids(self, provider):
        """Test that webhooks with different IDs are allowed"""
        assert not provider._check_webhook_duplicate("webhook_123")
        assert not provider._check_webhook_duplicate("webhook_456")

    def test_webhook_deduplication_cache_cleanup(self, provider):
        """Test that old webhook entries are cleaned up"""

        # Create a provider with very short dedup window for this test
        class QuickTestProvider(ChargifySourcePlugin):
            _DEDUP_WINDOW_SECONDS = 1  # 1 second for quick testing

        quick_provider = QuickTestProvider(webhook_secret="test_secret")

        webhook_id = "webhook_12345"

        # Process webhook
        quick_provider._check_webhook_duplicate(webhook_id)

        # Wait for window to expire
        time.sleep(2)

        # Should be allowed again after window expires
        assert not quick_provider._check_webhook_duplicate(webhook_id)

    def test_webhook_cache_size_limit(self, provider):
        """Test that webhook cache respects size limits"""
        provider._CACHE_MAX_SIZE = 5

        # Fill cache beyond limit
        for i in range(10):
            provider._check_webhook_duplicate(f"webhook_{i}")

        # Cache should not exceed max size
        assert len(provider._webhook_cache) <= provider._CACHE_MAX_SIZE

    def test_webhook_duplicate_with_empty_id(self, provider):
        """Test handling of empty webhook ID"""
        # Should return False and log warning for empty webhook ID
        assert not provider._check_webhook_duplicate("")
        assert not provider._check_webhook_duplicate(None)


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
        assert customer_data["total_revenue"] == 2999.0

    def test_event_type_mapping(self, provider):
        """Test event type mapping functionality"""
        test_cases = [
            ("payment_success", "payment_success"),
            ("payment_failure", "payment_failure"),
            ("renewal_success", "payment_success"),
            ("renewal_failure", "payment_failure"),
            ("subscription_state_change", "subscription_state_change"),
        ]

        for input_event, expected_output in test_cases:
            mapped_event = provider.EVENT_TYPE_MAPPING.get(input_event)
            assert mapped_event == expected_output

    def test_webhook_processing_end_to_end(self, provider):
        """Test complete webhook processing flow"""
        # This would test the entire flow from validation to data extraction
        # Implementation depends on your specific webhook router setup
        pass

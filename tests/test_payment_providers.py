"""Tests for payment provider webhook parsing and validation.

This module tests Chargify, Shopify, and Stripe webhook handling
including signature validation, data parsing, and deduplication.
"""

import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from plugins.sources.base import BaseSourcePlugin, InvalidDataError
from plugins.sources.chargify import ChargifySourcePlugin
from plugins.sources.shopify import ShopifySourcePlugin
from webhooks.services.event_processor import EventProcessor


def test_payment_provider_interface() -> None:
    """Test that payment providers implement the required interface.

    Verifies all providers are instances of BaseSourcePlugin.
    """
    providers: list[BaseSourcePlugin] = [
        ChargifySourcePlugin(webhook_secret="test_secret"),
        ShopifySourcePlugin(webhook_secret="test_secret"),
    ]

    for provider in providers:
        assert isinstance(provider, BaseSourcePlugin)


def test_chargify_payment_failure_parsing() -> None:
    """Verify Chargify payment failure webhook parsing works correctly.

    Tests that payment failure events are correctly parsed with
    customer data, amount, and metadata.
    """
    provider = ChargifySourcePlugin(webhook_secret="test_secret")

    # Create a mock request (analogous to Flask request)
    mock_request = MagicMock()
    mock_request.content_type = "application/x-www-form-urlencoded"
    mock_request.form = MagicMock()
    mock_request.headers = {
        "X-Chargify-Webhook-Id": "webhook_123",
        "X-Chargify-Webhook-Signature-Hmac-Sha-256": "test_signature",
    }
    mock_request.POST.dict.return_value = {
        "event": "payment_failure",
        "payload[subscription][id]": "sub_12345",
        "payload[subscription][state]": "past_due",
        "payload[subscription][customer][id]": "cust_456",
        "payload[subscription][customer][email]": "test@example.com",
        "payload[subscription][customer][first_name]": "Test",
        "payload[subscription][customer][last_name]": "User",
        "payload[subscription][customer][organization]": "Test Company",
        "payload[subscription][product][id]": "prod_789",
        "payload[subscription][product][name]": "Enterprise Plan",
        "payload[subscription][product][handle]": "enterprise",
        "payload[transaction][id]": "tr_123",
        "payload[transaction][amount_in_cents]": "2999",
        "payload[transaction][type]": "payment",
        "payload[transaction][memo]": "Payment failed: Card declined",
        "payload[transaction][failure_message]": "Card was declined",
        "created_at": "2024-03-15T10:00:00Z",
    }

    event = provider.parse_webhook(mock_request)
    assert event is not None
    assert event["type"] == "payment_failure"
    assert event["customer_id"] == "cust_456"
    assert event["amount"] == 29.99
    assert event["status"] == "failed"
    assert event["metadata"]["failure_reason"] == "Card was declined"
    assert event["metadata"]["subscription_id"] == "sub_12345"
    assert event["customer_data"]["company_name"] == "Test Company"
    assert event["customer_data"]["plan_name"] == "Enterprise Plan"


def test_shopify_order_parsing() -> None:
    """Verify Shopify order webhook parsing works correctly.

    Tests that orders/paid events are correctly parsed with
    customer, order, and line item data.
    """
    provider = ShopifySourcePlugin(webhook_secret="test_secret")

    # Create a mock request
    mock_request = MagicMock()
    mock_request.content_type = "application/json"
    mock_request.headers = {
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Shop-Domain": "test.myshopify.com",
        "X-Shopify-Hmac-SHA256": "test_signature",
        "X-Shopify-Order-Id": "123456789",
        "X-Shopify-Api-Version": "2024-01",
    }
    shopify_data: dict[str, Any] = {
        "id": 123456789,
        "order_number": 1001,
        "customer": {
            "id": 456,
            "email": "test@example.com",
            "first_name": "Test",
            "last_name": "User",
            "company": "Test Company",
            "orders_count": 5,
            "total_spent": "299.95",
            "note": "Enterprise customer",
            "tags": ["enterprise", "priority"],
            "default_address": {
                "company": "Test Company",
                "country": "United States",
                "country_code": "US",
            },
            "metafields": [
                {
                    "key": "team_size",
                    "value": "25",
                    "namespace": "customer",
                },
                {
                    "key": "plan_type",
                    "value": "enterprise_annual",
                    "namespace": "subscription",
                },
            ],
        },
        "total_price": "29.99",
        "subtotal_price": "24.99",
        "total_tax": "5.00",
        "currency": "USD",
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "created_at": "2024-03-15T10:00:00Z",
        "updated_at": "2024-03-15T10:05:00Z",
        "line_items": [
            {
                "id": 789,
                "title": "Enterprise Plan",
                "quantity": 1,
                "price": "29.99",
                "sku": "ENT-PLAN-1",
                "properties": [
                    {"name": "team_size", "value": "25"},
                    {"name": "plan_type", "value": "annual"},
                ],
            }
        ],
    }

    mock_request.get_json.return_value = shopify_data
    # Ensure request.data is JSON in byte format
    mock_request.data = json.dumps(shopify_data).encode("utf-8")

    event = provider.parse_webhook(mock_request)
    assert event is not None
    assert event["type"] == "payment_success"
    assert event["customer_id"] == "456"
    assert event["amount"] == Decimal("29.99")
    assert event["status"] == "success"
    assert event["metadata"]["order_number"] == 1001
    assert event["metadata"]["financial_status"] == "paid"
    assert event["metadata"]["fulfillment_status"] == "fulfilled"


def test_chargify_webhook_validation() -> None:
    """Verify Chargify webhook signature validation.

    Tests HMAC signature validation for Chargify webhooks.
    """
    provider = ChargifySourcePlugin(webhook_secret="test_secret")

    # Create a mock request with headers
    mock_request = MagicMock()
    mock_request.headers = {
        "X-Chargify-Webhook-Signature-Hmac-Sha-256": "1234567890abcdef",
        "X-Chargify-Webhook-Id": "webhook_123",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Chargify Webhooks",
    }
    # Provide a valid request body
    body = b"payload[event]=payment_failure&payload[subscription][id]=sub_12345"
    mock_request.get_data.return_value = body  # Bytes
    mock_request.body = body  # For compatibility with validate_webhook method

    # Patch hmac.compare_digest
    with patch("hmac.compare_digest", return_value=True):
        assert provider.validate_webhook(mock_request) is True


def test_shopify_webhook_validation() -> None:
    """Verify Shopify webhook signature validation.

    Tests HMAC-SHA256 signature validation and required headers.
    """
    provider = ShopifySourcePlugin("test_secret")

    # Create a mock request with a valid signature
    mock_request = Mock()
    mock_request.headers = {
        "X-Shopify-Hmac-SHA256": "crxL3PMfBMvgMYyppPUPjAooPtjS7fh0dOiGPTYm3QU=",
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Shop-Domain": "test.myshopify.com",
        "X-Shopify-Test": "true",
    }
    mock_request.content_type = "application/json"
    mock_request.body = b'{"test": "data"}'  # Explicitly setting the body attribute

    # Test valid signature
    with patch("hmac.new") as mock_hmac:
        mock_hmac.return_value.digest.return_value = b"test_digest"
        mock_b64encode = Mock(
            return_value=b"crxL3PMfBMvgMYyppPUPjAooPtjS7fh0dOiGPTYm3QU="
        )
        with patch("base64.b64encode", mock_b64encode):
            assert provider.validate_webhook(mock_request)

    # Test missing signature
    mock_request.headers.pop("X-Shopify-Hmac-SHA256")
    assert not provider.validate_webhook(mock_request)

    # Test missing topic
    mock_request.headers["X-Shopify-Hmac-SHA256"] = "test_signature"
    mock_request.headers.pop("X-Shopify-Topic")
    assert not provider.validate_webhook(mock_request)

    # Test missing shop domain
    mock_request.headers["X-Shopify-Topic"] = "orders/paid"
    mock_request.headers.pop("X-Shopify-Shop-Domain")
    assert not provider.validate_webhook(mock_request)

    # Test invalid signature
    mock_request.headers.update(
        {
            "X-Shopify-Hmac-SHA256": "invalid_signature",
            "X-Shopify-Topic": "orders/paid",
            "X-Shopify-Shop-Domain": "test.myshopify.com",
        }
    )
    assert not provider.validate_webhook(mock_request)


@pytest.mark.usefixtures("mock_webhook_validation")
def test_shopify_test_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify handling of Shopify test webhooks.

    Test webhooks should be ignored and return None.
    """
    provider = ShopifySourcePlugin("test_secret")

    # Create a mock request
    mock_request = Mock()
    mock_request.headers = {
        "X-Shopify-Hmac-SHA256": "test_signature",
        "X-Shopify-Topic": "test",
        "X-Shopify-Shop-Domain": "test.myshopify.com",
    }
    mock_request.content_type = "application/json"
    mock_request.data = b'{"test": true}'
    mock_request.get_json.return_value = {"test": True}

    # Test webhooks should return None (ignored)
    event = provider.parse_webhook(mock_request)
    assert event is None


def test_shopify_invalid_webhook_data() -> None:
    """Verify handling of invalid Shopify webhook data.

    Tests various invalid data scenarios including wrong content type,
    empty data, missing customer ID, and invalid amount.
    """
    provider = ShopifySourcePlugin("test_secret")

    # Create a mock request
    mock_request = Mock()
    mock_request.headers = {
        "X-Shopify-Hmac-SHA256": "test_signature",
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Shop-Domain": "test.myshopify.com",
    }
    mock_request.content_type = "application/json"

    # Test invalid content type
    mock_request.content_type = "application/x-www-form-urlencoded"
    with pytest.raises(InvalidDataError, match="Invalid content type"):
        provider.parse_webhook(mock_request)

    # Test empty data
    mock_request.content_type = "application/json"
    mock_request.data = b"{}"
    mock_request.get_json.return_value = {}
    with pytest.raises(InvalidDataError, match="Missing required fields"):
        provider.parse_webhook(mock_request)

    # Test missing customer_id
    mock_request.get_json.return_value = {"test": "data"}
    with pytest.raises(InvalidDataError, match="Missing required fields"):
        provider.parse_webhook(mock_request)

    # Test invalid amount format
    mock_request.get_json.return_value = {"id": 123, "total_price": "invalid"}
    with pytest.raises(InvalidDataError, match="Missing required fields"):
        provider.parse_webhook(mock_request)


def test_invalid_webhook_data() -> None:
    """Verify handling of invalid webhook data for Chargify.

    Tests that empty form data raises InvalidDataError.
    """
    chargify = ChargifySourcePlugin(webhook_secret="test_secret")

    # Test Chargify with invalid data
    mock_chargify_request = MagicMock()
    mock_chargify_request.content_type = "application/x-www-form-urlencoded"
    mock_chargify_request.form = MagicMock()
    mock_chargify_request.POST.dict.return_value = {}
    mock_chargify_request.headers = {
        "X-Chargify-Webhook-Id": "webhook_123",
        "X-Chargify-Webhook-Signature-Hmac-Sha-256": "test_signature",
    }

    with pytest.raises(InvalidDataError, match="Missing required fields"):
        chargify.parse_webhook(mock_chargify_request)


def test_chargify_subscription_state_change() -> None:
    """Verify Chargify subscription state change webhook parsing.

    Tests that subscription cancellation events are correctly parsed.
    """
    provider = ChargifySourcePlugin(webhook_secret="test_secret")

    mock_request = MagicMock()
    mock_request.content_type = "application/x-www-form-urlencoded"
    mock_request.form = MagicMock()
    mock_request.headers = {
        "X-Chargify-Webhook-Id": "webhook_123",
        "X-Chargify-Webhook-Signature-Hmac-Sha-256": "test_signature",
    }
    mock_request.POST.dict.return_value = {
        "event": "subscription_state_change",
        "payload[subscription][id]": "sub_12345",
        "payload[subscription][state]": "canceled",
        "payload[subscription][cancel_at_end_of_period]": "true",
        "payload[subscription][customer][id]": "cust_456",
        "payload[subscription][customer][email]": "test@example.com",
        "payload[subscription][customer][organization]": "Test Company",
        "payload[subscription][product][name]": "Enterprise Plan",
        "payload[subscription][product][handle]": "enterprise",
        "payload[subscription][total_revenue_in_cents]": "299900",
        "created_at": "2024-03-15T10:00:00Z",
    }

    event = provider.parse_webhook(mock_request)
    assert event is not None
    assert event["type"] == "subscription_state_change"
    assert event["customer_id"] == "cust_456"
    assert event["status"] == "canceled"
    assert event["metadata"]["subscription_id"] == "sub_12345"
    assert event["metadata"]["cancel_at_period_end"]
    assert event["customer_data"]["company_name"] == "Test Company"
    assert event["customer_data"]["plan_name"] == "Enterprise Plan"


def test_shopify_customer_data_update() -> None:
    """Verify Shopify customers/update webhook parsing.

    Tests that customer update events include company and profile data.
    """
    provider = ShopifySourcePlugin(webhook_secret="test_secret")

    mock_request = Mock()
    mock_request.content_type = "application/json"
    mock_request.headers = {
        "X-Shopify-Topic": "customers/update",
        "X-Shopify-Shop-Domain": "test.myshopify.com",
        "X-Shopify-Hmac-SHA256": "test_signature",
    }
    mock_data: dict[str, Any] = {
        "id": 456,
        "email": "test@example.com",
        "accepts_marketing": True,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-03-15T10:00:00Z",
        "first_name": "Test",
        "last_name": "User",
        "company": "Updated Company Name",
        "orders_count": 10,
        "total_spent": "599.90",
        "note": "Enterprise customer, upgraded plan",
        "tags": ["enterprise", "priority", "annual"],
        "addresses": [
            {
                "id": 1,
                "company": "Updated Company Name",
                "country": "United States",
                "country_code": "US",
            }
        ],
        "metafields": [
            {
                "key": "team_size",
                "value": "50",
                "namespace": "customer",
            },
            {
                "key": "plan_type",
                "value": "enterprise_annual",
                "namespace": "subscription",
            },
        ],
    }
    mock_request.get_json.return_value = mock_data
    mock_request.data = json.dumps(mock_data).encode("utf-8")

    event = provider.parse_webhook(mock_request)
    assert event is not None
    assert event["type"] == "customer_updated"
    assert event["customer_id"] == "456"
    assert event["customer_data"]["company"] == "Updated Company Name"


def test_chargify_webhook_deduplication() -> None:
    """Verify Chargify webhook dedup key handling.

    The plugin surfaces the webhook id as ``event_id`` so the router can
    deduplicate via the event consolidation service, and rejects webhooks
    without an id (no stable dedup key).
    """
    provider = ChargifySourcePlugin("")

    # Create a mock request with payment_success event
    mock_request = MagicMock()
    mock_request.content_type = "application/x-www-form-urlencoded"
    mock_request.form = MagicMock()
    mock_request.headers = {
        "X-Chargify-Webhook-Id": "test_webhook_1",
    }
    form_data: dict[str, str] = {
        "event": "payment_success",
        "id": "12345",
        "payload[subscription][id]": "sub_789",
        "payload[subscription][customer][id]": "cust_123",
        "payload[subscription][customer][email]": "test@example.com",
        "payload[subscription][customer][first_name]": "Test",
        "payload[subscription][customer][last_name]": "User",
        "payload[subscription][customer][organization]": "Test Co",
        "payload[subscription][product][name]": "Enterprise Plan",
        "payload[transaction][amount_in_cents]": "10000",
        "created_at": "2024-03-15T10:00:00Z",
    }
    mock_request.POST.dict.return_value = form_data
    # Event parses and surfaces the webhook id as the dedup key
    event1 = provider.parse_webhook(mock_request)
    assert event1 is not None
    assert event1["type"] == "payment_success"
    assert event1["customer_id"] == "cust_123"
    assert event1["event_id"] == "test_webhook_1"

    # A different webhook ID produces a different dedup key
    mock_request.headers["X-Chargify-Webhook-Id"] = "different_webhook_id"
    event2 = provider.parse_webhook(mock_request)
    assert event2 is not None
    assert event2["event_id"] == "different_webhook_id"

    # Missing webhook ID must not bypass dedup - it is a validation error
    mock_request.headers = {}
    with pytest.raises(InvalidDataError, match="X-Chargify-Webhook-Id"):
        provider.parse_webhook(mock_request)


def test_event_processor_notification_formatting() -> None:
    """Verify EventProcessor formats notifications correctly for different events.

    Tests both payment success and failure event formatting using RichNotification.
    """
    from webhooks.models.rich_notification import (
        NotificationSeverity,
        NotificationType,
        RichNotification,
    )

    processor = EventProcessor()

    # Test successful payment event
    event_data: dict[str, Any] = {
        "type": "payment_success",
        "customer_id": "cust_123",
        "amount": 29.99,
        "currency": "USD",
        "status": "success",
        "provider": "stripe",
        "external_id": "evt_123",
        "metadata": {
            "subscription_id": "sub_123",
            "plan_name": "Enterprise",
        },
    }
    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
        "first_name": "Test",
        "last_name": "User",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    # Headlines are event-focused (no company name)
    assert "$29.99" in notification.headline
    assert "received" in notification.headline.lower()
    assert notification.type == NotificationType.PAYMENT_SUCCESS
    assert notification.severity == NotificationSeverity.SUCCESS

    # Test payment failure event
    event_data["type"] = "payment_failure"
    event_data["status"] = "failed"
    event_data["metadata"]["failure_reason"] = "card_declined"

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    # Headlines are event-focused (no company name)
    assert "failed" in notification.headline.lower()
    assert notification.type == NotificationType.PAYMENT_FAILURE
    assert notification.severity == NotificationSeverity.ERROR


def test_chargify_memo_parsing() -> None:
    """Verify Chargify memo field parsing for Shopify order references.

    Tests various memo formats to extract Shopify order IDs.
    """
    provider = ChargifySourcePlugin(webhook_secret="test_secret")

    # Test different memo formats
    test_cases: list[tuple[str, str | None]] = [
        (
            "Wire payment received for $233.76 24th December '24\n"
            "$228.90 allocated to Shopify Order 2067",
            "2067",
        ),
        ("Payment for Shopify Order 1234", "1234"),
        ("$500 allocated to order 5678", "5678"),
        ("Regular payment - no order reference", None),
        (
            "Multiple orders: allocated to 1111 and Shopify Order 2222",
            "2222",  # Prioritize explicit Shopify Order mention
        ),
        (
            "Order 3333 and Shopify Order 4444",
            "4444",  # Prioritize explicit Shopify Order mention
        ),
        (
            "Just Order 5555",
            "5555",  # Match general order reference pattern
        ),
        (
            "",  # Empty memo
            None,
        ),
    ]

    for memo, expected_ref in test_cases:
        ref = provider._parse_shopify_order_ref(memo)
        assert ref == expected_ref, f"Failed to parse memo: {memo}"


def test_chargify_payment_success_with_shopify_ref() -> None:
    """Verify payment_success webhook includes Shopify order reference when present.

    Tests that memo parsing extracts Shopify order references.
    """
    provider = ChargifySourcePlugin(webhook_secret="test_secret")

    # Create a mock request
    mock_request = MagicMock()
    mock_request.content_type = "application/x-www-form-urlencoded"
    mock_request.form = MagicMock()
    mock_request.headers = {
        "X-Chargify-Webhook-Id": "webhook_123",
        "X-Chargify-Webhook-Signature-Hmac-Sha-256": "test_signature",
    }
    mock_request.POST.dict.return_value = {
        "event": "payment_success",
        "payload[subscription][id]": "sub_12345",
        "payload[subscription][customer][id]": "cust_456",
        "payload[subscription][customer][email]": "test@example.com",
        "payload[subscription][customer][first_name]": "Test",
        "payload[subscription][customer][last_name]": "User",
        "payload[subscription][customer][organization]": "Test Company",
        "payload[subscription][product][name]": "Enterprise Plan",
        "payload[transaction][id]": "tr_123",
        "payload[transaction][amount_in_cents]": "10000",
        "payload[transaction][memo]": (
            "Wire payment received for $100.00\nAllocated to Shopify Order 1234"
        ),
        "created_at": "2024-03-15T10:00:00Z",
    }

    event = provider.parse_webhook(mock_request)
    assert event is not None
    assert event["type"] == "payment_success"
    assert event["metadata"]["shopify_order_ref"] == "1234"
    assert "memo" in event["metadata"]  # Full memo should be preserved


def test_shopify_order_ref_matching() -> None:
    """Verify Shopify and Chargify events are correctly linked by order reference.

    Tests that cross-reference enrichment works without errors.
    """
    from webhooks.models.rich_notification import RichNotification

    processor = EventProcessor()

    # Test that the processor can handle events with cross-references
    # This is now handled internally through the _enrich_with_cross_references method
    chargify_event: dict[str, Any] = {
        "type": "payment_success",
        "provider": "chargify",
        "customer_id": "cust_123",
        "amount": 29.99,
        "currency": "USD",
        "external_id": "evt_123",
        "metadata": {
            "shopify_order_ref": "1234",
            "memo": "Payment for Shopify Order 1234",
        },
    }

    customer_data: dict[str, Any] = {
        "company": "Test Company",
        "email": "billing@test.com",
        "first_name": "Test",
        "last_name": "User",
    }

    # Test that enrichment works without errors
    notification = processor.build_rich_notification(chargify_event, customer_data)
    assert isinstance(notification, RichNotification)
    # Headlines are event-focused (no company name)
    assert "$29.99" in notification.headline

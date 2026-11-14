"""Tests for Shopify payload handling fixes.

Covers customers/* topics (the payload is the customer), refunds that
list failed transactions next to the successful one, and HMAC headers
holding non-ASCII characters.
"""

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any

import pytest
from django.http import HttpRequest
from django.test import Client, RequestFactory
from django.urls import reverse
from plugins.sources.shopify import ShopifySourcePlugin

SECRET = "shpss_payload_secret"


def signed_request(topic: str, payload: dict[str, Any]) -> HttpRequest:
    """Build a request signed the way Shopify signs deliveries.

    Args:
        topic: The Shopify webhook topic.
        payload: The webhook body.

    Returns:
        The signed request.
    """
    raw = json.dumps(payload).encode()
    digest = base64.b64encode(
        hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
    ).decode()
    return RequestFactory().post(
        "/webhook/",
        data=raw,
        content_type="application/json",
        HTTP_X_SHOPIFY_TOPIC=topic,
        HTTP_X_SHOPIFY_HMAC_SHA256=digest,
    )


class TestCustomerTopics:
    """customers/* payloads are the customer resource itself."""

    @pytest.mark.parametrize("topic", ["customers/create", "customers/update"])
    def test_customer_data_comes_from_top_level_payload(self, topic: str) -> None:
        """The person is read from the payload, not a nested customer key."""
        plugin = ShopifySourcePlugin(SECRET)
        request = signed_request(
            topic,
            {
                "id": 42,
                "email": "ann@example.com",
                "first_name": "Ann",
                "last_name": "Example",
                "orders_count": 3,
            },
        )
        assert plugin.validate_webhook(request)
        event = plugin.parse_webhook(request)
        assert event is not None

        customer = plugin.get_customer_data(event["customer_id"])

        assert event["customer_id"] == "42"
        assert customer["email"] == "ann@example.com"
        assert customer["first_name"] == "Ann"
        assert customer["last_name"] == "Example"
        assert customer["orders_count"] == 3

    def test_order_topics_still_read_nested_customer(self) -> None:
        """Order payloads keep reading data['customer']."""
        plugin = ShopifySourcePlugin(SECRET)
        request = signed_request(
            "orders/paid",
            {
                "id": 1,
                "email": "order-level@example.com",
                "total_price": "10.00",
                "currency": "USD",
                "customer": {"id": 7, "email": "buyer@example.com"},
            },
        )
        event = plugin.parse_webhook(request)
        assert event is not None
        assert plugin.get_customer_data("7")["email"] == "buyer@example.com"


class TestRefundAmount:
    """Only successful refund transactions count towards the amount."""

    def test_failed_and_pending_transactions_are_ignored(self) -> None:
        """A failed attempt followed by a successful one refunds once."""
        plugin = ShopifySourcePlugin(SECRET)
        event = plugin.build_event_from_payload(
            "refunds/create",
            {
                "id": 9,
                "order_id": 5,
                "transactions": [
                    {"amount": "50.00", "currency": "USD", "status": "failure"},
                    {"amount": "50.00", "currency": "USD", "status": "pending"},
                    {"amount": "50.00", "currency": "USD", "status": "error"},
                    {"amount": "50.00", "currency": "USD", "status": "success"},
                ],
            },
        )
        assert event is not None
        assert event["amount"] == Decimal("50.00")


class TestNonAsciiHmacHeader:
    """A non-ASCII signature header is rejected, never a 500."""

    def test_plugin_returns_false(self) -> None:
        """validate_webhook rejects instead of raising TypeError."""
        request = RequestFactory().post(
            "/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_X_SHOPIFY_TOPIC="orders/paid",
            HTTP_X_SHOPIFY_HMAC_SHA256="éabc",
        )
        assert ShopifySourcePlugin(SECRET).validate_webhook(request) is False

    @pytest.mark.django_db
    def test_compliance_endpoint_returns_401(
        self, client: Client, settings: Any
    ) -> None:
        """The compliance endpoint answers 401 for a non-ASCII header."""
        settings.SHOPIFY_CLIENT_SECRET = SECRET
        response = client.post(
            reverse("webhooks:shopify_shop_redact"),
            data=b"{}",
            content_type="application/json",
            HTTP_X_SHOPIFY_TOPIC="shop/redact",
            HTTP_X_SHOPIFY_HMAC_SHA256="éabc",
        )
        assert response.status_code == 401

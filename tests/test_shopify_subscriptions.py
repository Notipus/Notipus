"""Tests for Shopify subscription, billing attempt and refund parsing.

Payload shapes here mirror what Shopify actually sends, captured from
``shopify app webhook trigger`` against API version 2026-07.
"""

import json
from decimal import Decimal
from typing import Any

from django.test import RequestFactory
from plugins.sources.shopify import ShopifySourcePlugin

# A real subscription_contracts/create body, as delivered by Shopify.
CONTRACT_PAYLOAD: dict[str, Any] = {
    "admin_graphql_api_id": "gid://shopify/SubscriptionContract/402440842",
    "id": 402440842,
    "billing_policy": {
        "interval": "week",
        "interval_count": 4,
        "min_cycles": 1,
        "max_cycles": 2,
    },
    "currency_code": "CAD",
    "customer_id": 1,
    "admin_graphql_api_customer_id": "gid://shopify/Customer/1",
    "delivery_policy": {"interval": "week", "interval_count": 2},
    "status": "active",
    "admin_graphql_api_origin_order_id": "gid://shopify/Order/1",
    "origin_order_id": 1,
    "revision_id": "7232252373",
}


def parse(topic: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Parse a payload as if Shopify had delivered it.

    Args:
        topic: The Shopify webhook topic.
        payload: The webhook body.

    Returns:
        The parsed event dictionary.
    """
    request = RequestFactory().post(
        "/webhook/customer/x/shopify/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"x-shopify-topic": topic},
    )
    plugin = ShopifySourcePlugin(webhook_secret="unused")
    event = plugin.parse_webhook(request)
    assert event is not None
    return event


class TestSubscriptionContracts:
    """Parsing of SubscriptionContract payloads."""

    def test_contract_create_is_a_subscription_created_event(self) -> None:
        """A new contract maps to subscription_created."""
        event = parse("subscription_contracts/create", CONTRACT_PAYLOAD)
        assert event["type"] == "subscription_created"
        assert event["external_id"] == "402440842"
        assert event["currency"] == "CAD"

    def test_customer_comes_from_customer_id_not_the_contract_id(self) -> None:
        """The top-level id is the contract, never the customer.

        Using it would bucket every subscriber under their contract id
        and break per-customer consolidation.
        """
        event = parse("subscription_contracts/create", CONTRACT_PAYLOAD)
        assert event["customer_id"] == "1"
        assert event["customer_id"] != str(CONTRACT_PAYLOAD["id"])

    def test_billing_cadence_is_captured(self) -> None:
        """Interval details survive into metadata."""
        event = parse("subscription_contracts/create", CONTRACT_PAYLOAD)
        meta = event["metadata"]
        assert meta["billing_interval"] == "week"
        assert meta["billing_interval_count"] == 4
        assert meta["min_cycles"] == 1
        assert meta["max_cycles"] == 2
        assert meta["is_recurring"] is True

    def test_update_stays_an_update_while_active(self) -> None:
        """An active contract update is a plain update."""
        event = parse("subscription_contracts/update", CONTRACT_PAYLOAD)
        assert event["type"] == "subscription_updated"

    def test_cancelled_contract_is_promoted_to_cancellation(self) -> None:
        """Shopify has no cancel topic; status carries the news."""
        payload = {**CONTRACT_PAYLOAD, "status": "cancelled"}
        event = parse("subscription_contracts/update", payload)
        assert event["type"] == "subscription_canceled"

    def test_expired_contract_is_also_a_cancellation(self) -> None:
        """An expired contract has equally stopped billing."""
        payload = {**CONTRACT_PAYLOAD, "status": "expired"}
        event = parse("subscription_contracts/update", payload)
        assert event["type"] == "subscription_canceled"


class TestBillingAttempts:
    """Parsing of SubscriptionBillingAttempt payloads."""

    def test_successful_attempt_is_a_renewal(self) -> None:
        """A collected recurring charge is a renewal."""
        event = parse(
            "subscription_billing_attempts/success",
            {
                "id": 987,
                "subscription_contract_id": 402440842,
                "order_id": 555,
                "idempotency_key": "key-1",
                "origin_time": "2026-08-15T10:00:00-04:00",
            },
        )
        assert event["type"] == "subscription_renewed"
        assert event["status"] == "success"
        assert event["metadata"]["order_id"] == 555

    def test_failed_attempt_is_a_payment_failure_with_the_reason(self) -> None:
        """Dunning needs the error, not just the failure."""
        event = parse(
            "subscription_billing_attempts/failure",
            {
                "id": 988,
                "subscription_contract_id": 402440842,
                "error_code": "card_declined",
                "error_message": "The card was declined.",
            },
        )
        assert event["type"] == "payment_failure"
        assert event["status"] == "failed"
        assert event["metadata"]["error_code"] == "card_declined"
        assert event["metadata"]["error_message"] == "The card was declined."

    def test_attempt_groups_by_contract_when_no_customer_is_sent(self) -> None:
        """Billing attempts carry no customer at all.

        Falling back to the contract keeps retries for one subscription
        in a single consolidation bucket.
        """
        event = parse(
            "subscription_billing_attempts/failure",
            {"id": 989, "subscription_contract_id": 402440842},
        )
        assert event["customer_id"] == "contract_402440842"


class TestRefunds:
    """Parsing of Refund payloads."""

    def test_amount_is_summed_from_transactions(self) -> None:
        """A refund states no total; its transactions do."""
        event = parse(
            "refunds/create",
            {
                "id": 929361464,
                "order_id": 820982911946154508,
                "note": "Damaged in transit",
                "transactions": [
                    {"amount": "89.99", "currency": "USD"},
                    {"amount": "10.01", "currency": "USD"},
                ],
                "refund_line_items": [
                    {
                        "quantity": 1,
                        "line_item": {"name": "Aviator sunglasses", "sku": "SKU-1"},
                    }
                ],
            },
        )
        assert event["type"] == "refund_issued"
        assert event["amount"] == Decimal("100.00")
        assert event["currency"] == "USD"
        assert event["metadata"]["line_items"][0]["name"] == "Aviator sunglasses"

    def test_refund_without_transactions_is_zero_not_an_error(self) -> None:
        """A zero-value refund must not break the parse."""
        event = parse("refunds/create", {"id": 1, "order_id": 2, "transactions": []})
        assert event["amount"] == Decimal(0)

    def test_refund_identifies_by_order(self) -> None:
        """Refunds name no customer, so the order is the identity."""
        event = parse("refunds/create", {"id": 1, "order_id": 820982911946154508})
        assert event["customer_id"] == "order_820982911946154508"


class TestTopicCoverage:
    """The mapping and the subscribable categories must agree."""

    def test_every_mapped_topic_is_subscribable(self) -> None:
        """A parsed topic nobody can subscribe to is dead code.

        orders/refunded was mapped for months while no category offered
        it, so refunds could never arrive.
        """
        from core.views.integrations.shopify import SHOPIFY_WEBHOOK_TOPICS

        mapped = set(ShopifySourcePlugin.EVENT_TYPE_MAPPING) - {
            "test",
            # Not offered as a category: high volume, low signal.
            "checkouts/create",
            "customers/create",
        }
        assert mapped <= set(SHOPIFY_WEBHOOK_TOPICS)

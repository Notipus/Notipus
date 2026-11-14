"""Regression tests for notification building and rendering correctness.

Covers how events become RichNotifications (headlines, insights, badges)
and how destination plugins render them (Slack, Telegram).
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from plugins.destinations.slack import SlackDestinationPlugin
from plugins.destinations.telegram import TelegramDestinationPlugin
from webhooks.models.rich_notification import (
    NotificationSeverity,
    NotificationType,
    PaymentInfo,
    RichNotification,
)
from webhooks.services.database_lookup import DatabaseLookupService
from webhooks.services.event_processor import EventProcessor
from webhooks.services.insight_detector import InsightDetector
from webhooks.services.notification_builder import NotificationBuilder


def _build(event: dict[str, Any], customer: dict[str, Any]) -> Any:
    """Build a RichNotification with a fresh builder."""
    return NotificationBuilder().build(event, customer)


def _payment_failure_event() -> dict[str, Any]:
    """Return a Stripe payment failure event with a dashboard link."""
    return {
        "type": "payment_failure",
        "provider": "stripe",
        "amount": 49.0,
        "currency": "USD",
        "customer_id": "cus_123",
        "metadata": {"attempt_count": 2, "failure_reason": "Card declined"},
    }


class TestTelegramContactButton:
    """Telegram url buttons must only carry schemes Telegram accepts."""

    def test_mailto_button_dropped_and_email_in_text(self) -> None:
        """A payment failure keeps only http(s) buttons; email moves to text."""
        n = _build(_payment_failure_event(), {"email": "ann@example.com"})
        assert any(a.url.startswith("mailto:") for a in n.actions)

        result = TelegramDestinationPlugin().format(n)

        buttons = [b for row in result["reply_markup"]["inline_keyboard"] for b in row]
        assert buttons
        assert all(b["url"].startswith("https://") for b in buttons)
        assert "📧 Contact Customer: ann@example.com" in result["text"]

    def test_no_reply_markup_when_only_mailto(self) -> None:
        """When every action is mailto, no inline keyboard is sent at all."""
        event = _payment_failure_event()
        event.pop("customer_id")
        n = _build(event, {"email": "ann@example.com"})

        result = TelegramDestinationPlugin().format(n)

        assert "reply_markup" not in result
        assert "ann@example.com" in result["text"]


class TestRetryNumbering:
    """Headline and insight must number the same retry the same way."""

    def test_headline_and_insight_agree(self) -> None:
        """attempt_count=2 is retry #1 in both the headline and the insight."""
        n = _build(_payment_failure_event(), {"email": "ann@example.com"})

        assert n.headline == "$49.00 payment failed (retry #1)"
        assert n.insight is not None
        assert n.insight.text.startswith("Retry #1")


class TestNullCustomerNames:
    """Explicit null name fields must not render as the string "None"."""

    def test_null_last_name(self) -> None:
        """A present-but-null last_name yields "Ann", not "Ann None"."""
        n = _build(
            {"type": "payment_success", "provider": "stripe", "amount": 10.0},
            {"email": "ann@example.com", "first_name": "Ann", "last_name": None},
        )

        assert n.customer.name == "Ann"
        assert n.customer.company_name == "Ann"

    def test_all_null_fields_fall_back_to_customer_id(self) -> None:
        """Null email and names fall through to the customer id."""
        n = _build(
            {"type": "payment_success", "provider": "stripe", "amount": 10.0},
            {
                "email": None,
                "first_name": None,
                "last_name": None,
                "customer_id": "cus_x",
            },
        )

        assert n.customer.name is None
        assert n.customer.email == ""
        assert n.customer.company_name == "cus_x"


class TestCurrencyAwareThresholds:
    """USD insight thresholds are scaled to the event currency."""

    def test_small_yen_payment_not_large(self) -> None:
        """A ¥1,500 payment (about $10) is not a "Large payment"."""
        event = {"type": "payment_success", "amount": 1500.0, "currency": "JPY"}

        assert InsightDetector()._detect_large_payment(event, {}) is None

    def test_large_yen_payment_is_large(self) -> None:
        """A ¥200,000 payment clears the scaled large-payment threshold."""
        event = {"type": "payment_success", "amount": 200000.0, "currency": "jpy"}

        insight = InsightDetector()._detect_large_payment(event, {})

        assert insight is not None
        assert insight.text == "Large payment received"

    def test_usd_large_payment_unchanged(self) -> None:
        """USD keeps the nominal $1,000 threshold."""
        event = {"type": "payment_success", "amount": 1000.0, "currency": "USD"}

        assert InsightDetector()._detect_large_payment(event, {}) is not None

    def test_unknown_currency_skips_money_insights(self) -> None:
        """Currencies without a factor get no large/VIP/LTV insight or flags."""
        detector = InsightDetector()
        event = {"type": "payment_success", "amount": 50000.0, "currency": "VND"}
        customer = {"total_spent": 900000.0}

        assert detector._detect_large_payment(event, customer) is None
        assert detector._detect_vip_status(event, customer) is None
        assert detector._detect_ltv_milestone(event, customer) is None
        failure = {**event, "type": "payment_failure"}
        assert detector.detect_risk_status(failure, customer) == []

    def test_krw_ltv_not_vip_at_nominal_threshold(self) -> None:
        """₩20,000 lifetime spend is neither VIP nor at risk."""
        detector = InsightDetector()
        event = {"type": "payment_failure", "amount": 5000.0, "currency": "KRW"}

        assert detector.detect_risk_status(event, {"total_spent": 20000.0}) == []

    def test_yen_milestone_uses_scaled_amount_and_formatting(self) -> None:
        """The milestone text shows the scaled amount in yen formatting."""
        event = {"type": "payment_success", "amount": 30000.0, "currency": "JPY"}

        insight = InsightDetector()._detect_ltv_milestone(
            event, {"total_spent": 160000.0}
        )

        assert insight is not None
        assert insight.text == "Crossed ¥150,000 lifetime!"

    def test_yen_vip_text(self) -> None:
        """The VIP insight states the scaled threshold in yen."""
        event = {"type": "payment_success", "amount": 30000.0, "currency": "JPY"}

        insight = InsightDetector()._detect_vip_status(
            event, {"total_spent": 2000000.0}
        )

        assert insight is not None
        assert insight.text == "VIP customer (¥1,500,000+ LTV)"

    @pytest.mark.parametrize("currency", ["USD", "EUR", "GBP"])
    def test_parity_currencies_keep_nominal_milestones(self, currency: str) -> None:
        """Near-dollar currencies keep round nominal milestones."""
        event = {"type": "payment_success", "amount": 100.0, "currency": currency}

        insight = InsightDetector()._detect_ltv_milestone(
            event, {"total_spent": 1050.0}
        )

        assert insight is not None
        assert "1,000 lifetime!" in insight.text


class TestFirstOrderConservative:
    """Shopify first-order detection reads the snapshot post-payment."""

    @staticmethod
    def _event(amount: float = 30.0) -> dict[str, Any]:
        """Return a Shopify payment_success event."""
        return {
            "type": "payment_success",
            "provider": "shopify",
            "amount": amount,
            "currency": "USD",
            "metadata": {},
        }

    def test_zero_orders_is_lagging_snapshot(self) -> None:
        """orders_count=0 means the snapshot lags; no first-order claim."""
        insight = InsightDetector()._detect_first_payment(
            self._event(), {"orders_count": 0}
        )

        assert insight is None

    def test_one_prior_order_in_lagging_snapshot(self) -> None:
        """orders_count=1 with spend from an earlier order is not first."""
        insight = InsightDetector()._detect_first_payment(
            self._event(30.0), {"orders_count": 1, "total_spent": "55.00"}
        )

        assert insight is None

    def test_consistent_first_order_fires(self) -> None:
        """orders_count=1 whose spend equals this order is a first order."""
        insight = InsightDetector()._detect_first_payment(
            self._event(30.0), {"orders_count": "1", "total_spent": "30.00"}
        )

        assert insight is not None
        assert insight.text == "First payment from this customer"


def _slack_text(formatted: dict[str, Any]) -> str:
    """Concatenate every text in a Slack attachment's blocks."""
    parts: list[str] = []
    for block in formatted["attachments"][0]["blocks"]:
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(text["text"])
        for element in block.get("elements", []):
            if isinstance(element, dict) and isinstance(element.get("text"), str):
                parts.append(element["text"])
        for fld in block.get("fields", []):
            parts.append(fld["text"])
    return "\n".join(parts)


def _order(currency: str, line_items: list[dict[str, Any]]) -> RichNotification:
    """Return an order notification in ``currency``."""
    return RichNotification(
        type=NotificationType.ORDER_CREATED,
        severity=NotificationSeverity.SUCCESS,
        headline="New order",
        headline_icon="cart",
        provider="shopify",
        provider_display="Shopify",
        payment=PaymentInfo(
            amount=1500.0,
            currency=currency,
            order_number="1001",
            line_items=line_items,
        ),
    )


class TestEcommerceMoneyFormatting:
    """Order totals and line items go through format_money."""

    def test_slack_yen_order(self) -> None:
        """Yen totals and prices render with ¥ and no decimals."""
        n = _order("JPY", [{"name": "Tea", "quantity": 2, "price": "750"}])

        text = _slack_text(SlackDestinationPlugin().format(n))

        assert "*Amount:* ¥1,500" in text
        assert "• 2x Tea (¥750)" in text
        assert "$" not in text

    def test_telegram_yen_order(self) -> None:
        """Telegram uses the same currency formatting."""
        n = _order("JPY", [{"name": "Tea", "quantity": 2, "price": "750"}])

        text = TelegramDestinationPlugin().format(n)["text"]

        assert "<b>Amount:</b> ¥1,500" in text
        assert "• 2x Tea (¥750)" in text


def _shopify_refund_event() -> dict[str, Any]:
    """Return a refund_issued event as the Shopify parser emits it."""
    return {
        "type": "refund_issued",
        "provider": "shopify",
        "customer_id": "order_820982911946154508",
        "amount": 25.0,
        "currency": "USD",
        "metadata": {
            "refund_id": 929361462,
            "order_id": 820982911946154508,
            "order_ref": "820982911946154508",
            "line_items": [{"name": "Shirt", "sku": "", "quantity": 1}],
        },
    }


class TestShopifyRefundRendering:
    """Refunds get a refund headline, an order ref and no payment badges."""

    def test_refund_headline(self) -> None:
        """The headline states the formatted refunded amount."""
        n = _build(_shopify_refund_event(), {"customer_id": "order_1"})

        assert n.headline == "$25.00 refunded for order #820982911946154508"

    def test_refund_headline_without_order(self) -> None:
        """Without an order reference the headline is just the amount."""
        event = _shopify_refund_event()
        event["metadata"] = {}
        n = _build(event, {"customer_id": "order_1"})

        assert n.headline == "$25.00 refunded"

    def test_refund_order_ref_used_as_order_number(self) -> None:
        """order_ref fills in for the missing order_number."""
        n = _build(_shopify_refund_event(), {"customer_id": "order_1"})

        assert n.payment is not None
        assert n.payment.order_number == "820982911946154508"

    def test_slack_refund_rendering(self) -> None:
        """Slack shows the order ref, no $0.00 price and no payment badge."""
        n = _build(_shopify_refund_event(), {"customer_id": "order_1"})

        text = _slack_text(SlackDestinationPlugin().format(n))

        assert "Order #820982911946154508" in text
        assert "N/A" not in text
        assert "• 1x Shirt" in text
        assert "$0.00" not in text
        assert "One-Time" not in text
        assert "Recurring" not in text

    def test_telegram_refund_rendering(self) -> None:
        """Telegram shows the order ref, no $0.00 price and no payment badge."""
        n = _build(_shopify_refund_event(), {"customer_id": "order_1"})

        text = TelegramDestinationPlugin().format(n)["text"]

        assert "Order #820982911946154508" in text
        assert "N/A" not in text
        assert "• 1x Shirt" in text
        assert "0.00" not in text
        assert "One-Time" not in text
        assert "Recurring" not in text


class TestDashboardStoresAllLifecycleEvents:
    """Canceled, renewed and refund events reach the recent-activity feed."""

    @pytest.mark.parametrize(
        ("event_type", "display_type"),
        [
            ("subscription_canceled", "subscription"),
            ("subscription_renewed", "subscription"),
            ("refund_issued", "payment"),
        ],
    )
    def test_event_is_stored_with_display_type(
        self, event_type: str, display_type: str
    ) -> None:
        """The processor stores the event and it gets a display type."""
        processor = EventProcessor.__new__(EventProcessor)
        processor.db_lookup = MagicMock()
        event = {"type": event_type, "provider": "stripe", "customer_id": "c"}

        processor._store_enriched_record(event, MagicMock(), None)

        processor.db_lookup.store_enriched_record.assert_called_once()
        lookup = DatabaseLookupService()
        assert lookup._get_event_display_type(event_type) == display_type

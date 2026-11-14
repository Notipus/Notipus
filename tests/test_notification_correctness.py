"""Regression tests for notification building and rendering correctness.

Covers how events become RichNotifications (headlines, insights, badges)
and how destination plugins render them (Slack, Telegram).
"""

from typing import Any

from plugins.destinations.telegram import TelegramDestinationPlugin
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

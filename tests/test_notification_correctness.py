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

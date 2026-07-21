"""Security tests for Slack notification injection and price crashes.

These tests prove that attacker-controlled webhook fields (customer name,
line-item name, order number, etc.) cannot inject Slack broadcast
mentions (``<!channel>``) or fake links (``<url|text>``) into rendered
mrkdwn blocks, that non-numeric prices no longer abort formatting, and
that an unsafe Shopify shop domain is rejected before it reaches a URL.
"""

from typing import Any

import pytest
from plugins.destinations.slack import SlackDestinationPlugin
from plugins.destinations.slack_utils import safe_mrkdwn
from webhooks.models.rich_notification import (
    CompanyInfo,
    CustomerInfo,
    NotificationSeverity,
    NotificationType,
    PaymentInfo,
    RichNotification,
)
from webhooks.services.notification_builder import (
    NotificationBuilder,
    _normalize_shopify_shop_domain,
)


def get_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract Block Kit blocks from a formatted Slack message.

    Args:
        result: Formatted Slack message dict.

    Returns:
        List of block dicts.
    """
    if "attachments" in result and result["attachments"]:
        return result["attachments"][0].get("blocks", [])
    return result.get("blocks", [])


def render_text(result: dict[str, Any]) -> str:
    """Flatten all mrkdwn/plain text across every block into one string.

    Args:
        result: Formatted Slack message dict.

    Returns:
        Concatenated text of every text-bearing element in the message.
    """
    parts: list[str] = []
    for block in get_blocks(result):
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(text.get("text", ""))
        for element in block.get("elements", []):
            if isinstance(element, dict) and "text" in element:
                parts.append(str(element["text"]))
    return "\n".join(parts)


@pytest.fixture
def plugin() -> SlackDestinationPlugin:
    """Provide a SlackDestinationPlugin instance.

    Returns:
        A SlackDestinationPlugin ready to format notifications.
    """
    return SlackDestinationPlugin()


@pytest.fixture
def builder() -> NotificationBuilder:
    """Provide a NotificationBuilder instance.

    Returns:
        A NotificationBuilder for dashboard-action tests.
    """
    return NotificationBuilder()


def test_safe_mrkdwn_neutralizes_broadcast_mention() -> None:
    """safe_mrkdwn turns a broadcast mention into inert readable text."""
    result = safe_mrkdwn("Hi <!channel> everyone")
    assert "<!channel>" not in result
    assert "@channel" in result


def test_safe_mrkdwn_escapes_fake_link() -> None:
    """safe_mrkdwn escapes angle brackets so a fake link cannot render."""
    result = safe_mrkdwn("<https://evil/login|Update billing>")
    assert "<https://evil/login|Update billing>" not in result
    assert "&lt;" in result and "&gt;" in result


def test_safe_mrkdwn_handles_none() -> None:
    """safe_mrkdwn returns an empty string for None input."""
    assert safe_mrkdwn(None) == ""


def test_customer_name_injection_is_neutralized(
    plugin: SlackDestinationPlugin,
) -> None:
    """A ``<!channel>`` in the customer name is neutralized in the blocks."""
    notification = RichNotification(
        type=NotificationType.CUSTOMER_CREATED,
        severity=NotificationSeverity.INFO,
        headline="New customer",
        headline_icon="user",
        provider="shopify",
        provider_display="Shopify",
        customer=CustomerInfo(email="", name="Evil <!channel> Corp"),
    )

    rendered = render_text(plugin.format(notification))

    assert "<!channel>" not in rendered
    assert "@channel" in rendered


def test_customer_email_fake_link_is_escaped(
    plugin: SlackDestinationPlugin,
) -> None:
    """A ``<url|text>`` payload in the email field cannot form a link."""
    notification = RichNotification(
        type=NotificationType.CUSTOMER_CREATED,
        severity=NotificationSeverity.INFO,
        headline="New customer",
        headline_icon="user",
        provider="shopify",
        provider_display="Shopify",
        customer=CustomerInfo(email="<https://evil/login|Update billing>"),
    )

    rendered = render_text(plugin.format(notification))

    assert "<https://evil/login|Update billing>" not in rendered
    assert "&lt;https://evil/login|Update billing&gt;" in rendered


def test_line_item_name_injection_is_neutralized(
    plugin: SlackDestinationPlugin,
) -> None:
    """A ``<!channel>`` in a line-item name is neutralized in the blocks."""
    notification = RichNotification(
        type=NotificationType.PAYMENT_SUCCESS,
        severity=NotificationSeverity.SUCCESS,
        headline="Order paid",
        headline_icon="money",
        provider="shopify",
        provider_display="Shopify",
        payment=PaymentInfo(
            amount=29.99,
            currency="USD",
            order_number="1001",
            line_items=[{"quantity": 1, "name": "Widget <!channel>", "price": 9.99}],
        ),
    )

    rendered = render_text(plugin.format(notification))

    assert "<!channel>" not in rendered
    assert "@channel" in rendered


def test_company_name_injection_is_neutralized(
    plugin: SlackDestinationPlugin,
) -> None:
    """A fake link in the company name is escaped in the company block."""
    notification = RichNotification(
        type=NotificationType.PAYMENT_SUCCESS,
        severity=NotificationSeverity.SUCCESS,
        headline="Payment",
        headline_icon="money",
        provider="stripe",
        provider_display="Stripe",
        company=CompanyInfo(
            name="<https://evil|ACME>",
            domain="acme.test",
        ),
    )

    rendered = render_text(plugin.format(notification))

    assert "<https://evil|ACME>" not in rendered
    assert "&lt;https://evil|ACME&gt;" in rendered


def test_string_line_item_price_renders_without_raising(
    plugin: SlackDestinationPlugin,
) -> None:
    """A string price (Shopify style) is coerced and does not raise."""
    notification = RichNotification(
        type=NotificationType.PAYMENT_SUCCESS,
        severity=NotificationSeverity.SUCCESS,
        headline="Order paid",
        headline_icon="money",
        provider="shopify",
        provider_display="Shopify",
        payment=PaymentInfo(
            amount="29.99",  # type: ignore[arg-type]
            currency="USD",
            order_number="1001",
            line_items=[{"quantity": "2", "name": "Widget", "price": "19.99"}],
        ),
    )

    rendered = render_text(plugin.format(notification))

    # Both the order amount and the line-item price render as money.
    assert "29.99" in rendered
    assert "19.99" in rendered
    assert "2x Widget" in rendered


def test_non_numeric_price_falls_back_without_raising(
    plugin: SlackDestinationPlugin,
) -> None:
    """A garbage price falls back to 0.00 instead of aborting the message."""
    notification = RichNotification(
        type=NotificationType.PAYMENT_SUCCESS,
        severity=NotificationSeverity.SUCCESS,
        headline="Order paid",
        headline_icon="money",
        provider="shopify",
        provider_display="Shopify",
        payment=PaymentInfo(
            amount="not-a-number",  # type: ignore[arg-type]
            currency="USD",
            order_number="1001",
            line_items=[{"quantity": 1, "name": "Widget", "price": "free"}],
        ),
    )

    rendered = render_text(plugin.format(notification))

    assert "$0.00" in rendered


@pytest.mark.parametrize(
    "shop_domain",
    [
        "evil.com/admin/orders/1?x=",
        "evil.com#",
        "evil.com/../../foo",
        "not a domain",
        "http://evil.com",
        "evil.com:8080",
        "user:pass@evil.com",
        "",
        "nodot",
    ],
)
def test_invalid_shop_domain_is_rejected(
    builder: NotificationBuilder, shop_domain: str
) -> None:
    """An unsafe shop domain yields no dashboard button (URL not built)."""
    assert _normalize_shopify_shop_domain(shop_domain) is None

    event_data = {
        "provider": "shopify",
        "metadata": {"order_id": "123", "shop_domain": shop_domain},
    }
    action = builder._build_provider_dashboard_action(event_data)
    assert action is None


def test_valid_shop_domain_builds_button(builder: NotificationBuilder) -> None:
    """A legitimate shop domain still produces a working dashboard link."""
    assert _normalize_shopify_shop_domain("Acme.myshopify.com") == "acme.myshopify.com"

    event_data = {
        "provider": "shopify",
        "metadata": {"order_id": "123", "shop_domain": "acme.myshopify.com"},
    }
    action = builder._build_provider_dashboard_action(event_data)
    assert action is not None
    assert action.url == "https://acme.myshopify.com/admin/orders/123"

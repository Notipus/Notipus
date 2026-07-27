"""Tests for the TelegramDestinationPlugin.

This module tests the TelegramDestinationPlugin class that converts
RichNotification objects to Telegram HTML format and sends them via
the Telegram Bot API.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from plugins.base import PluginCapability, PluginType
from plugins.destinations.base import BaseDestinationPlugin
from plugins.destinations.telegram import TelegramDestinationPlugin
from webhooks.models.rich_notification import (
    ActionButton,
    CompanyInfo,
    CustomerInfo,
    DetailSection,
    InsightInfo,
    NotificationSeverity,
    NotificationType,
    PaymentInfo,
    RichNotification,
)


@pytest.fixture
def plugin() -> TelegramDestinationPlugin:
    """Create a TelegramDestinationPlugin instance."""
    return TelegramDestinationPlugin()


@pytest.fixture
def basic_notification() -> RichNotification:
    """Create a basic RichNotification for testing."""
    return RichNotification(
        type=NotificationType.PAYMENT_SUCCESS,
        severity=NotificationSeverity.SUCCESS,
        headline="$299.00 from Acme Inc",
        headline_icon="money",
        provider="stripe",
        provider_display="Stripe",
        customer=CustomerInfo(
            email="alice@acme.com",
            name="Alice Smith",
            company_name="Acme Inc",
            tenure_display="Since Mar 2024",
            ltv_display="$1.5k",
            orders_count=5,
            total_spent=1500.00,
        ),
        payment=PaymentInfo(
            amount=299.00,
            currency="USD",
            interval="monthly",
            plan_name="Enterprise",
            subscription_id="sub_123",
            payment_method="visa",
            card_last4="4242",
        ),
        is_recurring=True,
        billing_interval="monthly",
    )


@pytest.fixture
def notification_with_insight(basic_notification: RichNotification) -> RichNotification:
    """Create a notification with an insight."""
    basic_notification.insight = InsightInfo(
        icon="celebration",
        text="Crossed $5,000 lifetime!",
    )
    return basic_notification


@pytest.fixture
def notification_with_company(basic_notification: RichNotification) -> RichNotification:
    """Create a notification with company enrichment."""
    basic_notification.company = CompanyInfo(
        name="Acme Corporation",
        domain="acme.com",
        industry="Technology",
        year_founded=2015,
        employee_count="51-200",
        description="Acme builds tools for developers to build better software.",
        logo_url="https://example.com/logo.png",
        linkedin_url="https://linkedin.com/company/acme-corp",
    )
    return basic_notification


@pytest.fixture
def notification_with_actions(basic_notification: RichNotification) -> RichNotification:
    """Create a notification with action buttons."""
    basic_notification.actions = [
        ActionButton(text="View in Stripe", url="https://stripe.com", style="primary"),
        ActionButton(text="Website", url="https://acme.com", style="default"),
    ]
    return basic_notification


@pytest.fixture
def ecommerce_notification() -> RichNotification:
    """Create an ecommerce notification with line items."""
    return RichNotification(
        type=NotificationType.ORDER_CREATED,
        severity=NotificationSeverity.SUCCESS,
        headline="New order #12345 from John Doe",
        headline_icon="cart",
        provider="shopify",
        provider_display="Shopify",
        customer=CustomerInfo(
            email="john@example.com",
            name="John Doe",
        ),
        payment=PaymentInfo(
            amount=199.99,
            currency="USD",
            order_number="12345",
            line_items=[
                {"name": "Widget Pro", "quantity": 2, "price": 49.99},
                {"name": "Gadget Plus", "quantity": 1, "price": 99.99},
            ],
        ),
    )


@pytest.fixture
def non_payment_notification() -> RichNotification:
    """Create a non-payment notification (e.g., feedback)."""
    notification = RichNotification(
        type=NotificationType.FEEDBACK_RECEIVED,
        severity=NotificationSeverity.INFO,
        headline="New feedback from customer",
        headline_icon="feedback",
        provider="intercom",
        provider_display="Intercom",
        customer=CustomerInfo(
            email="user@example.com",
            name="Test User",
        ),
    )
    section = DetailSection(
        title="Feedback Details",
        icon="feedback",
        text="The product is great!",
    )
    section.add_field("Rating", "5 stars", "star")
    notification.detail_sections.append(section)
    return notification


class TestTelegramPluginMetadata:
    """Test TelegramDestinationPlugin metadata and registration."""

    def test_plugin_metadata_name(self) -> None:
        """Test TelegramDestinationPlugin has correct name."""
        metadata = TelegramDestinationPlugin.get_metadata()
        assert metadata.name == "telegram"

    def test_plugin_metadata_display_name(self) -> None:
        """Test TelegramDestinationPlugin has correct display name."""
        metadata = TelegramDestinationPlugin.get_metadata()
        assert metadata.display_name == "Telegram"

    def test_plugin_metadata_type(self) -> None:
        """Test TelegramDestinationPlugin has correct plugin type."""
        metadata = TelegramDestinationPlugin.get_metadata()
        assert metadata.plugin_type == PluginType.DESTINATION

    def test_plugin_metadata_capabilities(self) -> None:
        """Test TelegramDestinationPlugin has expected capabilities."""
        metadata = TelegramDestinationPlugin.get_metadata()
        assert PluginCapability.RICH_FORMATTING in metadata.capabilities
        assert PluginCapability.ACTIONS in metadata.capabilities

    def test_plugin_instance(self) -> None:
        """Test creating TelegramDestinationPlugin instance."""
        plugin = TelegramDestinationPlugin()
        assert isinstance(plugin, TelegramDestinationPlugin)
        assert isinstance(plugin, BaseDestinationPlugin)

    def test_plugin_name(self) -> None:
        """Test TelegramDestinationPlugin name method."""
        plugin = TelegramDestinationPlugin()
        assert plugin.get_plugin_name() == "telegram"

    def test_plugin_custom_timeout(self) -> None:
        """Test TelegramDestinationPlugin with custom timeout."""
        plugin = TelegramDestinationPlugin(timeout=60)
        assert plugin.timeout == 60


class TestTelegramFormatBasicOutput:
    """Test basic format output structure."""

    def test_format_returns_dict(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format returns a dictionary."""
        result = plugin.format(basic_notification)
        assert isinstance(result, dict)

    def test_format_has_text(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format output has text."""
        result = plugin.format(basic_notification)
        assert "text" in result
        assert isinstance(result["text"], str)
        assert len(result["text"]) > 0

    def test_format_has_parse_mode(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format output has HTML parse mode."""
        result = plugin.format(basic_notification)
        assert "parse_mode" in result
        assert result["parse_mode"] == "HTML"

    def test_format_includes_headline(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format output includes headline."""
        result = plugin.format(basic_notification)
        assert "$299.00 from Acme Inc" in result["text"]

    def test_format_includes_emoji_for_headline(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format uses emoji for headline icon."""
        result = plugin.format(basic_notification)
        assert "💰" in result["text"]  # money icon

    def test_format_includes_provider(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format output includes provider."""
        result = plugin.format(basic_notification)
        assert "Stripe" in result["text"]

    def test_format_includes_html_tags(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format output includes HTML tags."""
        result = plugin.format(basic_notification)
        assert "<b>" in result["text"]  # Bold text


class TestTelegramFormatPaymentNotification:
    """Test formatting payment notifications."""

    def test_format_includes_payment_amount(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that payment amount is included."""
        result = plugin.format(basic_notification)
        assert "299" in result["text"]

    def test_format_includes_plan_name(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that plan name is included."""
        result = plugin.format(basic_notification)
        assert "Enterprise" in result["text"]

    def test_format_includes_subscription_id(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that subscription ID is included."""
        result = plugin.format(basic_notification)
        assert "sub_123" in result["text"]

    def test_format_includes_recurring_badge(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that recurring badge is included."""
        result = plugin.format(basic_notification)
        assert "Recurring" in result["text"]

    def test_format_includes_billing_interval(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that billing interval is included."""
        result = plugin.format(basic_notification)
        assert "Monthly" in result["text"]

    def test_format_includes_payment_method(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that payment method is included."""
        result = plugin.format(basic_notification)
        assert "Visa" in result["text"]
        assert "4242" in result["text"]

    def test_format_includes_failure_reason(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that payment failure reason is displayed."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_FAILURE,
            severity=NotificationSeverity.ERROR,
            headline="Payment failed for Acme Inc",
            headline_icon="error",
            provider="stripe",
            provider_display="Stripe",
            payment=PaymentInfo(
                amount=99.00,
                currency="USD",
                failure_reason="Card declined - insufficient funds",
            ),
        )
        result = plugin.format(notification)
        assert "Card declined - insufficient funds" in result["text"]
        assert "❌" in result["text"]
        assert "Reason" in result["text"]


class TestTelegramFormatEcommerceNotification:
    """Test formatting e-commerce notifications."""

    def test_format_includes_order_number(
        self,
        plugin: TelegramDestinationPlugin,
        ecommerce_notification: RichNotification,
    ) -> None:
        """Test that order number is included."""
        result = plugin.format(ecommerce_notification)
        assert "12345" in result["text"]

    def test_format_includes_line_items(
        self,
        plugin: TelegramDestinationPlugin,
        ecommerce_notification: RichNotification,
    ) -> None:
        """Test that line items are included."""
        result = plugin.format(ecommerce_notification)
        assert "Widget Pro" in result["text"]
        assert "Gadget Plus" in result["text"]

    def test_format_includes_quantities(
        self,
        plugin: TelegramDestinationPlugin,
        ecommerce_notification: RichNotification,
    ) -> None:
        """Test that quantities are included."""
        result = plugin.format(ecommerce_notification)
        assert "2x" in result["text"]
        assert "1x" in result["text"]

    def test_format_truncates_line_items_to_five(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that line items are truncated to max 5 with 'more items' text."""
        notification = RichNotification(
            type=NotificationType.ORDER_CREATED,
            severity=NotificationSeverity.SUCCESS,
            headline="Large order",
            headline_icon="cart",
            provider="shopify",
            provider_display="Shopify",
            payment=PaymentInfo(
                amount=999.99,
                currency="USD",
                order_number="99999",
                line_items=[
                    {"name": f"Item {i}", "quantity": 1, "price": 10.00}
                    for i in range(8)
                ],
            ),
        )
        result = plugin.format(notification)
        # First 5 items should be present
        for i in range(5):
            assert f"Item {i}" in result["text"]
        # Items 5-7 should NOT be present
        assert "Item 5" not in result["text"]
        assert "Item 6" not in result["text"]
        assert "Item 7" not in result["text"]
        # Should show "...and 3 more items"
        assert "3 more items" in result["text"]

    def test_format_uses_correct_currency_in_line_items(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that line items use the payment currency, not hardcoded $."""
        notification = RichNotification(
            type=NotificationType.ORDER_CREATED,
            severity=NotificationSeverity.SUCCESS,
            headline="Euro order",
            headline_icon="cart",
            provider="shopify",
            provider_display="Shopify",
            payment=PaymentInfo(
                amount=50.00,
                currency="EUR",
                order_number="12345",
                line_items=[
                    {"name": "Widget", "quantity": 1, "price": 50.00},
                ],
            ),
        )
        result = plugin.format(notification)
        assert "EUR 50.00" in result["text"]


class TestTelegramFormatInsight:
    """Test formatting insights."""

    def test_format_includes_insight(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_insight: RichNotification,
    ) -> None:
        """Test that insight is included."""
        result = plugin.format(notification_with_insight)
        assert "Crossed $5,000 lifetime!" in result["text"]

    def test_format_insight_has_emoji(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_insight: RichNotification,
    ) -> None:
        """Test that insight has emoji."""
        result = plugin.format(notification_with_insight)
        assert "🎉" in result["text"]  # celebration icon

    def test_format_insight_is_italic(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_insight: RichNotification,
    ) -> None:
        """Test that insight is italic."""
        result = plugin.format(notification_with_insight)
        assert "<i>" in result["text"]


class TestTelegramFormatCompany:
    """Test formatting company information."""

    def test_format_includes_company_name(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test that company name is included."""
        result = plugin.format(notification_with_company)
        assert "Acme Corporation" in result["text"]

    def test_format_includes_company_industry(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test that company industry is included."""
        result = plugin.format(notification_with_company)
        assert "Technology" in result["text"]

    def test_format_includes_company_founded(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test that company founded year is included."""
        result = plugin.format(notification_with_company)
        assert "2015" in result["text"]

    def test_format_includes_company_size(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test that company size is included."""
        result = plugin.format(notification_with_company)
        assert "51-200" in result["text"]

    def test_format_includes_website_link(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test that website link is included."""
        result = plugin.format(notification_with_company)
        assert "acme.com" in result["text"]
        assert 'href="https://acme.com"' in result["text"]

    def test_format_includes_linkedin_link(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test that LinkedIn link is included."""
        result = plugin.format(notification_with_company)
        assert "LinkedIn" in result["text"]

    def test_format_truncates_long_description(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that company description is truncated to 100 chars."""
        long_description = "A" * 150  # 150 characters
        basic_notification.company = CompanyInfo(
            name="Test Company",
            domain="test.com",
            description=long_description,
        )
        result = plugin.format(basic_notification)
        # Should have first 100 chars + "..."
        assert "A" * 100 + "..." in result["text"]
        # Should NOT have the full 150 chars
        assert "A" * 150 not in result["text"]

    def test_format_no_truncation_for_short_description(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that short company descriptions are not truncated."""
        short_description = "A short description."
        basic_notification.company = CompanyInfo(
            name="Test Company",
            domain="test.com",
            description=short_description,
        )
        result = plugin.format(basic_notification)
        assert short_description in result["text"]
        assert "..." not in result["text"] or "..." not in short_description


class TestTelegramFormatCustomer:
    """Test formatting customer information."""

    def test_format_includes_customer_email(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that customer email is included."""
        result = plugin.format(basic_notification)
        assert "alice@acme.com" in result["text"]

    def test_format_includes_customer_tenure(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that customer tenure is included."""
        result = plugin.format(basic_notification)
        assert "Since Mar 2024" in result["text"]

    def test_format_includes_customer_ltv(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that customer LTV is included."""
        result = plugin.format(basic_notification)
        assert "$1.5k" in result["text"]

    def test_format_includes_vip_flag(self, plugin: TelegramDestinationPlugin) -> None:
        """Test that VIP status flag is displayed."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="Payment received",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(
                email="vip@example.com",
                status_flags=["vip"],
            ),
        )
        result = plugin.format(notification)
        assert "⭐" in result["text"]
        assert "VIP" in result["text"]

    def test_format_includes_at_risk_flag(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that at-risk status flag is displayed."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_FAILURE,
            severity=NotificationSeverity.ERROR,
            headline="Payment failed",
            headline_icon="error",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(
                email="risky@example.com",
                status_flags=["at_risk"],
            ),
        )
        result = plugin.format(notification)
        assert "🚨" in result["text"]
        assert "At Risk" in result["text"]

    def test_format_shows_name_when_no_email(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that customer name is shown when email is not available."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="Payment received",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(
                email="",  # Empty email
                name="John Doe",
            ),
        )
        result = plugin.format(notification)
        assert "John Doe" in result["text"]


class TestTelegramFormatActions:
    """Test formatting action buttons."""

    def test_format_includes_reply_markup(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test that action buttons create reply_markup."""
        result = plugin.format(notification_with_actions)
        assert "reply_markup" in result
        assert "inline_keyboard" in result["reply_markup"]

    def test_format_action_buttons_have_text(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test that action buttons have text."""
        result = plugin.format(notification_with_actions)
        buttons = result["reply_markup"]["inline_keyboard"]
        # Flatten the buttons
        all_buttons = [b for row in buttons for b in row]
        texts = [b["text"] for b in all_buttons]
        assert "View in Stripe" in texts
        assert "Website" in texts

    def test_format_action_buttons_have_url(
        self,
        plugin: TelegramDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test that action buttons have URLs."""
        result = plugin.format(notification_with_actions)
        buttons = result["reply_markup"]["inline_keyboard"]
        all_buttons = [b for row in buttons for b in row]
        urls = [b["url"] for b in all_buttons]
        assert "https://stripe.com" in urls
        assert "https://acme.com" in urls

    def test_format_no_actions_no_reply_markup(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that no actions means no reply_markup."""
        result = plugin.format(basic_notification)
        assert "reply_markup" not in result

    def test_format_limits_buttons_to_six(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that action buttons are limited to 6."""
        basic_notification.actions = [
            ActionButton(text=f"Button {i}", url=f"https://example.com/{i}")
            for i in range(10)
        ]
        result = plugin.format(basic_notification)
        buttons = result["reply_markup"]["inline_keyboard"]
        all_buttons = [b for row in buttons for b in row]
        assert len(all_buttons) == 6

    def test_format_buttons_two_per_row(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that buttons are arranged with max 2 per row."""
        basic_notification.actions = [
            ActionButton(text=f"Button {i}", url=f"https://example.com/{i}")
            for i in range(4)
        ]
        result = plugin.format(basic_notification)
        buttons = result["reply_markup"]["inline_keyboard"]
        # Should be 2 rows with 2 buttons each
        assert len(buttons) == 2
        assert len(buttons[0]) == 2
        assert len(buttons[1]) == 2


class TestTelegramFormatNonPayment:
    """Test formatting non-payment notifications."""

    def test_format_non_payment_has_category(
        self,
        plugin: TelegramDestinationPlugin,
        non_payment_notification: RichNotification,
    ) -> None:
        """Test that non-payment notification shows category."""
        result = plugin.format(non_payment_notification)
        assert "Support" in result["text"]

    def test_format_includes_detail_section(
        self,
        plugin: TelegramDestinationPlugin,
        non_payment_notification: RichNotification,
    ) -> None:
        """Test that detail sections are included."""
        result = plugin.format(non_payment_notification)
        assert "Feedback Details" in result["text"]
        assert "The product is great!" in result["text"]

    def test_format_includes_detail_fields(
        self,
        plugin: TelegramDestinationPlugin,
        non_payment_notification: RichNotification,
    ) -> None:
        """Test that detail fields are included."""
        result = plugin.format(non_payment_notification)
        assert "Rating" in result["text"]
        assert "5 stars" in result["text"]


class TestTelegramHtmlEscaping:
    """Test HTML escaping in formatted output."""

    def test_escapes_html_in_headline(self, plugin: TelegramDestinationPlugin) -> None:
        """Test that HTML characters are escaped in headline."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="$100 from <script>alert('xss')</script>",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
        )
        result = plugin.format(notification)
        assert "<script>" not in result["text"]
        assert "&lt;script&gt;" in result["text"]

    def test_escapes_html_in_customer_email(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that HTML characters are escaped in customer email."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="Payment received",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(
                email="<script>alert('xss')</script>@example.com",
            ),
        )
        result = plugin.format(notification)
        assert "<script>" not in result["text"]
        assert "&lt;script&gt;" in result["text"]

    def test_escape_html_handles_empty_string(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that _escape_html handles empty strings."""
        assert plugin._escape_html("") == ""

    def test_escape_html_handles_ampersand(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that _escape_html escapes ampersands."""
        assert plugin._escape_html("Tom & Jerry") == "Tom &amp; Jerry"

    def test_escapes_html_in_line_items(
        self, plugin: TelegramDestinationPlugin
    ) -> None:
        """Test that HTML characters are escaped in line item names."""
        notification = RichNotification(
            type=NotificationType.ORDER_CREATED,
            severity=NotificationSeverity.SUCCESS,
            headline="New order",
            headline_icon="cart",
            provider="shopify",
            provider_display="Shopify",
            payment=PaymentInfo(
                amount=100.00,
                currency="USD",
                order_number="123",
                line_items=[
                    {
                        "name": "<script>alert('xss')</script>",
                        "quantity": 1,
                        "price": 100,
                    },
                ],
            ),
        )
        result = plugin.format(notification)
        assert "<script>" not in result["text"]
        assert "&lt;script&gt;" in result["text"]


class TestTelegramSend:
    """Test sending notifications to Telegram."""

    def test_send_requires_bot_token(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that send raises error without bot_token."""
        formatted = plugin.format(basic_notification)
        with pytest.raises(ValueError, match="bot_token"):
            plugin.send(formatted, {"chat_id": "123"})

    def test_send_requires_chat_id(
        self, plugin: TelegramDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that send raises error without chat_id."""
        formatted = plugin.format(basic_notification)
        with pytest.raises(ValueError, match="chat_id"):
            plugin.send(formatted, {"bot_token": "123:ABC"})

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_calls_telegram_api(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Test that send calls the Telegram API."""
        mock_post.return_value.json.return_value = {"ok": True}
        formatted = plugin.format(basic_notification)

        result = plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "456"})

        assert result is True
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "https://api.telegram.org/bot123:ABC/sendMessage" == call_url

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_includes_chat_id(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Test that send includes chat_id in payload."""
        mock_post.return_value.json.return_value = {"ok": True}
        formatted = plugin.format(basic_notification)

        plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "-1001234"})

        call_data = mock_post.call_args[1]["data"]
        assert call_data["chat_id"] == "-1001234"

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_includes_parse_mode(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Test that send includes parse_mode in payload."""
        mock_post.return_value.json.return_value = {"ok": True}
        formatted = plugin.format(basic_notification)

        plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "456"})

        call_data = mock_post.call_args[1]["data"]
        assert call_data["parse_mode"] == "HTML"

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_disables_web_preview(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Send disables link previews via link_preview_options.

        link_preview_options replaced the deprecated
        disable_web_page_preview parameter and is JSON-serialized like
        reply_markup.
        """
        mock_post.return_value.json.return_value = {"ok": True}
        formatted = plugin.format(basic_notification)

        plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "456"})

        call_data = mock_post.call_args[1]["data"]
        assert "disable_web_page_preview" not in call_data
        assert json.loads(call_data["link_preview_options"]) == {"is_disabled": True}

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_serializes_reply_markup_as_json(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test that reply_markup is JSON serialized when sending."""
        import json

        mock_post.return_value.json.return_value = {"ok": True}
        formatted = plugin.format(notification_with_actions)

        plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "456"})

        call_data = mock_post.call_args[1]["data"]
        assert "reply_markup" in call_data
        # Should be a JSON string, not a dict
        assert isinstance(call_data["reply_markup"], str)
        # Should be valid JSON
        parsed = json.loads(call_data["reply_markup"])
        assert "inline_keyboard" in parsed

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_handles_api_error(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Test that send raises error on API error."""
        mock_post.return_value.json.return_value = {
            "ok": False,
            "description": "Bot was blocked",
        }
        formatted = plugin.format(basic_notification)

        with pytest.raises(RuntimeError, match="Bot was blocked"):
            plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "456"})

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_handles_timeout(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Test that send handles timeout."""
        import requests

        mock_post.side_effect = requests.exceptions.Timeout()
        formatted = plugin.format(basic_notification)

        with pytest.raises(RuntimeError, match="timed out"):
            plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "456"})

    @patch("plugins.destinations.telegram.requests.post")
    def test_send_handles_request_exception(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Test that send handles request exceptions."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError()
        formatted = plugin.format(basic_notification)

        with pytest.raises(RuntimeError, match="Failed to send"):
            plugin.send(formatted, {"bot_token": "123:ABC", "chat_id": "456"})


class TestTelegramFormatAndSend:
    """Test format_and_send convenience method."""

    @patch("plugins.destinations.telegram.requests.post")
    def test_format_and_send(
        self,
        mock_post: MagicMock,
        plugin: TelegramDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """Test format_and_send convenience method."""
        mock_post.return_value.json.return_value = {"ok": True}

        result = plugin.format_and_send(
            basic_notification, {"bot_token": "123:ABC", "chat_id": "456"}
        )

        assert result is True
        mock_post.assert_called_once()

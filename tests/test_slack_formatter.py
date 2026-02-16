"""Tests for the SlackDestinationPlugin.

This module tests the SlackDestinationPlugin class that converts
RichNotification objects to Slack Block Kit JSON.
"""

from typing import Any

import pytest
from plugins.destinations.base import BaseDestinationPlugin
from plugins.destinations.slack import SlackDestinationPlugin
from webhooks.models.rich_notification import (
    ActionButton,
    CompanyInfo,
    CustomerInfo,
    InsightInfo,
    NotificationSeverity,
    NotificationType,
    PaymentInfo,
    RichNotification,
)


def get_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract blocks from Slack message format.

    Handles both old format (top-level blocks) and new attachments format.

    Args:
        result: Formatted Slack message dict.

    Returns:
        List of block dicts.
    """
    if "blocks" in result:
        return result["blocks"]
    if "attachments" in result and result["attachments"]:
        return result["attachments"][0].get("blocks", [])
    return []


def get_color(result: dict[str, Any]) -> str | None:
    """Extract color from Slack message format.

    Handles both old format (top-level color) and new attachments format.

    Args:
        result: Formatted Slack message dict.

    Returns:
        Color string or None.
    """
    if "color" in result:
        return result["color"]
    if "attachments" in result and result["attachments"]:
        return result["attachments"][0].get("color")
    return None


@pytest.fixture
def formatter() -> SlackDestinationPlugin:
    """Create a SlackDestinationPlugin instance."""
    return SlackDestinationPlugin()


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


class TestSlackDestinationPluginRegistration:
    """Test SlackDestinationPlugin registration."""

    def test_plugin_metadata(self) -> None:
        """Test SlackDestinationPlugin has correct metadata."""
        metadata = SlackDestinationPlugin.get_metadata()
        assert metadata.name == "slack"

    def test_plugin_instance(self) -> None:
        """Test creating SlackDestinationPlugin instance."""
        plugin = SlackDestinationPlugin()
        assert isinstance(plugin, SlackDestinationPlugin)
        assert isinstance(plugin, BaseDestinationPlugin)

    def test_plugin_name(self) -> None:
        """Test SlackDestinationPlugin name."""
        plugin = SlackDestinationPlugin()
        assert plugin.get_plugin_name() == "slack"


class TestSlackDestinationPluginBasicOutput:
    """Test basic formatter output structure."""

    def test_format_returns_dict(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format returns a dictionary."""
        result = formatter.format(basic_notification)
        assert isinstance(result, dict)

    def test_format_has_blocks(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format output has blocks."""
        result = formatter.format(basic_notification)
        blocks = get_blocks(result)
        assert isinstance(blocks, list)
        assert len(blocks) > 0

    def test_format_has_color(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test that format output has color."""
        result = formatter.format(basic_notification)
        color = get_color(result)
        assert color is not None
        assert color.startswith("#")

    def test_success_color(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test success severity uses green color."""
        result = formatter.format(basic_notification)
        assert get_color(result) == "#28a745"

    def test_error_color(self, formatter: SlackDestinationPlugin) -> None:
        """Test error severity uses red color."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_FAILURE,
            severity=NotificationSeverity.ERROR,
            headline="Payment failed",
            headline_icon="error",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(email="test@example.com"),
        )
        result = formatter.format(notification)
        assert get_color(result) == "#dc3545"


class TestSlackDestinationPluginHeader:
    """Test header block formatting."""

    def test_header_block_present(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test header block is present."""
        result = formatter.format(basic_notification)
        header_block = get_blocks(result)[0]

        assert header_block["type"] == "header"

    def test_header_contains_headline(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test header contains headline text."""
        result = formatter.format(basic_notification)
        header_block = get_blocks(result)[0]

        assert "$299.00 from Acme Inc" in header_block["text"]["text"]

    def test_header_contains_emoji(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test header contains emoji."""
        result = formatter.format(basic_notification)
        header_block = get_blocks(result)[0]

        assert ":moneybag:" in header_block["text"]["text"]


class TestSlackDestinationPluginInsight:
    """Test insight block formatting."""

    def test_insight_block_present(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_insight: RichNotification,
    ) -> None:
        """Test insight block is present when notification has insight."""
        result = formatter.format(notification_with_insight)

        # Find context block with insight
        insight_blocks = [
            b
            for b in get_blocks(result)
            if b["type"] == "context"
            and any("lifetime" in str(e.get("text", "")) for e in b.get("elements", []))
        ]
        assert len(insight_blocks) == 1

    def test_insight_contains_text(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_insight: RichNotification,
    ) -> None:
        """Test insight contains the milestone text."""
        result = formatter.format(notification_with_insight)

        # Find the insight block
        for block in get_blocks(result):
            if block["type"] == "context":
                for element in block.get("elements", []):
                    if "lifetime" in str(element.get("text", "")):
                        assert "$5,000" in element["text"]
                        return
        pytest.fail("Insight text not found")

    def test_no_insight_block_without_insight(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test no insight block when notification has no insight."""
        basic_notification.insight = None
        result = formatter.format(basic_notification)

        # Count context blocks - should only have provider badge and customer footer
        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        assert len(context_blocks) == 2  # Provider badge + customer footer


class TestSlackDestinationPluginProviderBadge:
    """Test provider badge formatting."""

    def test_provider_badge_present(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test provider badge is present."""
        result = formatter.format(basic_notification)

        # Provider badge is a context block
        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        assert len(context_blocks) >= 1

    def test_provider_badge_contains_provider(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test provider badge contains provider name."""
        result = formatter.format(basic_notification)

        # Find the provider badge (first context block after header)
        for block in get_blocks(result):
            if block["type"] == "context":
                text = str(block.get("elements", [{}])[0].get("text", ""))
                if "Stripe" in text:
                    assert "Stripe" in text
                    return
        pytest.fail("Provider not found in badge")

    def test_provider_badge_contains_payment_type(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test provider badge contains payment type."""
        result = formatter.format(basic_notification)

        for block in get_blocks(result):
            if block["type"] == "context":
                text = str(block.get("elements", [{}])[0].get("text", ""))
                if "Recurring" in text:
                    assert "Monthly" in text
                    return
        pytest.fail("Payment type not found in badge")


class TestSlackDestinationPluginPaymentDetails:
    """Test payment details section formatting."""

    def test_payment_details_present(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test payment details section is present."""
        result = formatter.format(basic_notification)

        # Find section with payment details
        section_blocks = [b for b in get_blocks(result) if b["type"] == "section"]
        assert len(section_blocks) >= 1

    def test_payment_details_contains_amount(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test payment details contains amount."""
        result = formatter.format(basic_notification)

        for block in get_blocks(result):
            if block["type"] == "section":
                text = str(block.get("text", {}).get("text", ""))
                if "299" in text or "Amount" in text:
                    assert "299" in text
                    return
        pytest.fail("Amount not found in payment details")

    def test_payment_details_contains_arr(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test payment details contains ARR for monthly subscriptions."""
        result = formatter.format(basic_notification)

        for block in get_blocks(result):
            if block["type"] == "section":
                text = str(block.get("text", {}).get("text", ""))
                if "ARR" in text:
                    return
        pytest.fail("ARR not found in payment details")


class TestSlackDestinationPluginCompanySection:
    """Test company section formatting."""

    def test_company_section_present(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test company section is present when notification has company."""
        result = formatter.format(notification_with_company)

        # Find section with company info
        company_sections = [
            b
            for b in get_blocks(result)
            if b["type"] == "section" and "Acme Corporation" in str(b.get("text", {}))
        ]
        assert len(company_sections) == 1

    def test_company_section_has_logo(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test company section has logo accessory."""
        result = formatter.format(notification_with_company)

        for block in get_blocks(result):
            if block["type"] == "section" and "Acme Corporation" in str(
                block.get("text", {})
            ):
                assert "accessory" in block
                assert block["accessory"]["type"] == "image"
                assert block["accessory"]["image_url"] == "https://example.com/logo.png"
                return
        pytest.fail("Company section with logo not found")

    def test_company_section_contains_industry(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test company section contains industry."""
        result = formatter.format(notification_with_company)

        for block in get_blocks(result):
            if block["type"] == "section":
                text = str(block.get("text", {}).get("text", ""))
                if "Technology" in text:
                    return
        pytest.fail("Industry not found in company section")

    def test_company_section_html_description_converted(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test HTML in company description is converted to mrkdwn."""
        basic_notification.company = CompanyInfo(
            name="Test Corp",
            domain="test.com",
            description=(
                '<p><a href="https://test.com">Test Corp</a> builds software.</p>'
            ),
        )
        result = formatter.format(basic_notification)

        # Find company section
        for block in get_blocks(result):
            if block["type"] == "section" and "Test Corp" in str(block.get("text", {})):
                text = str(block.get("text", {}).get("text", ""))
                # Should have Slack link format, not HTML
                assert "<https://test.com|Test Corp>" in text
                assert "<p>" not in text
                assert "<a " not in text
                return
        pytest.fail("Company section with converted description not found")

    def test_company_section_html_tags_stripped(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test HTML tags are stripped from company description."""
        basic_notification.company = CompanyInfo(
            name="Html Corp",
            domain="html.com",
            description="<div><span>A company that makes things.</span></div>",
        )
        result = formatter.format(basic_notification)

        for block in get_blocks(result):
            if block["type"] == "section" and "Html Corp" in str(block.get("text", {})):
                text = str(block.get("text", {}).get("text", ""))
                # Should not contain HTML tags
                assert "<div>" not in text
                assert "<span>" not in text
                assert "A company that makes things" in text
                return
        pytest.fail("Company section not found")

    def test_company_section_description_full(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test long descriptions are shown in full (not truncated)."""
        long_description = "A" * 200  # 200 character description
        basic_notification.company = CompanyInfo(
            name="Long Corp",
            domain="long.com",
            description=long_description,
        )
        result = formatter.format(basic_notification)

        for block in get_blocks(result):
            if block["type"] == "section" and "Long Corp" in str(block.get("text", {})):
                text = str(block.get("text", {}).get("text", ""))
                # Full description should be present (not truncated)
                assert long_description in text
                return
        pytest.fail("Company section not found")


class TestSlackDestinationPluginCompanyLinks:
    """Test company links formatting."""

    def test_company_links_block_present(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test company links block is present when company has LinkedIn."""
        result = formatter.format(notification_with_company)

        # Find context block with LinkedIn link (Website is no longer shown here)
        links_blocks = [
            b
            for b in get_blocks(result)
            if b["type"] == "context"
            and any("LinkedIn" in str(e.get("text", "")) for e in b.get("elements", []))
        ]
        assert len(links_blocks) == 1

    def test_company_links_contains_linkedin(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Test company links contains LinkedIn link."""
        result = formatter.format(notification_with_company)

        for block in get_blocks(result):
            if block["type"] == "context":
                text = str(block.get("elements", [{}])[0].get("text", ""))
                if "LinkedIn" in text:
                    assert "https://linkedin.com/company/acme-corp" in text
                    assert ":briefcase:" in text
                    return
        pytest.fail("LinkedIn link not found in company links")

    def test_company_links_no_block_without_linkedin(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test no links block when company has domain but no LinkedIn."""
        basic_notification.company = CompanyInfo(
            name="Test Corp",
            domain="test.com",
            linkedin_url=None,
        )
        result = formatter.format(basic_notification)

        # No context block with links should be present (Website no longer shown)
        for block in get_blocks(result):
            if block["type"] == "context":
                text = str(block.get("elements", [{}])[0].get("text", ""))
                # Should not have Website or LinkedIn links
                assert "Website" not in text
                assert "LinkedIn" not in text

    def test_company_links_linkedin_only(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test company links shows LinkedIn only when no domain."""
        basic_notification.company = CompanyInfo(
            name="Test Corp",
            domain="",  # Empty domain
            linkedin_url="https://linkedin.com/company/test",
        )
        result = formatter.format(basic_notification)

        for block in get_blocks(result):
            if block["type"] == "context":
                text = str(block.get("elements", [{}])[0].get("text", ""))
                if "LinkedIn" in text:
                    assert "Website" not in text
                    return
        pytest.fail("LinkedIn-only links block not found")

    def test_no_company_links_without_data(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test no company links block when no domain and no LinkedIn."""
        basic_notification.company = CompanyInfo(
            name="Test Corp",
            domain="",
            linkedin_url=None,
        )
        result = formatter.format(basic_notification)

        # Should not have a links context block (only provider badge + customer footer)
        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        for block in context_blocks:
            text = str(block.get("elements", [{}])[0].get("text", ""))
            assert "Website" not in text
            assert "LinkedIn" not in text


class TestSlackDestinationPluginCustomerFooter:
    """Test customer footer formatting."""

    def test_customer_footer_present(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test customer footer is present."""
        result = formatter.format(basic_notification)

        # Customer footer is the last context block
        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        assert len(context_blocks) >= 1

    def test_customer_footer_contains_email(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test customer footer contains email."""
        result = formatter.format(basic_notification)

        # Check last context block
        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        last_context = context_blocks[-1]
        text = str(last_context.get("elements", [{}])[0].get("text", ""))

        assert "alice@acme.com" in text

    def test_customer_footer_contains_tenure(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test customer footer contains tenure."""
        result = formatter.format(basic_notification)

        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        last_context = context_blocks[-1]
        text = str(last_context.get("elements", [{}])[0].get("text", ""))

        assert "Since Mar 2024" in text

    def test_customer_footer_shows_risk_flag(
        self, formatter: SlackDestinationPlugin
    ) -> None:
        """Test customer footer shows risk flag."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_FAILURE,
            severity=NotificationSeverity.ERROR,
            headline="Payment failed",
            headline_icon="error",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(
                email="test@example.com",
                status_flags=["at_risk"],
            ),
        )
        result = formatter.format(notification)

        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        last_context = context_blocks[-1]
        text = str(last_context.get("elements", [{}])[0].get("text", ""))

        assert "At Risk" in text


class TestSlackDestinationPluginActions:
    """Test action buttons formatting."""

    def test_actions_block_present(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test actions block is present when notification has actions."""
        result = formatter.format(notification_with_actions)

        actions_blocks = [b for b in get_blocks(result) if b["type"] == "actions"]
        assert len(actions_blocks) == 1

    def test_actions_contain_buttons(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test actions block contains buttons."""
        result = formatter.format(notification_with_actions)

        actions_block = [b for b in get_blocks(result) if b["type"] == "actions"][0]
        assert len(actions_block["elements"]) == 2

    def test_button_has_correct_text(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test buttons have correct text."""
        result = formatter.format(notification_with_actions)

        actions_block = [b for b in get_blocks(result) if b["type"] == "actions"][0]
        button_texts = [e["text"]["text"] for e in actions_block["elements"]]

        assert "View in Stripe" in button_texts
        assert "Website" in button_texts

    def test_button_has_style(
        self,
        formatter: SlackDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Test primary button has style."""
        result = formatter.format(notification_with_actions)

        actions_block = [b for b in get_blocks(result) if b["type"] == "actions"][0]
        primary_button = [
            e
            for e in actions_block["elements"]
            if e["text"]["text"] == "View in Stripe"
        ][0]

        assert primary_button.get("style") == "primary"

    def test_no_actions_block_without_actions(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test no actions block when notification has no actions."""
        basic_notification.actions = []
        result = formatter.format(basic_notification)

        actions_blocks = [b for b in get_blocks(result) if b["type"] == "actions"]
        assert len(actions_blocks) == 0


class TestSlackDestinationPluginDivider:
    """Test divider block."""

    def test_divider_present(
        self, formatter: SlackDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Test divider block is present."""
        result = formatter.format(basic_notification)

        divider_blocks = [b for b in get_blocks(result) if b["type"] == "divider"]
        assert len(divider_blocks) >= 1


class TestSlackDestinationPluginEcommerceDetails:
    """Test e-commerce order details formatting."""

    def test_ecommerce_order_details(self, formatter: SlackDestinationPlugin) -> None:
        """Test e-commerce order details formatting."""
        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="$150.00 from Customer",
            headline_icon="money",
            provider="shopify",
            provider_display="Shopify",
            customer=CustomerInfo(email="test@example.com"),
            payment=PaymentInfo(
                amount=150.00,
                currency="USD",
                order_number="1001",
                line_items=[
                    {"name": "Widget", "quantity": 2, "price": 50.00},
                    {"name": "Gadget", "quantity": 1, "price": 50.00},
                ],
            ),
            is_recurring=False,
        )
        result = formatter.format(notification)

        # Find order details section
        for block in get_blocks(result):
            if block["type"] == "section":
                text = str(block.get("text", {}).get("text", ""))
                if "Order #1001" in text:
                    assert "Widget" in text
                    assert "Gadget" in text
                    return
        pytest.fail("Order details not found")


class TestSlackDestinationPluginDetailSections:
    """Test generic detail section formatting for non-payment events."""

    def test_detail_section_rendered(self, formatter: SlackDestinationPlugin) -> None:
        """Test detail sections are rendered for non-payment events."""
        from webhooks.models.rich_notification import DetailSection

        notification = RichNotification(
            type=NotificationType.FEEDBACK_RECEIVED,
            severity=NotificationSeverity.INFO,
            headline="NPS Response: 9",
            headline_icon="feedback",
            provider="intercom",
            provider_display="Intercom",
            customer=CustomerInfo(email="happy@example.com", name="Happy User"),
        )
        section = DetailSection(
            title="Feedback Details",
            icon="feedback",
            text="Great product! Love using it.",
        )
        section.add_field("Score", "9/10", icon="star")
        section.add_field("Category", "Product Feedback")
        notification.detail_sections.append(section)

        result = formatter.format(notification)

        # Find the detail section
        found_section = False
        for block in get_blocks(result):
            if block["type"] == "section":
                text = str(block.get("text", {}).get("text", ""))
                if "Feedback Details" in text:
                    found_section = True
                    assert "Score" in text
                    assert "9/10" in text
                    assert "Great product" in text
                    break

        assert found_section, "Detail section not found"

    def test_detail_section_with_accessory(
        self, formatter: SlackDestinationPlugin
    ) -> None:
        """Test detail section with accessory image."""
        from webhooks.models.rich_notification import DetailSection

        notification = RichNotification(
            type=NotificationType.FEATURE_ADOPTED,
            severity=NotificationSeverity.SUCCESS,
            headline="Feature Adopted - Dashboard",
            headline_icon="feature",
            provider="segment",
            provider_display="Segment",
            customer=CustomerInfo(email="user@example.com"),
        )
        section = DetailSection(
            title="Feature Usage",
            icon="feature",
            accessory_url="https://example.com/feature-icon.png",
        )
        section.add_field("Feature", "Advanced Dashboard")
        section.add_field("First Used", "Today")
        notification.detail_sections.append(section)

        result = formatter.format(notification)

        # Find section with accessory
        for block in get_blocks(result):
            if block["type"] == "section" and "Feature Usage" in str(
                block.get("text", {})
            ):
                assert "accessory" in block
                assert block["accessory"]["type"] == "image"
                assert (
                    block["accessory"]["image_url"]
                    == "https://example.com/feature-icon.png"
                )
                return

        pytest.fail("Detail section with accessory not found")

    def test_multiple_detail_sections(self, formatter: SlackDestinationPlugin) -> None:
        """Test multiple detail sections are rendered."""
        notification = RichNotification(
            type=NotificationType.SUPPORT_TICKET,
            severity=NotificationSeverity.INFO,
            headline="Support Ticket - Priority High",
            headline_icon="support",
            provider="zendesk",
            provider_display="Zendesk",
            customer=CustomerInfo(email="support@example.com"),
        )
        notification.add_detail_section(
            title="Ticket Info",
            icon="support",
            fields=[("Ticket ID", "#12345"), ("Priority", "High")],
        )
        notification.add_detail_section(
            title="Customer Context",
            icon="user",
            fields=[("Plan", "Enterprise"), ("Account Age", "2 years")],
        )

        result = formatter.format(notification)

        # Count detail sections (should have 2)
        section_texts = [
            b.get("text", {}).get("text", "")
            for b in get_blocks(result)
            if b["type"] == "section"
        ]
        ticket_found = any("Ticket Info" in t for t in section_texts)
        context_found = any("Customer Context" in t for t in section_texts)

        assert ticket_found, "Ticket Info section not found"
        assert context_found, "Customer Context section not found"


class TestSlackDestinationPluginNonPaymentEvents:
    """Test formatting for non-payment event types."""

    def test_usage_event_category_badge(
        self, formatter: SlackDestinationPlugin
    ) -> None:
        """Test usage event shows category badge instead of payment type."""
        notification = RichNotification(
            type=NotificationType.QUOTA_WARNING,
            severity=NotificationSeverity.WARNING,
            headline="Quota Warning - API Calls",
            headline_icon="quota",
            provider="segment",
            provider_display="Segment",
            customer=CustomerInfo(email="dev@example.com"),
            is_recurring=False,
        )

        result = formatter.format(notification)

        # Find provider badge context block
        for block in get_blocks(result):
            if block["type"] == "context":
                text = str(block.get("elements", [{}])[0].get("text", ""))
                if "Segment" in text:
                    # Should show Usage category, not payment type
                    assert "Usage" in text
                    assert "Recurring" not in text
                    assert "One-Time" not in text
                    return

        pytest.fail("Provider badge with category not found")

    def test_system_event_without_customer(
        self, formatter: SlackDestinationPlugin
    ) -> None:
        """Test system event can render without customer info."""
        notification = RichNotification(
            type=NotificationType.INTEGRATION_CONNECTED,
            severity=NotificationSeverity.SUCCESS,
            headline="Integration Connected - Stripe",
            headline_icon="check",
            provider="system",
            provider_display="System",
            customer=None,  # No customer for system events
        )

        result = formatter.format(notification)

        # Should render without error
        blocks = get_blocks(result)
        assert len(blocks) > 0

        # Should not have customer footer
        context_blocks = [b for b in blocks if b["type"] == "context"]
        for block in context_blocks:
            text = str(block.get("elements", [{}])[0].get("text", ""))
            assert "@" not in text  # No email in footer

    def test_customer_event_formatting(self, formatter: SlackDestinationPlugin) -> None:
        """Test customer event type formatting."""
        notification = RichNotification(
            type=NotificationType.CUSTOMER_CHURNED,
            severity=NotificationSeverity.ERROR,
            headline="Customer Churned - Acme Inc",
            headline_icon="warning",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(
                email="lost@acme.com",
                name="Lost Customer",
                ltv_display="$5.2k",
                status_flags=["at_risk"],
            ),
        )

        result = formatter.format(notification)

        # Should have error color
        assert get_color(result) == "#dc3545"

        # Customer footer should show at_risk flag
        context_blocks = [b for b in get_blocks(result) if b["type"] == "context"]
        customer_footer = context_blocks[-1]
        text = str(customer_footer.get("elements", [{}])[0].get("text", ""))
        assert "At Risk" in text


class TestSlackDestinationPluginMetadata:
    """Test metadata field handling."""

    def test_metadata_available_on_notification(self) -> None:
        """Test metadata field can be set and accessed."""
        notification = RichNotification(
            type=NotificationType.WEBHOOK_RECEIVED,
            severity=NotificationSeverity.INFO,
            headline="Webhook Received",
            headline_icon="integration",
            provider="api",
            provider_display="API",
            metadata={
                "webhook_id": "wh_123",
                "source_ip": "192.168.1.1",
                "payload_size": 1024,
            },
        )

        assert notification.metadata["webhook_id"] == "wh_123"
        assert notification.metadata["source_ip"] == "192.168.1.1"
        assert notification.metadata["payload_size"] == 1024

    def test_notification_category_property(self) -> None:
        """Test category property returns correct EventCategory."""
        from webhooks.models.rich_notification import EventCategory

        payment_notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="Payment",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
        )
        assert payment_notification.category == EventCategory.PAYMENT

        usage_notification = RichNotification(
            type=NotificationType.QUOTA_EXCEEDED,
            severity=NotificationSeverity.ERROR,
            headline="Quota",
            headline_icon="error",
            provider="segment",
            provider_display="Segment",
        )
        assert usage_notification.category == EventCategory.USAGE

        support_notification = RichNotification(
            type=NotificationType.SUPPORT_TICKET,
            severity=NotificationSeverity.INFO,
            headline="Ticket",
            headline_icon="support",
            provider="zendesk",
            provider_display="Zendesk",
        )
        assert support_notification.category == EventCategory.SUPPORT

    def test_is_payment_event_property(self) -> None:
        """Test is_payment_event property."""
        payment_notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="Payment",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
        )
        assert payment_notification.is_payment_event is True

        subscription_notification = RichNotification(
            type=NotificationType.SUBSCRIPTION_CREATED,
            severity=NotificationSeverity.SUCCESS,
            headline="Subscription",
            headline_icon="celebration",
            provider="stripe",
            provider_display="Stripe",
        )
        assert subscription_notification.is_payment_event is True

        usage_notification = RichNotification(
            type=NotificationType.FEATURE_ADOPTED,
            severity=NotificationSeverity.SUCCESS,
            headline="Feature",
            headline_icon="feature",
            provider="segment",
            provider_display="Segment",
        )
        assert usage_notification.is_payment_event is False

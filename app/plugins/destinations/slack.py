"""Slack destination plugin for notification delivery.

This module converts RichNotification objects into Slack Block Kit JSON
format and sends them via Slack's incoming webhook API.
"""

import logging
from typing import Any

import requests
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.destinations.base import BaseDestinationPlugin
from plugins.destinations.slack_utils import html_to_slack_mrkdwn
from webhooks.models.rich_notification import (
    ActionButton,
    CompanyInfo,
    CustomerInfo,
    DetailSection,
    InsightInfo,
    NotificationSeverity,
    NotificationType,
    PaymentInfo,
    PersonInfo,
    RichNotification,
)

logger = logging.getLogger(__name__)

# Default timeout for Slack API requests (seconds)
DEFAULT_TIMEOUT = 30

# Trial notification types - used to show "Trial" badge instead of payment type
TRIAL_NOTIFICATION_TYPES = {
    NotificationType.TRIAL_STARTED,
    NotificationType.TRIAL_ENDING,
    NotificationType.TRIAL_CONVERTED,
}

# Semantic icon to Slack emoji mapping
SLACK_ICONS: dict[str, str] = {
    # Headline icons
    "money": "moneybag",
    "error": "x",
    "celebration": "tada",
    "warning": "warning",
    "info": "information_source",
    # Insight icons
    "new": "new",
    "chart": "chart_with_upwards_trend",
    "trophy": "trophy",
    # Non-payment event icons
    "user": "bust_in_silhouette",
    "users": "busts_in_silhouette",
    "feedback": "speech_balloon",
    "support": "ticket",
    "feature": "sparkles",
    "usage": "bar_chart",
    "quota": "hourglass",
    "integration": "link",
    "system": "gear",
    "bell": "bell",
    "star": "star",
    "fire": "fire",
    "rocket": "rocket",
    "check": "white_check_mark",
    "calendar": "calendar",
    "clock": "clock",
    "email": "email",
    "phone": "phone",
    "globe": "globe_with_meridians",
    # Logistics icons
    "cart": "shopping_trolley",
    "package": "package",
    "truck": "truck",
}

# Provider display icons
PROVIDER_ICONS: dict[str, str] = {
    # Payment providers
    "shopify": "shopping_bags",
    "chargify": "dollar",
    "stripe": "credit_card",
    "stripe_customer": "credit_card",
    # Other providers
    "intercom": "speech_balloon",
    "zendesk": "ticket",
    "segment": "bar_chart",
    "mixpanel": "chart_with_upwards_trend",
    "amplitude": "chart_with_upwards_trend",
    "slack": "slack",
    "github": "octocat",
    "webhook": "link",
    "api": "gear",
    "system": "gear",
    "unknown": "globe_with_meridians",
}

# Default icon for unknown providers (must be a valid Slack emoji)
DEFAULT_PROVIDER_ICON = "globe_with_meridians"

# Payment method icons
PAYMENT_METHOD_ICONS: dict[str, str] = {
    # Card brands
    "visa": "credit_card",
    "mastercard": "credit_card",
    "amex": "credit_card",
    "discover": "credit_card",
    # Bank/ACH
    "bank_account": "bank",
    "us_bank_account": "bank",
    "ach": "bank",
    "sepa_debit": "bank",
    # Digital wallets
    "paypal": "paypal",
    "apple_pay": "apple",
    "google_pay": "iphone",
    "shop_pay": "shopping_bags",
}

# Severity to color mapping
SEVERITY_COLORS: dict[NotificationSeverity, str] = {
    NotificationSeverity.SUCCESS: "#28a745",  # Green
    NotificationSeverity.WARNING: "#ffc107",  # Yellow
    NotificationSeverity.ERROR: "#dc3545",  # Red
    NotificationSeverity.INFO: "#17a2b8",  # Blue
}


class SlackDestinationPlugin(BaseDestinationPlugin):
    """Format and send RichNotification as Slack Block Kit JSON.

    This plugin converts target-agnostic RichNotification objects
    into Slack's Block Kit format and delivers them via incoming webhooks.
    """

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Slack destination plugin.
        """
        return PluginMetadata(
            name="slack",
            display_name="Slack",
            version="1.0.0",
            description="Send notifications to Slack via incoming webhooks",
            plugin_type=PluginType.DESTINATION,
            capabilities={
                PluginCapability.RICH_FORMATTING,
                PluginCapability.ATTACHMENTS,
                PluginCapability.ACTIONS,
            },
            priority=100,
        )

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        """Initialize the Slack destination plugin.

        Args:
            timeout: Request timeout in seconds (default: 30).
        """
        self.timeout = timeout

    def format(self, n: RichNotification) -> dict[str, Any]:
        """Format notification as Slack Block Kit message.

        Args:
            n: RichNotification to format.

        Returns:
            Dict with 'blocks' and 'color' for Slack API.
        """
        blocks: list[dict[str, Any]] = []

        # Header with headline
        blocks.append(self._format_header(n))

        # Insight line (if present)
        if n.insight:
            blocks.append(self._format_insight(n.insight))

        # Provider badge (adapts based on event type)
        blocks.append(self._format_provider_badge(n))

        # Payment/order details (for payment events)
        if n.payment:
            blocks.append(self._format_payment_details(n))

        # Generic detail sections (for non-payment events or extras)
        for section in n.detail_sections:
            blocks.append(self._format_detail_section(section))

        # Divider before company/customer section
        blocks.append({"type": "divider"})

        # Company section with logo (if enriched)
        if n.company:
            blocks.append(self._format_company_section(n.company))
            # Add website & LinkedIn links below company section
            links_block = self._format_company_links(n.company)
            if links_block:
                blocks.append(links_block)

        # Person section (if enriched via Hunter.io)
        if n.person:
            person_blocks = self._format_person_section(n.person)
            blocks.extend(person_blocks)

        # Customer footer (optional - only shown when there's meaningful data)
        if n.customer:
            customer_footer = self._format_customer_footer(n.customer)
            if customer_footer:
                blocks.append(customer_footer)

        # Action buttons (if present)
        if n.actions:
            blocks.append(self._format_actions(n.actions))

        # Use attachments format for colored sidebar
        # Top-level "color" is invalid for incoming webhooks
        color = SEVERITY_COLORS.get(n.severity, "#17a2b8")
        return {
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                }
            ],
        }

    def send(self, formatted: Any, credentials: dict[str, Any]) -> bool:
        """Send formatted notification to Slack via webhook.

        Args:
            formatted: Slack Block Kit formatted message.
            credentials: Dictionary containing 'webhook_url'.

        Returns:
            True if message was sent successfully.

        Raises:
            ValueError: If webhook_url is missing from credentials.
            RuntimeError: If the request fails or times out.
        """
        webhook_url = credentials.get("webhook_url")
        if not webhook_url:
            raise ValueError("Missing 'webhook_url' in credentials")

        try:
            response = requests.post(
                webhook_url,
                json=formatted,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return True
        except requests.exceptions.Timeout:
            logger.error(
                "Slack request timed out",
                extra={"timeout": self.timeout},
            )
            raise RuntimeError("Slack request timed out") from None
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to send message to Slack",
                extra={"error": str(e)},
                exc_info=True,
            )
            raise RuntimeError("Failed to send notification to Slack") from e

    def _format_header(self, n: RichNotification) -> dict[str, Any]:
        """Format the notification header block.

        Args:
            n: RichNotification.

        Returns:
            Slack header block dict.
        """
        emoji_name = SLACK_ICONS.get(n.headline_icon, "bell")
        return {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":{emoji_name}: {n.headline}",
                "emoji": True,
            },
        }

    def _format_insight(self, insight: InsightInfo) -> dict[str, Any]:
        """Format the insight/milestone line.

        Args:
            insight: InsightInfo object.

        Returns:
            Slack context block dict.
        """
        emoji_name = SLACK_ICONS.get(insight.icon, "star")
        return {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":{emoji_name}: *{insight.text}*",
                }
            ],
        }

    def _format_provider_badge(self, n: RichNotification) -> dict[str, Any]:
        """Format the provider/source badge.

        Adapts based on whether this is a payment event or not.

        Args:
            n: RichNotification.

        Returns:
            Slack context block dict.
        """
        provider_emoji = PROVIDER_ICONS.get(n.provider, DEFAULT_PROVIDER_ICON)
        elements = [f":{provider_emoji}: {n.provider_display}"]

        # Only add payment-specific badges for payment events
        if n.is_payment_event:
            # Check for trial events - show "Trial" badge instead of payment type
            if n.type in TRIAL_NOTIFICATION_TYPES:
                elements.append(":rocket: Trial")
            # Add payment type (recurring/one-time) without extra emojis
            elif n.is_recurring:
                if n.billing_interval:
                    elements.append(f"Recurring ({n.billing_interval.title()})")
                else:
                    elements.append("Recurring")
            elif n.payment:
                elements.append("One-Time")

            # Add payment method if available (keep credit_card emoji for clarity)
            if n.payment and n.payment.payment_method:
                pm_emoji = PAYMENT_METHOD_ICONS.get(
                    n.payment.payment_method.lower(), "credit_card"
                )
                pm_display = n.payment.payment_method.title()
                if n.payment.card_last4:
                    pm_display += f" ••••{n.payment.card_last4}"
                elements.append(f":{pm_emoji}: {pm_display}")
        else:
            # For non-payment events, add category badge
            category = n.category.value.title()
            category_icons = {
                "usage": "bar_chart",
                "support": "ticket",
                "customer": "bust_in_silhouette",
                "system": "gear",
                "custom": "link",
            }
            cat_emoji = category_icons.get(n.category.value, "information_source")
            elements.append(f":{cat_emoji}: {category}")

        return {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": " • ".join(elements)},
            ],
        }

    def _format_payment_details(self, n: RichNotification) -> dict[str, Any]:
        """Format payment/order details section.

        Args:
            n: RichNotification with payment info.

        Returns:
            Slack section block dict.
        """
        payment = n.payment
        if not payment:
            return {"type": "section", "text": {"type": "mrkdwn", "text": ""}}

        # Check if this is e-commerce (has order number or line items)
        is_ecommerce = payment.order_number or payment.line_items

        if is_ecommerce:
            return self._format_ecommerce_details(payment)
        return self._format_subscription_details(payment)

    def _format_subscription_details(self, payment: PaymentInfo) -> dict[str, Any]:
        """Format SaaS subscription payment details.

        Args:
            payment: PaymentInfo object.

        Returns:
            Slack section block dict.
        """
        lines = ["*Payment Details*"]

        # Amount with ARR
        lines.append(f"*Amount:* {payment.format_amount_with_arr()}")

        if payment.plan_name:
            lines.append(f"*Plan:* {payment.plan_name}")
        if payment.subscription_id:
            lines.append(f"*Subscription:* #{payment.subscription_id}")
        if payment.failure_reason:
            lines.append(f":x: *Reason:* {payment.failure_reason}")

        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }

    def _format_ecommerce_details(self, payment: PaymentInfo) -> dict[str, Any]:
        """Format e-commerce order details with line items.

        Args:
            payment: PaymentInfo object.

        Returns:
            Slack section block dict.
        """
        order_display = payment.order_number or "N/A"
        lines = [f":shopping_trolley: *Order #{order_display}*"]

        # Amount
        lines.append(f"*Amount:* {payment.currency} {payment.amount:,.2f}")

        # Line items (max 5)
        has_many_items = False
        if payment.line_items:
            has_many_items = len(payment.line_items) > 3
            for item in payment.line_items[:5]:
                qty = item.get("quantity", 1)
                name = item.get("name", "Item")
                price = item.get("price", 0)
                lines.append(f"• {qty}x {name} (${price:.2f})")

            if len(payment.line_items) > 5:
                remaining = len(payment.line_items) - 5
                lines.append(f"_...and {remaining} more items_")

        block: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }

        # Make collapsible if many line items (shows "see more")
        if has_many_items:
            block["expand"] = False

        return block

    def _format_detail_section(self, section: DetailSection) -> dict[str, Any]:
        """Format a generic detail section.

        Args:
            section: DetailSection object.

        Returns:
            Slack section block dict.
        """
        icon_emoji = SLACK_ICONS.get(section.icon, "information_source")
        lines = [f":{icon_emoji}: *{section.title}*"]

        # Add fields
        for detail_field in section.fields:
            field_icon = ""
            if detail_field.icon:
                field_emoji = SLACK_ICONS.get(detail_field.icon, "")
                if field_emoji:
                    field_icon = f":{field_emoji}: "
            lines.append(f"{field_icon}*{detail_field.label}:* {detail_field.value}")

        # Add freeform text
        if section.text:
            lines.append(section.text)

        block: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }

        # Add accessory image if present
        if section.accessory_url:
            block["accessory"] = {
                "type": "image",
                "image_url": section.accessory_url,
                "alt_text": section.title,
            }

        return block

    def _format_company_section(self, company: CompanyInfo) -> dict[str, Any]:
        """Format company enrichment section with logo.

        Args:
            company: CompanyInfo object.

        Returns:
            Slack section block dict.
        """
        text_parts = [f":office: *{company.name}*"]

        # Company details line
        details: list[str] = []
        if company.industry:
            details.append(company.industry)
        if company.year_founded:
            details.append(f"Founded {company.year_founded}")
        if company.employee_count:
            details.append(f"{company.employee_count} employees")
        if details:
            text_parts.append(f"_{' • '.join(details)}_")

        # Description as blockquote
        if company.description:
            desc = html_to_slack_mrkdwn(company.description)
            text_parts.append(f">{desc}")

        block: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(text_parts)},
        }

        # Add logo as accessory
        if company.logo_url:
            block["accessory"] = {
                "type": "image",
                "image_url": company.logo_url,
                "alt_text": company.name,
            }

        return block

    def _format_company_links(self, company: CompanyInfo) -> dict[str, Any] | None:
        """Format company LinkedIn link as context block.

        Shows LinkedIn if available. Website link is omitted since it's
        redundant with the domain shown inline above.

        Args:
            company: CompanyInfo object.

        Returns:
            Slack context block dict, or None if no LinkedIn available.
        """
        if not company.linkedin_url:
            return None

        link_text = f":briefcase: <{company.linkedin_url}|LinkedIn>"
        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": link_text}],
        }

    def _format_person_section(self, person: PersonInfo) -> list[dict[str, Any]]:
        """Format person enrichment section (from Hunter.io).

        Displays person information from email enrichment, including
        name, job title, seniority, location, and social links.

        Args:
            person: PersonInfo object from Hunter.io enrichment.

        Returns:
            List of Slack blocks (section and optional context block).
        """
        blocks: list[dict[str, Any]] = []

        # Build main text content
        text_parts: list[str] = []

        # Person name with icon
        display_name = person.full_name or person.email
        text_parts.append(f":bust_in_silhouette: *{display_name}*")

        # Job info line (title + seniority)
        job_parts: list[str] = []
        if person.position:
            job_parts.append(person.position)
        if person.seniority:
            # Capitalize seniority for display (e.g., "senior" -> "Senior")
            job_parts.append(person.seniority.title())
        if job_parts:
            text_parts.append(f"_{' • '.join(job_parts)}_")

        # Location line
        if person.location:
            text_parts.append(f":round_pushpin: {person.location}")

        # Main section block
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(text_parts)},
            }
        )

        # Social links as context block
        links: list[str] = []
        if person.linkedin_url:
            links.append(f":briefcase: <{person.linkedin_url}|LinkedIn>")
        if person.twitter_handle:
            twitter_url = f"https://twitter.com/{person.twitter_handle}"
            links.append(f":bird: <{twitter_url}|Twitter>")
        if person.github_handle:
            github_url = f"https://github.com/{person.github_handle}"
            links.append(f":octocat: <{github_url}|GitHub>")

        if links:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": " | ".join(links)}],
                }
            )

        return blocks

    def _format_customer_footer(self, customer: CustomerInfo) -> dict[str, Any] | None:
        """Format customer info footer.

        Args:
            customer: CustomerInfo object.

        Returns:
            Slack context block dict, or None if no meaningful data.
        """
        elements: list[str] = []

        # Email
        if customer.email:
            elements.append(f":bust_in_silhouette: {customer.email}")

        # Name if no email
        if not customer.email and customer.name:
            elements.append(f":bust_in_silhouette: {customer.name}")

        # Tenure (no emoji for cleaner look)
        if customer.tenure_display:
            elements.append(customer.tenure_display)

        # LTV (no emoji for cleaner look)
        if customer.ltv_display:
            elements.append(f"{customer.ltv_display} LTV")

        # Orders count (no emoji for cleaner look)
        if customer.orders_count:
            elements.append(f"{customer.orders_count} orders")

        # Status flags
        for flag in customer.status_flags:
            if flag == "at_risk":
                elements.append(":rotating_light: *At Risk*")
            elif flag == "vip":
                elements.append(":star: *VIP*")

        # Return None if no meaningful customer data to display
        if not elements:
            return None

        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " • ".join(elements)}],
        }

    def _format_actions(self, actions: list[ActionButton]) -> dict[str, Any]:
        """Format action buttons.

        Args:
            actions: List of ActionButton objects.

        Returns:
            Slack actions block dict.
        """
        button_elements: list[dict[str, Any]] = []

        for action in actions[:5]:  # Slack limits to 5 buttons
            button: dict[str, Any] = {
                "type": "button",
                "text": {"type": "plain_text", "text": action.text, "emoji": True},
                "url": action.url,
            }

            # Map style to Slack style
            if action.style == "primary":
                button["style"] = "primary"
            elif action.style == "danger":
                button["style"] = "danger"
            # "default" has no style attribute in Slack

            button_elements.append(button)

        return {
            "type": "actions",
            "elements": button_elements,
        }

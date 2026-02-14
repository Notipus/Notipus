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
    SentimentInfo,
    TicketInfo,
)

logger = logging.getLogger(__name__)

# Default timeout for Slack API requests (seconds)
DEFAULT_TIMEOUT = 30

# Slack API endpoint for posting messages
SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"

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
    "cart": "shopping_cart",
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
        blocks = self._build_notification_blocks(n)
        return {
            "blocks": blocks,
            "color": SEVERITY_COLORS.get(n.severity, "#17a2b8"),
        }

    def _build_notification_blocks(self, n: RichNotification) -> list[dict[str, Any]]:
        """Build all blocks for a notification.

        Args:
            n: RichNotification to build blocks for.

        Returns:
            List of Slack block dicts.
        """
        blocks: list[dict[str, Any]] = []

        # Header section blocks
        self._add_header_blocks(blocks, n)

        # Content section blocks
        self._add_content_blocks(blocks, n)

        # Footer section blocks
        blocks.append({"type": "divider"})
        self._add_footer_blocks(blocks, n)

        return blocks

    def _add_header_blocks(
        self, blocks: list[dict[str, Any]], n: RichNotification
    ) -> None:
        """Add header section blocks (sentiment, headline, insight, provider)."""
        if n.sentiment:
            blocks.append(self._format_sentiment_header(n.sentiment))
        blocks.append(self._format_header(n))
        if n.insight:
            blocks.append(self._format_insight(n.insight))
        blocks.append(self._format_provider_badge(n))

    def _add_content_blocks(
        self, blocks: list[dict[str, Any]], n: RichNotification
    ) -> None:
        """Add content section blocks (ticket, payment, detail sections)."""
        if n.ticket:
            blocks.append(self._format_ticket_details(n.ticket))
        if n.payment:
            blocks.append(self._format_payment_details(n))
        for section in n.detail_sections:
            blocks.append(self._format_detail_section(section))

    def _add_footer_blocks(
        self, blocks: list[dict[str, Any]], n: RichNotification
    ) -> None:
        """Add footer section blocks (company, person, customer, actions)."""
        if n.company:
            blocks.append(self._format_company_section(n.company))
            links_block = self._format_company_links(n.company)
            if links_block:
                blocks.append(links_block)
        # Person section (if enriched via Hunter.io)
        if n.person:
            person_blocks = self._format_person_section(n.person)
            blocks.extend(person_blocks)
        if n.customer:
            customer_footer = self._format_customer_footer(n.customer)
            if customer_footer:
                blocks.append(customer_footer)
        if n.actions:
            blocks.append(self._format_actions(n.actions))

    def send(
        self,
        formatted: Any,
        credentials: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send formatted notification to Slack via webhook or API.

        When using incoming webhooks, thread support is limited.
        For full thread support, use chat.postMessage with a bot token.

        Args:
            formatted: Slack Block Kit formatted message.
            credentials: Dictionary containing 'webhook_url' or 'bot_token'.
            options: Optional dict with 'thread_ts' for threading and 'channel'.

        Returns:
            Dict with 'success', and optionally 'thread_ts', 'channel', 'ts'.

        Raises:
            ValueError: If webhook_url is missing from credentials.
            RuntimeError: If the request fails or times out.
        """
        options = options or {}
        thread_ts = options.get("thread_ts")
        channel = options.get("channel")
        bot_token = credentials.get("bot_token")
        webhook_url = credentials.get("webhook_url")

        # If we have a bot token and channel, use chat.postMessage for threading
        if bot_token and channel:
            return self._send_via_api(formatted, bot_token, channel, thread_ts)

        # Fall back to webhook (limited thread support)
        if not webhook_url:
            raise ValueError("Missing 'webhook_url' or 'bot_token' in credentials")

        return self._send_via_webhook(formatted, webhook_url, thread_ts)

    def _send_via_webhook(
        self,
        formatted: dict[str, Any],
        webhook_url: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Send message via incoming webhook.

        Note: Incoming webhooks have limited support for threading.
        The message will be sent but thread_ts may be ignored depending
        on the webhook configuration.

        Args:
            formatted: Slack Block Kit formatted message.
            webhook_url: Slack incoming webhook URL.
            thread_ts: Optional thread timestamp for reply.

        Returns:
            Dict with 'success' bool. Webhook doesn't return message details.
        """
        payload = formatted.copy()

        # Add thread_ts if provided (may be ignored by webhook)
        if thread_ts:
            payload["thread_ts"] = thread_ts

        try:
            response = requests.post(
                webhook_url,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return {"success": True}
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

    def _send_via_api(
        self,
        formatted: dict[str, Any],
        bot_token: str,
        channel: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Send message via Slack API (chat.postMessage).

        This method provides full thread support and returns message details.

        Args:
            formatted: Slack Block Kit formatted message.
            bot_token: Slack bot OAuth token.
            channel: Channel ID to post to.
            thread_ts: Optional thread timestamp for reply.

        Returns:
            Dict with 'success', 'ts' (message timestamp), 'channel',
            and 'thread_ts' if this created/replied to a thread.
        """
        payload: dict[str, Any] = {
            "channel": channel,
            "blocks": formatted.get("blocks", []),
        }

        # Add attachments for color sidebar if present
        if "color" in formatted:
            payload["attachments"] = [
                {
                    "color": formatted["color"],
                    "blocks": formatted.get("blocks", []),
                }
            ]
            # When using attachments, blocks go inside the attachment
            del payload["blocks"]

        # Add thread_ts for reply
        if thread_ts:
            payload["thread_ts"] = thread_ts

        try:
            response = requests.post(
                SLACK_API_POST_MESSAGE,
                headers={
                    "Authorization": f"Bearer {bot_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                error = data.get("error", "Unknown error")
                logger.error(f"Slack API error: {error}")
                raise RuntimeError(f"Slack API error: {error}")

            result: dict[str, Any] = {
                "success": True,
                "ts": data.get("ts"),
                "channel": data.get("channel"),
            }

            # Return thread_ts - either the one we replied to or the new message ts
            if thread_ts:
                result["thread_ts"] = thread_ts
            else:
                # New message becomes the thread parent
                result["thread_ts"] = data.get("ts")

            return result

        except requests.exceptions.Timeout:
            logger.error(
                "Slack API request timed out",
                extra={"timeout": self.timeout},
            )
            raise RuntimeError("Slack API request timed out") from None
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to send message via Slack API",
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
        lines = [f":shopping_cart: *Order #{order_display}*"]

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

    def _format_ticket_details(self, ticket: TicketInfo) -> dict[str, Any]:
        """Format support ticket details section.

        Args:
            ticket: TicketInfo object.

        Returns:
            Slack section block dict.
        """
        lines = [f":ticket: *Ticket #{ticket.ticket_id}*"]

        # Status and priority badges
        badges = self._format_ticket_badges(ticket)
        if badges:
            lines.append(badges)

        # Core ticket content
        self._add_ticket_content_lines(lines, ticket)

        # Metadata fields
        self._add_ticket_metadata_lines(lines, ticket)

        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }

    def _format_ticket_badges(self, ticket: TicketInfo) -> str:
        """Format status and priority badges for a ticket.

        Args:
            ticket: TicketInfo object.

        Returns:
            Formatted badge string or empty string.
        """
        badges = []
        if ticket.status:
            emoji = self._get_status_emoji(ticket.status)
            badges.append(f"{emoji} {ticket.status.title()}")
        if ticket.priority:
            emoji = self._get_priority_emoji(ticket.priority)
            badges.append(f"{emoji} {ticket.priority.title()}")
        return " • ".join(badges)

    def _add_ticket_content_lines(self, lines: list[str], ticket: TicketInfo) -> None:
        """Add subject, description, and comment lines to ticket output."""
        if ticket.subject:
            lines.append(f"*Subject:* {ticket.subject}")

        if ticket.description:
            lines.append(f">{self._truncate_text(ticket.description, 200)}")

        if ticket.latest_comment:
            comment = self._truncate_text(ticket.latest_comment, 200)
            lines.append(f"\n:speech_balloon: *Latest Comment:*\n>{comment}")

    def _add_ticket_metadata_lines(self, lines: list[str], ticket: TicketInfo) -> None:
        """Add requester, assignee, channel, and tags lines to ticket output."""
        requester = ticket.requester_name or ticket.requester_email
        if requester:
            lines.append(f"*Requester:* {requester}")

        if ticket.assignee_name:
            lines.append(f"*Assignee:* {ticket.assignee_name}")

        if ticket.channel:
            lines.append(f"*Channel:* {ticket.channel}")

        if ticket.tags:
            lines.append(f"*Tags:* {self._format_tag_list(ticket.tags)}")

    def _truncate_text(self, text: str, max_length: int) -> str:
        """Truncate text with ellipsis if needed.

        Args:
            text: Text to truncate.
            max_length: Maximum length before truncation.

        Returns:
            Truncated text with ellipsis or original text.
        """
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."

    def _format_tag_list(self, tags: list[str], max_tags: int = 5) -> str:
        """Format a list of tags with overflow indicator.

        Args:
            tags: List of tag strings.
            max_tags: Maximum tags to show before overflow.

        Returns:
            Formatted tag string.
        """
        tag_list = ", ".join(tags[:max_tags])
        if len(tags) > max_tags:
            tag_list += f" (+{len(tags) - max_tags} more)"
        return tag_list

    def _format_sentiment_header(self, sentiment: SentimentInfo) -> dict[str, Any]:
        """Format sentiment analysis header block.

        Shows sentiment, urgency, and topics at the top of support notifications.

        Args:
            sentiment: SentimentInfo object.

        Returns:
            Slack context block dict.
        """
        elements = []

        # Sentiment emoji and label
        sentiment_config = {
            "positive": (":smile:", "Positive"),
            "negative": (":worried:", "Negative"),
            "neutral": (":neutral_face:", "Neutral"),
        }
        emoji, label = sentiment_config.get(
            sentiment.sentiment.lower(), (":question:", sentiment.sentiment.title())
        )
        elements.append(f"{emoji} *{label}*")

        # Urgency indicator
        urgency_config = {
            "high": ":red_circle:",
            "medium": ":large_orange_circle:",
            "low": ":large_green_circle:",
        }
        urgency_emoji = urgency_config.get(sentiment.urgency.lower(), ":white_circle:")
        elements.append(f"{urgency_emoji} {sentiment.urgency.title()} Urgency")

        # Topics (first 3)
        if sentiment.topics:
            topic_icons = {
                "billing": ":credit_card:",
                "technical": ":wrench:",
                "account": ":bust_in_silhouette:",
                "feature": ":sparkles:",
                "bug": ":bug:",
            }
            for topic in sentiment.topics[:3]:
                topic_lower = topic.lower()
                icon = topic_icons.get(topic_lower, ":label:")
                elements.append(f"{icon} {topic.title()}")

        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " • ".join(elements)}],
        }

    def _get_status_emoji(self, status: str) -> str:
        """Get emoji for ticket status.

        Args:
            status: Ticket status string.

        Returns:
            Slack emoji string.
        """
        status_emojis = {
            "new": ":new:",
            "open": ":hourglass:",
            "pending": ":clock3:",
            "on_hold": ":pause_button:",
            "solved": ":white_check_mark:",
            "closed": ":lock:",
        }
        return status_emojis.get(status.lower(), ":question:")

    def _get_priority_emoji(self, priority: str) -> str:
        """Get emoji for ticket priority.

        Args:
            priority: Ticket priority string.

        Returns:
            Slack emoji string.
        """
        priority_emojis = {
            "urgent": ":rotating_light:",
            "high": ":red_circle:",
            "normal": ":large_blue_circle:",
            "low": ":white_circle:",
        }
        return priority_emojis.get(priority.lower(), ":white_circle:")

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

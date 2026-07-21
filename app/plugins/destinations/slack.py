"""Slack destination plugin for notification delivery.

This module converts RichNotification objects into Slack Block Kit JSON
format and sends them via Slack's incoming webhook API.
"""

import logging
import math
from typing import Any
from urllib.parse import quote

import requests
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.destinations.base import BaseDestinationPlugin
from plugins.destinations.slack_utils import (
    html_to_slack_mrkdwn,
    safe_mrkdwn,
    safe_mrkdwn_link,
)
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
from webhooks.utils.currency import format_money

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

# Badges appended after the customer email for domain-type tags.
# Keys are EmailTag values from webhooks.utils.email_classifier.
EMAIL_TAG_BADGES: dict[str, str] = {
    "government": ":classical_building: Government",
    "education": ":mortar_board: Education",
    "military": ":shield: Military",
    "healthcare": ":hospital: Healthcare",
    "free": ":mailbox: Free email",
    "disposable": ":wastebasket: Disposable email",
}

# Company descriptions are enrichment boilerplate; anything longer than
# this dominates the message and can push the customer footer and action
# buttons behind Slack's "Show more" collapse.
MAX_DESCRIPTION_LENGTH = 160


def _coerce_float(value: Any, default: float = 0.0) -> float:
    """Coerce a possibly non-numeric value to float.

    Webhook payloads sometimes deliver numeric fields as strings (e.g.
    Shopify sends prices like ``"19.99"``). Formatting such a value with a
    numeric format spec would raise ``ValueError`` and abort the whole
    notification, so fall back to a default when coercion fails. Non-finite
    values ("nan", "inf") coerce successfully but would render as literal
    "nan"/"inf", so they are also treated as invalid and replaced with the
    default.

    Args:
        value: The value to coerce (str, int, float, or None).
        default: Value returned when coercion fails.

    Returns:
        The coerced finite float, or ``default`` on failure.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _truncate_mrkdwn(text: str, max_length: int) -> str:
    """Truncate mrkdwn text without breaking link syntax.

    Cutting inside a ``<url|label>`` link would leave a dangling ``<``
    that Slack renders as broken markup, so a partial link at the cut
    point is dropped entirely. The cut then backs up to a word boundary
    and an ellipsis is appended.

    Args:
        text: Already-sanitized mrkdwn text.
        max_length: Maximum length of the result, including the ellipsis.

    Returns:
        The text unchanged if it fits, otherwise a truncated version
        ending in an ellipsis.
    """
    if len(text) <= max_length:
        return text
    cut = text[: max_length - 1]
    open_bracket = cut.rfind("<")
    if open_bracket > cut.rfind(">"):
        cut = cut[:open_bracket]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "…"


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
            Dict with fallback 'text' and 'attachments' for Slack API.
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
            payment_block = self._format_payment_details(n)
            if payment_block:
                blocks.append(payment_block)

        # Generic detail sections (for non-payment events or extras)
        for section in n.detail_sections:
            blocks.append(self._format_detail_section(section))

        # Company/person/customer blocks, preceded by a divider only when
        # at least one is present - sparse events (e.g. an integration
        # error with no customer) must not end on a dangling rule.
        tail = self._format_tail_blocks(n)
        if tail:
            blocks.append({"type": "divider"})
            blocks.extend(tail)

        # Action buttons (if present)
        if n.actions:
            blocks.append(self._format_actions(n.actions))

        # Use attachments format for colored sidebar
        # Top-level "color" is invalid for incoming webhooks
        color = SEVERITY_COLORS.get(n.severity, "#17a2b8")
        return {
            "text": self._format_fallback_text(n),
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                }
            ],
        }

    def _format_tail_blocks(self, n: RichNotification) -> list[dict[str, Any]]:
        """Build the company/person/customer blocks shown after the divider.

        Args:
            n: RichNotification.

        Returns:
            List of Slack blocks; empty when no enrichment or customer
            data is available.
        """
        tail: list[dict[str, Any]] = []

        # Company section with logo (if enriched)
        if n.company:
            tail.append(self._format_company_section(n.company))
            # Add LinkedIn link below company section
            links_block = self._format_company_links(n.company)
            if links_block:
                tail.append(links_block)

        # Person section (if enriched via Hunter.io). It absorbs the
        # customer facts (email, tenure, LTV, flags) so the reader gets
        # one person block instead of two overlapping ones.
        if n.person:
            tail.extend(self._format_person_section(n.person, n.customer))
        # Standalone customer footer (only shown when there's meaningful
        # data and no person section already carries it)
        elif n.customer:
            customer_footer = self._format_customer_footer(n.customer)
            if customer_footer:
                tail.append(customer_footer)

        return tail

    def _format_fallback_text(self, n: RichNotification) -> str:
        """Build the top-level fallback text for the message.

        Slack renders this text in mobile push banners, desktop
        notifications, and the channel sidebar preview. Attachment
        blocks are invisible on those surfaces, so without it every
        event shows up as a generic "sent a message" line.

        Args:
            n: RichNotification.

        Returns:
            Plain one-line summary, sanitized for mrkdwn.
        """
        parts = [n.headline]
        if n.insight:
            parts.append(n.insight.text)
        # Collapse all whitespace (payload-derived strings like failure
        # reasons can contain newlines) so the preview stays one line.
        one_line = " ".join(" — ".join(parts).split())
        fallback: str = safe_mrkdwn(one_line)
        return fallback

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

        Insights (milestones, failure reasons, retry dates) are the
        highest-value line in the message, so they render as a full-size
        section block rather than muted context text.

        Args:
            insight: InsightInfo object.

        Returns:
            Slack section block dict.
        """
        emoji_name = SLACK_ICONS.get(insight.icon, "star")
        return {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":{emoji_name}: *{safe_mrkdwn(insight.text)}*",
            },
        }

    def _format_provider_badge(self, n: RichNotification) -> dict[str, Any]:
        """Format the provider/source badge.

        Adapts based on whether this is a payment event or not. Kept
        emoji-free so the headline severity emoji stays the only icon
        above the fold.

        Args:
            n: RichNotification.

        Returns:
            Slack context block dict.
        """
        elements = [n.provider_display]

        # Only add payment-specific badges for payment events
        if n.is_payment_event:
            # Check for trial events - show "Trial" badge instead of payment type
            if n.type in TRIAL_NOTIFICATION_TYPES:
                elements.append("Trial")
            # Add payment type (recurring/one-time)
            elif n.is_recurring:
                if n.billing_interval:
                    elements.append(f"Recurring ({n.billing_interval.title()})")
                else:
                    elements.append("Recurring")
            elif n.payment:
                elements.append("One-Time")

            # Add payment method if available
            if n.payment and n.payment.payment_method:
                pm_display = n.payment.payment_method.title()
                if n.payment.card_last4:
                    pm_display += f" ••••{n.payment.card_last4}"
                elements.append(pm_display)
        else:
            # For non-payment events, add category badge
            elements.append(n.category.value.title())

        # Provider display, billing interval, and payment method all
        # derive from webhook payload data, so each element is sanitized
        # before joining into mrkdwn.
        badge_text = " • ".join(safe_mrkdwn(e) for e in elements)
        return {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": badge_text},
            ],
        }

    def _format_payment_details(self, n: RichNotification) -> dict[str, Any] | None:
        """Format payment/order details section.

        Args:
            n: RichNotification with payment info.

        Returns:
            Slack section block dict, or None when there is nothing to
            show beyond what the headline already carries.
        """
        payment = n.payment
        if not payment:
            return None

        # Check if this is e-commerce (has order number or line items)
        is_ecommerce = payment.order_number or payment.line_items

        if is_ecommerce:
            return self._format_ecommerce_details(payment)
        return self._format_subscription_details(payment, n.headline)

    def _format_subscription_details(
        self, payment: PaymentInfo, headline: str
    ) -> dict[str, Any] | None:
        """Format SaaS subscription payment details as a two-column grid.

        The headline usually carries the base amount already (e.g.
        "$299.00 received"), so repeating it here would be noise - in
        that case only the ARR is surfaced. Headlines without an amount
        (e.g. "New subscription!") get the full amount-with-ARR field so
        the money is never lost.

        Args:
            payment: PaymentInfo object.
            headline: The notification headline, used to detect whether
                the amount is already visible.

        Returns:
            Slack section block dict with a ``fields`` grid, or None
            when every detail would duplicate the headline.
        """
        fields: list[str] = []

        if payment.plan_name:
            fields.append(f"*Plan*\n{safe_mrkdwn(payment.plan_name)}")

        amount_display: str = format_money(payment.amount, payment.currency)
        arr = payment.get_arr()
        if amount_display not in headline:
            fields.append(f"*Amount*\n{payment.format_amount_with_arr()}")
        elif arr is not None:
            fields.append(f"*ARR*\n{format_money(arr, payment.currency, 0)}")

        if payment.subscription_id:
            fields.append(f"*Subscription*\n#{safe_mrkdwn(payment.subscription_id)}")

        block: dict[str, Any] = {"type": "section"}
        if fields:
            block["fields"] = [{"type": "mrkdwn", "text": f} for f in fields]
        # The failure insight does not always carry the reason (it may
        # show retry info instead), so the reason stays here as well.
        if payment.failure_reason:
            block["text"] = {
                "type": "mrkdwn",
                "text": f":x: *Reason:* {safe_mrkdwn(payment.failure_reason)}",
            }
        if "fields" not in block and "text" not in block:
            return None
        return block

    def _format_ecommerce_details(self, payment: PaymentInfo) -> dict[str, Any]:
        """Format e-commerce order details with line items.

        Args:
            payment: PaymentInfo object.

        Returns:
            Slack section block dict.
        """
        order_display = (
            safe_mrkdwn(payment.order_number) if payment.order_number else "N/A"
        )
        lines = [f":shopping_trolley: *Order #{order_display}*"]

        # Amount (coerce defensively; some providers send amounts as strings)
        amount = _coerce_float(payment.amount)
        lines.append(f"*Amount:* {safe_mrkdwn(payment.currency)} {amount:,.2f}")

        # Line items (max 5)
        has_many_items = False
        if payment.line_items:
            has_many_items = len(payment.line_items) > 3
            for item in payment.line_items[:5]:
                # qty/price come raw from the webhook and may be strings.
                qty = _coerce_float(item.get("quantity", 1), default=1.0)
                name = safe_mrkdwn(str(item.get("name", "Item")))
                price = _coerce_float(item.get("price", 0))
                # Render whole quantities without a trailing ".0".
                qty_display = f"{qty:g}"
                lines.append(f"• {qty_display}x {name} (${price:.2f})")

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

        The company domain is linked inline next to the name, so the
        website does not need its own action button.

        Args:
            company: CompanyInfo object.

        Returns:
            Slack section block dict.
        """
        name_line = f"*{safe_mrkdwn(company.name)}*"
        if company.domain:
            domain_link = safe_mrkdwn_link(f"https://{company.domain}", company.domain)
            if domain_link:
                name_line += f" · {domain_link}"
        text_parts = [name_line]

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

        # Description as blockquote, truncated so enrichment boilerplate
        # cannot dominate the message or push the actions behind Slack's
        # "Show more" collapse.
        if company.description:
            desc = html_to_slack_mrkdwn(company.description)
            desc = _truncate_mrkdwn(desc, MAX_DESCRIPTION_LENGTH)
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
        link_text = safe_mrkdwn_link(company.linkedin_url, "LinkedIn")
        if not link_text:
            return None

        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": link_text}],
        }

    def _format_person_section(
        self, person: PersonInfo, customer: CustomerInfo | None = None
    ) -> list[dict[str, Any]]:
        """Format person enrichment section (from Hunter.io).

        Displays person information from email enrichment, including
        name, job title, seniority, location, and social links. When
        customer info is passed, its facts line (email, tenure, LTV,
        flags) is folded in below the person so the message shows a
        single merged person block instead of a separate footer.

        Args:
            person: PersonInfo object from Hunter.io enrichment.
            customer: Optional CustomerInfo to merge into this section.

        Returns:
            List of Slack blocks (section and optional context blocks).
        """
        blocks: list[dict[str, Any]] = []

        # Build main text content
        text_parts: list[str] = []

        # Person name with icon, job info (title + seniority) inline.
        # Hunter.io enrichment data is third-party input, so every field
        # is sanitized before interpolation into mrkdwn.
        display_name = person.full_name or person.email
        name_line = f":bust_in_silhouette: *{safe_mrkdwn(display_name)}*"
        job_parts: list[str] = []
        if person.position:
            job_parts.append(safe_mrkdwn(person.position))
        if person.seniority:
            # Capitalize seniority for display (e.g., "senior" -> "Senior")
            job_parts.append(safe_mrkdwn(person.seniority.title()))
        if job_parts:
            name_line += f" — _{' • '.join(job_parts)}_"
        text_parts.append(name_line)

        # Location line
        if person.location:
            text_parts.append(f":round_pushpin: {safe_mrkdwn(person.location)}")

        # Main section block
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(text_parts)},
            }
        )

        # Customer facts folded in under the person (icon suppressed -
        # the name line above already carries it)
        if customer:
            customer_facts = self._format_customer_footer(customer, include_icon=False)
            if customer_facts:
                blocks.append(customer_facts)

        # Social links as context block
        links_block = self._format_person_links(person)
        if links_block:
            blocks.append(links_block)

        return blocks

    def _format_person_links(self, person: PersonInfo) -> dict[str, Any] | None:
        """Format the person's social links as a context block.

        Args:
            person: PersonInfo object from Hunter.io enrichment.

        Returns:
            Slack context block dict, or None when no links available.
        """
        # Enrichment URLs are untrusted; handles are percent-encoded so
        # they cannot smuggle mrkdwn or path segments into the URL.
        candidates = [
            (person.linkedin_url, "LinkedIn"),
            (
                f"https://twitter.com/{quote(person.twitter_handle, safe='')}"
                if person.twitter_handle
                else None,
                "Twitter",
            ),
            (
                f"https://github.com/{quote(person.github_handle, safe='')}"
                if person.github_handle
                else None,
                "GitHub",
            ),
        ]
        links = [
            link for url, label in candidates if (link := safe_mrkdwn_link(url, label))
        ]

        if not links:
            return None
        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(links)}],
        }

    def _format_customer_footer(
        self, customer: CustomerInfo, include_icon: bool = True
    ) -> dict[str, Any] | None:
        """Format customer info footer.

        Args:
            customer: CustomerInfo object.
            include_icon: Whether to prefix the email/name with the
                person icon. Pass False when a person section already
                shows it, so the icon appears once per message.

        Returns:
            Slack context block dict, or None if no meaningful data.
        """
        elements: list[str] = []
        icon_prefix = ":bust_in_silhouette: " if include_icon else ""

        # Email, with compact domain-type badges (e.g.
        # ":bust_in_silhouette: jane@stanford.edu · :mortar_board: Education")
        if customer.email:
            email_parts = [f"{icon_prefix}{safe_mrkdwn(customer.email)}"]
            email_parts.extend(
                EMAIL_TAG_BADGES[tag]
                for tag in customer.email_tags
                if tag in EMAIL_TAG_BADGES
            )
            elements.append(" · ".join(email_parts))

        # Name if no email
        if not customer.email and customer.name:
            elements.append(f"{icon_prefix}{safe_mrkdwn(customer.name)}")

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

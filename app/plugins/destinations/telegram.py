"""Telegram destination plugin for notification delivery.

This module converts RichNotification objects into Telegram HTML
format and sends them via the Telegram Bot API.
"""

import json
import logging
from typing import Any

import requests
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.destinations.base import BaseDestinationPlugin
from webhooks.models.rich_notification import (
    ActionButton,
    CompanyInfo,
    CustomerInfo,
    DetailSection,
    InsightInfo,
    PaymentInfo,
    RichNotification,
)

logger = logging.getLogger(__name__)

# Default timeout for Telegram API requests (seconds)
DEFAULT_TIMEOUT = 30

# Telegram Bot API base URL
TELEGRAM_API_BASE = "https://api.telegram.org/bot"

# Semantic icon to Unicode emoji mapping
TELEGRAM_ICONS: dict[str, str] = {
    # Headline icons
    "money": "💰",
    "error": "❌",
    "celebration": "🎉",
    "warning": "⚠️",
    "info": "ℹ️",
    # Insight icons
    "new": "🆕",
    "chart": "📈",
    "trophy": "🏆",
    # Non-payment event icons
    "user": "👤",
    "users": "👥",
    "feedback": "💬",
    "support": "🎫",
    "feature": "✨",
    "usage": "📊",
    "quota": "⏳",
    "integration": "🔗",
    "system": "⚙️",
    "bell": "🔔",
    "star": "⭐",
    "fire": "🔥",
    "rocket": "🚀",
    "check": "✅",
    "calendar": "📅",
    "clock": "🕐",
    "email": "📧",
    "phone": "📞",
    "globe": "🌐",
    # Logistics icons
    "cart": "🛒",
    "package": "📦",
    "truck": "🚚",
}

# Provider display icons
PROVIDER_ICONS: dict[str, str] = {
    # Payment providers
    "shopify": "🛍️",
    "chargify": "💵",
    "stripe": "💳",
    "stripe_customer": "💳",
    # Other providers
    "intercom": "💬",
    "zendesk": "🎫",
    "segment": "📊",
    "mixpanel": "📈",
    "amplitude": "📈",
    "slack": "💬",
    "github": "🐙",
    "webhook": "🔗",
    "api": "⚙️",
    "system": "⚙️",
    "unknown": "🌐",
}

# Default icon for unknown providers
DEFAULT_PROVIDER_ICON = "🌐"

# Payment method icons
PAYMENT_METHOD_ICONS: dict[str, str] = {
    # Card brands
    "visa": "💳",
    "mastercard": "💳",
    "amex": "💳",
    "discover": "💳",
    # Bank/ACH
    "bank_account": "🏦",
    "us_bank_account": "🏦",
    "ach": "🏦",
    "sepa_debit": "🏦",
    # Digital wallets
    "paypal": "💳",
    "apple_pay": "🍎",
    "google_pay": "📱",
    "shop_pay": "🛍️",
}

# Category icons for non-payment events
CATEGORY_ICONS: dict[str, str] = {
    "usage": "📊",
    "support": "🎫",
    "customer": "👤",
    "system": "⚙️",
    "custom": "🔗",
}


class TelegramDestinationPlugin(BaseDestinationPlugin):
    """Format and send RichNotification as Telegram HTML message.

    This plugin converts target-agnostic RichNotification objects
    into Telegram's HTML format and delivers them via the Bot API.
    """

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Telegram destination plugin.
        """
        return PluginMetadata(
            name="telegram",
            display_name="Telegram",
            version="1.0.0",
            description="Send notifications to Telegram via Bot API",
            plugin_type=PluginType.DESTINATION,
            capabilities={
                PluginCapability.RICH_FORMATTING,
                PluginCapability.ACTIONS,
            },
            priority=100,
        )

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        """Initialize the Telegram destination plugin.

        Args:
            timeout: Request timeout in seconds (default: 30).
        """
        self.timeout = timeout

    def format(self, n: RichNotification) -> dict[str, Any]:
        """Format notification as Telegram HTML message.

        Args:
            n: RichNotification to format.

        Returns:
            Dict with 'text', 'parse_mode', and optional 'reply_markup'.
        """
        lines: list[str] = []

        # Header with headline
        lines.append(self._format_header(n))

        # Insight line (if present)
        if n.insight:
            lines.append(self._format_insight(n.insight))
            lines.append("")

        # Provider badge
        lines.append(self._format_provider_badge(n))
        lines.append("")

        # Payment/order details (for payment events)
        if n.payment:
            lines.append(self._format_payment_details(n))
            lines.append("")

        # Generic detail sections (for non-payment events or extras)
        for section in n.detail_sections:
            lines.append(self._format_detail_section(section))
            lines.append("")

        # Divider
        lines.append("━━━━━━━━━━")
        lines.append("")

        # Company section with info (if enriched)
        if n.company:
            lines.append(self._format_company_section(n.company))
            links = self._format_company_links(n.company)
            if links:
                lines.append(links)
            lines.append("")

        # Customer footer
        if n.customer:
            customer_footer = self._format_customer_footer(n.customer)
            if customer_footer:
                lines.append(customer_footer)

        result: dict[str, Any] = {
            "text": "\n".join(lines).strip(),
            "parse_mode": "HTML",
        }

        # Action buttons (if present)
        if n.actions:
            result["reply_markup"] = self._format_actions(n.actions)

        return result

    def send(self, formatted: dict[str, Any], credentials: dict[str, Any]) -> bool:
        """Send formatted notification to Telegram via Bot API.

        Args:
            formatted: Telegram formatted message dict.
            credentials: Dictionary containing 'bot_token' and 'chat_id'.

        Returns:
            True if message was sent successfully.

        Raises:
            ValueError: If required credentials are missing.
            RuntimeError: If the request fails or times out.
        """
        bot_token = credentials.get("bot_token")
        chat_id = credentials.get("chat_id")

        if not bot_token:
            raise ValueError("Missing 'bot_token' in credentials")
        if not chat_id:
            raise ValueError("Missing 'chat_id' in credentials")

        url = f"{TELEGRAM_API_BASE}{bot_token}/sendMessage"

        payload = {
            "chat_id": chat_id,
            "text": formatted["text"],
            "parse_mode": formatted.get("parse_mode", "HTML"),
        }

        # Add inline keyboard if present
        if "reply_markup" in formatted:
            payload["reply_markup"] = json.dumps(formatted["reply_markup"])

        # Disable link previews for cleaner messages. link_preview_options
        # replaced the deprecated disable_web_page_preview flag; as a nested
        # object it must be JSON-serialized in a form-encoded request, the
        # same as reply_markup above.
        payload["link_preview_options"] = json.dumps({"is_disabled": True})

        try:
            response = requests.post(
                url,
                data=payload,
                timeout=self.timeout,
            )

            # Telegram returns JSON with ok=true/false
            result = response.json()
            if not result.get("ok"):
                error_desc = result.get("description", "Unknown error")
                logger.error(
                    "Telegram API error",
                    extra={"error": error_desc, "error_code": result.get("error_code")},
                )
                raise RuntimeError(f"Telegram API error: {error_desc}")

            return True

        except requests.exceptions.Timeout:
            logger.error(
                "Telegram request timed out",
                extra={"timeout": self.timeout},
            )
            raise RuntimeError("Telegram request timed out") from None
        except ValueError as e:
            # response.json() raises when Telegram (or an intermediary proxy)
            # returns a non-JSON body, e.g. an HTML 502/504 page. Surface it
            # distinctly from network errors so failures are diagnosable.
            logger.error(
                "Telegram returned a non-JSON response",
                extra={"error": str(e)},
            )
            raise RuntimeError(
                "Telegram returned an invalid (non-JSON) response"
            ) from e
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to send message to Telegram",
                extra={"error": str(e)},
                exc_info=True,
            )
            raise RuntimeError("Failed to send notification to Telegram") from e

    def _format_header(self, n: RichNotification) -> str:
        """Format the notification header.

        Args:
            n: RichNotification.

        Returns:
            HTML formatted header string.
        """
        emoji = TELEGRAM_ICONS.get(n.headline_icon, "🔔")
        return f"{emoji} <b>{self._escape_html(n.headline)}</b>"

    def _format_insight(self, insight: InsightInfo) -> str:
        """Format the insight/milestone line.

        Args:
            insight: InsightInfo object.

        Returns:
            HTML formatted insight string.
        """
        emoji = TELEGRAM_ICONS.get(insight.icon, "⭐")
        return f"{emoji} <i>{self._escape_html(insight.text)}</i>"

    def _format_provider_badge(self, n: RichNotification) -> str:
        """Format the provider/source badge.

        Args:
            n: RichNotification.

        Returns:
            HTML formatted provider badge string.
        """
        provider_emoji = PROVIDER_ICONS.get(n.provider, DEFAULT_PROVIDER_ICON)
        elements = [f"{provider_emoji} {self._escape_html(n.provider_display)}"]

        # Only add payment-specific badges for payment events
        if n.is_payment_event:
            # Add payment type (recurring/one-time)
            if n.is_recurring:
                if n.billing_interval:
                    interval = self._escape_html(n.billing_interval.title())
                    elements.append(f"🔄 Recurring ({interval})")
                else:
                    elements.append("🔄 Recurring")
            elif n.payment:
                elements.append("💵 One-Time")

            # Add payment method if available. payment_method/card_last4 come
            # from the webhook payload, so escape before rendering as HTML.
            if n.payment and n.payment.payment_method:
                pm_emoji = PAYMENT_METHOD_ICONS.get(
                    n.payment.payment_method.lower(), "💳"
                )
                pm_display = n.payment.payment_method.title()
                if n.payment.card_last4:
                    pm_display += f" ••••{n.payment.card_last4}"
                elements.append(f"{pm_emoji} {self._escape_html(pm_display)}")
        else:
            # For non-payment events, add category badge
            category = n.category.value.title()
            cat_emoji = CATEGORY_ICONS.get(n.category.value, "ℹ️")
            elements.append(f"{cat_emoji} {category}")

        return " • ".join(elements)

    def _format_payment_details(self, n: RichNotification) -> str:
        """Format payment/order details section.

        Args:
            n: RichNotification with payment info.

        Returns:
            HTML formatted payment details string.
        """
        payment = n.payment
        if not payment:
            return ""

        # Check if this is e-commerce (has order number or line items)
        is_ecommerce = payment.order_number or payment.line_items

        if is_ecommerce:
            return self._format_ecommerce_details(payment)
        return self._format_subscription_details(payment)

    def _format_subscription_details(self, payment: PaymentInfo) -> str:
        """Format SaaS subscription payment details.

        Args:
            payment: PaymentInfo object.

        Returns:
            HTML formatted subscription details string.
        """
        lines = ["📊 <b>Payment Details</b>"]

        # Amount with ARR
        amount_str = self._escape_html(payment.format_amount_with_arr())
        lines.append(f"<b>Amount:</b> {amount_str}")

        if payment.plan_name:
            plan = self._escape_html(payment.plan_name)
            lines.append(f"<b>Plan:</b> {plan}")
        if payment.subscription_id:
            sub_id = self._escape_html(payment.subscription_id)
            lines.append(f"<b>Subscription:</b> #{sub_id}")
        if payment.failure_reason:
            reason = self._escape_html(payment.failure_reason)
            lines.append(f"❌ <b>Reason:</b> {reason}")

        return "\n".join(lines)

    def _format_ecommerce_details(self, payment: PaymentInfo) -> str:
        """Format e-commerce order details with line items.

        Args:
            payment: PaymentInfo object.

        Returns:
            HTML formatted e-commerce details string.
        """
        order_display = payment.order_number or "N/A"
        lines = [f"🛒 <b>Order #{self._escape_html(order_display)}</b>"]

        # currency comes from the webhook payload; escape it once for reuse.
        currency = self._escape_html(payment.currency)

        # Amount. Coerce defensively: some providers send amounts as strings,
        # which would raise on :.2f formatting and fail the whole send.
        amount = self._coerce_float(payment.amount)
        lines.append(f"<b>Amount:</b> {currency} {amount:,.2f}")

        # Line items (max 5)
        if payment.line_items:
            for item in payment.line_items[:5]:
                qty = self._coerce_int(item.get("quantity", 1), default=1)
                name = self._escape_html(item.get("name", "Item"))
                price = self._coerce_float(item.get("price", 0))
                lines.append(f"• {qty}x {name} ({currency} {price:.2f})")

            if len(payment.line_items) > 5:
                remaining = len(payment.line_items) - 5
                lines.append(f"<i>...and {remaining} more items</i>")

        return "\n".join(lines)

    def _format_detail_section(self, section: DetailSection) -> str:
        """Format a generic detail section.

        Args:
            section: DetailSection object.

        Returns:
            HTML formatted detail section string.
        """
        icon_emoji = TELEGRAM_ICONS.get(section.icon, "ℹ️")
        lines = [f"{icon_emoji} <b>{self._escape_html(section.title)}</b>"]

        # Add fields
        for detail_field in section.fields:
            field_icon = ""
            if detail_field.icon:
                field_emoji = TELEGRAM_ICONS.get(detail_field.icon, "")
                if field_emoji:
                    field_icon = f"{field_emoji} "
            lines.append(
                f"{field_icon}<b>{self._escape_html(detail_field.label)}:</b> "
                f"{self._escape_html(detail_field.value)}"
            )

        # Add freeform text
        if section.text:
            lines.append(self._escape_html(section.text))

        return "\n".join(lines)

    def _format_company_section(self, company: CompanyInfo) -> str:
        """Format company enrichment section.

        Args:
            company: CompanyInfo object.

        Returns:
            HTML formatted company section string.
        """
        lines = [f"🏢 <b>{self._escape_html(company.name)}</b>"]

        # Company details line
        details: list[str] = []
        if company.industry:
            details.append(company.industry)
        if company.year_founded:
            details.append(f"Founded {company.year_founded}")
        if company.employee_count:
            details.append(f"{company.employee_count} employees")
        if details:
            lines.append(f"<i>{self._escape_html(' • '.join(details))}</i>")

        # Description (truncated)
        if company.description:
            desc = company.description[:100]
            if len(company.description) > 100:
                desc += "..."
            lines.append(f"<i>{self._escape_html(desc)}</i>")

        return "\n".join(lines)

    def _format_company_links(self, company: CompanyInfo) -> str | None:
        """Format company website and LinkedIn links.

        Args:
            company: CompanyInfo object.

        Returns:
            HTML formatted links string, or None if no links available.
        """
        elements: list[str] = []

        # Website link. Escape the enrichment-supplied values for the href
        # attribute so a domain/URL with quotes or angle brackets can't break
        # out of the tag or inject markup.
        if company.domain:
            domain = self._escape_html_attr(company.domain)
            elements.append(f'🌐 <a href="https://{domain}">Website</a>')

        # LinkedIn link
        if company.linkedin_url:
            linkedin_url = self._escape_html_attr(company.linkedin_url)
            elements.append(f'💼 <a href="{linkedin_url}">LinkedIn</a>')

        if not elements:
            return None

        return " • ".join(elements)

    def _format_customer_footer(self, customer: CustomerInfo) -> str | None:
        """Format customer info footer.

        Args:
            customer: CustomerInfo object.

        Returns:
            HTML formatted customer footer string, or None if no meaningful data.
        """
        elements: list[str] = []

        # Email
        if customer.email:
            elements.append(f"👤 {self._escape_html(customer.email)}")

        # Name if no email
        if not customer.email and customer.name:
            elements.append(f"👤 {self._escape_html(customer.name)}")

        # Tenure
        if customer.tenure_display:
            elements.append(f"📅 {self._escape_html(customer.tenure_display)}")

        # LTV
        if customer.ltv_display:
            elements.append(f"💰 {self._escape_html(customer.ltv_display)} LTV")

        # Orders count
        if customer.orders_count:
            elements.append(f"{customer.orders_count} orders")

        # Status flags
        for flag in customer.status_flags:
            if flag == "at_risk":
                elements.append("🚨 <b>At Risk</b>")
            elif flag == "vip":
                elements.append("⭐ <b>VIP</b>")

        # Return None if no meaningful customer data to display
        if not elements:
            return None

        return " • ".join(elements)

    def _format_actions(self, actions: list[ActionButton]) -> dict[str, Any]:
        """Format action buttons as Telegram inline keyboard.

        Args:
            actions: List of ActionButton objects.

        Returns:
            Telegram inline keyboard markup dict.
        """
        buttons: list[list[dict[str, str]]] = []

        # Create rows with max 2 buttons per row for better mobile display
        # Telegram supports more buttons, but limit to 6 for cleaner UI
        row: list[dict[str, str]] = []
        for action in actions[:6]:
            button: dict[str, str] = {
                "text": action.text,
                "url": action.url,
            }
            row.append(button)

            # 2 buttons per row
            if len(row) == 2:
                buttons.append(row)
                row = []

        # Add remaining buttons
        if row:
            buttons.append(row)

        return {"inline_keyboard": buttons}

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape special HTML characters for Telegram.

        Args:
            text: Text to escape.

        Returns:
            HTML-escaped text.
        """
        if not text:
            return ""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @classmethod
    def _escape_html_attr(cls, value: str) -> str:
        """Escape a value for use inside a double-quoted HTML attribute.

        Same as :meth:`_escape_html` plus ``"`` -> ``&quot;`` so that
        enrichment-supplied URLs (domain, LinkedIn) cannot break out of an
        ``href="..."`` attribute or inject markup. ``&quot;`` is one of the
        named entities Telegram's HTML parse mode accepts.

        Args:
            value: The attribute value to escape.

        Returns:
            The escaped attribute value.
        """
        return cls._escape_html(value).replace('"', "&quot;")

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        """Best-effort float coercion for amounts that may arrive as strings.

        Some providers send numeric fields as strings; formatting those with
        ``:.2f`` directly would raise and fail the whole send. Mirrors the
        defensive handling in the Slack plugin.

        Args:
            value: The value to coerce.
            default: Fallback when coercion fails.

        Returns:
            The value as a float, or ``default`` when it can't be parsed.
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_int(value: Any, default: int = 0) -> int:
        """Best-effort int coercion for counts that may arrive as strings.

        Line-item quantities can arrive as strings (or junk); rendering them
        raw risks broken markup, so coerce and fall back to ``default``.

        Args:
            value: The value to coerce.
            default: Fallback when coercion fails.

        Returns:
            The value as an int, or ``default`` when it can't be parsed.
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

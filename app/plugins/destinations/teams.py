"""Microsoft Teams destination plugin.

Delivers notifications to a Teams channel via a Power Automate **Workflows**
incoming webhook — the supported successor to the retiring Office 365
connector webhooks. The plugin renders a RichNotification into an Adaptive
Card and POSTs it to the workspace's webhook URL.

Design constraints (from the Teams/Power Automate docs):

- The Workflows "when a webhook request is received" trigger expects a
  ``{"type": "message", "attachments": [{contentType, content}]}`` envelope
  wrapping the Adaptive Card.
- Target Adaptive Card schema **1.2**: Teams desktop/web supports up to 1.5,
  but the Teams mobile app only renders up to 1.2, so we stay 1.2-safe.
- Only ``Action.OpenUrl`` works for a fire-and-forget post; interactive
  (Action.Submit) actions error unless the flow waits for a response, which
  a one-way notification never does. We therefore emit URL buttons only.
- Messages post under the Workflows "Flow" bot identity (no custom name/icon
  is supported on this path).
"""

import logging
from typing import Any

import requests
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.destinations.base import BaseDestinationPlugin
from webhooks.models.rich_notification import (
    CustomerInfo,
    PaymentInfo,
    RichNotification,
)

logger = logging.getLogger(__name__)

# Default timeout for Teams webhook requests (seconds).
DEFAULT_TIMEOUT = 30

# Adaptive Card content type the Workflows webhook expects in attachments.
ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

# Stay 1.2-safe: the Teams mobile app only renders Adaptive Cards up to 1.2.
ADAPTIVE_CARD_VERSION = "1.2"

# Semantic headline icon -> unicode emoji (Teams renders emoji in TextBlocks).
HEADLINE_ICONS: dict[str, str] = {
    "money": "💰",
    "error": "❌",
    "celebration": "🎉",
    "warning": "⚠️",
    "info": "ℹ️",
    "cart": "🛒",
    "package": "📦",
    "feedback": "💬",
    "support": "🎫",
    "user": "👤",
}

# Semantic insight icon -> unicode emoji.
INSIGHT_ICONS: dict[str, str] = {
    "celebration": "🎉",
    "warning": "⚠️",
    "trophy": "🏆",
    "new": "🆕",
    "chart": "📈",
}

# Human-readable labels for customer status flags.
STATUS_FLAG_LABELS: dict[str, str] = {
    "at_risk": "🚨 At Risk",
    "vip": "⭐ VIP",
}

# Characters that carry meaning in the Markdown subset Teams renders inside
# Adaptive Card ``TextBlock``/``FactSet`` fields (emphasis, links, lists,
# code, blockquote, headings). Webhook payloads (customer/company names,
# plan names, failure reasons, ...) are attacker-controllable, so these are
# neutralized before interpolation to prevent unintended formatting or link
# injection (e.g. a plan name of ``[Update billing](https://evil/login)``).
_MARKDOWN_SPECIAL = frozenset("\\`*_[]()~#>")


def _escape_md(text: str | None) -> str:
    """Escape untrusted text for the Teams Adaptive Card Markdown subset.

    Args:
        text: The untrusted text (coerced to str). May be None.

    Returns:
        The text with Markdown control characters backslash-escaped so they
        render literally. Empty string for None/empty input.
    """
    if not text:
        return ""
    return "".join(f"\\{ch}" if ch in _MARKDOWN_SPECIAL else ch for ch in str(text))


class TeamsDestinationPlugin(BaseDestinationPlugin):
    """Format and send a RichNotification as a Teams Adaptive Card."""

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="teams",
            display_name="Microsoft Teams",
            version="1.0.0",
            description="Send notifications to Microsoft Teams via a Workflows webhook",
            plugin_type=PluginType.DESTINATION,
            capabilities={
                PluginCapability.RICH_FORMATTING,
                PluginCapability.ACTIONS,
            },
            priority=100,
        )

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        """Initialize the Teams destination plugin.

        Args:
            timeout: Request timeout in seconds (default: 30).
        """
        self.timeout = timeout

    def format(self, n: RichNotification) -> dict[str, Any]:
        """Format a notification as the Teams webhook Adaptive Card envelope.

        Args:
            n: RichNotification to format.

        Returns:
            The ``{"type": "message", "attachments": [...]}`` payload the
            Workflows webhook expects.
        """
        icon = HEADLINE_ICONS.get(n.headline_icon, "🔔")
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": f"{icon} {_escape_md(n.headline)}",
                "weight": "Bolder",
                "size": "Large",
                "wrap": True,
            }
        ]

        if n.insight:
            insight_emoji = INSIGHT_ICONS.get(n.insight.icon, "⭐")
            body.append(
                {
                    "type": "TextBlock",
                    "text": f"{insight_emoji} {_escape_md(n.insight.text)}",
                    "wrap": True,
                    "isSubtle": True,
                    "spacing": "Small",
                }
            )

        # Provider + payment-type / category subtitle. The payment-type and
        # category come from our own enums (safe); only provider_display can
        # carry attacker-controlled text, so escape that one.
        provider = _escape_md(n.provider_display)
        if n.is_payment_event:
            subtitle = f"{provider} • {n.get_payment_type_display()}"
        else:
            subtitle = f"{provider} • {n.category.value.title()}"
        body.append(
            {
                "type": "TextBlock",
                "text": subtitle,
                "wrap": True,
                "isSubtle": True,
                "spacing": "Small",
            }
        )

        facts = self._build_facts(n)
        if facts:
            body.append({"type": "FactSet", "facts": facts})

        card: dict[str, Any] = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": ADAPTIVE_CARD_VERSION,
            "body": body,
        }

        # Only Action.OpenUrl works for a fire-and-forget webhook post.
        actions = [
            {"type": "Action.OpenUrl", "title": action.text, "url": action.url}
            for action in (n.actions or [])[:6]
            if action.url
        ]
        if actions:
            card["actions"] = actions

        return {
            "type": "message",
            "attachments": [
                {"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "content": card}
            ],
        }

    def _build_facts(self, n: RichNotification) -> list[dict[str, str]]:
        """Build the Adaptive Card FactSet rows (payment/company/customer).

        Values are plain strings placed into JSON (json-serialized on send),
        so no HTML escaping is needed — Adaptive Cards aren't HTML.
        """
        facts: list[dict[str, str]] = []
        if n.payment:
            facts.extend(self._payment_facts(n.payment))
        if n.company:
            facts.append({"title": "Company", "value": _escape_md(n.company.name)})
            if n.company.industry:
                facts.append(
                    {"title": "Industry", "value": _escape_md(n.company.industry)}
                )
        if n.customer:
            facts.extend(self._customer_facts(n.customer))
        return facts

    @staticmethod
    def _payment_facts(payment: PaymentInfo) -> list[dict[str, str]]:
        """FactSet rows for the payment/order portion of a notification.

        Values are escaped for the Adaptive Card Markdown subset because
        several (plan name, order/subscription id, failure reason) originate
        from the untrusted webhook payload.
        """
        facts = [
            {"title": "Amount", "value": _escape_md(payment.format_amount_with_arr())}
        ]
        if payment.plan_name:
            facts.append({"title": "Plan", "value": _escape_md(payment.plan_name)})
        if payment.order_number:
            facts.append(
                {"title": "Order", "value": f"#{_escape_md(payment.order_number)}"}
            )
        if payment.subscription_id:
            facts.append(
                {"title": "Subscription", "value": _escape_md(payment.subscription_id)}
            )
        if payment.payment_method:
            method = payment.payment_method.title()
            if payment.card_last4:
                method += f" ••••{payment.card_last4}"
            facts.append({"title": "Payment", "value": _escape_md(method)})
        if payment.failure_reason:
            facts.append(
                {"title": "Reason", "value": _escape_md(payment.failure_reason)}
            )
        return facts

    @staticmethod
    def _customer_facts(customer: CustomerInfo) -> list[dict[str, str]]:
        """FactSet rows for the customer portion of a notification.

        Identity fields come from the untrusted webhook payload, so they are
        escaped; the status labels are our own constants and are safe.
        """
        facts: list[dict[str, str]] = []
        who = customer.email or customer.name
        if who:
            facts.append({"title": "Customer", "value": _escape_md(who)})
        if customer.tenure_display:
            facts.append(
                {"title": "Since", "value": _escape_md(customer.tenure_display)}
            )
        if customer.ltv_display:
            facts.append({"title": "LTV", "value": _escape_md(customer.ltv_display)})
        flags = [
            STATUS_FLAG_LABELS[flag]
            for flag in customer.status_flags
            if flag in STATUS_FLAG_LABELS
        ]
        if flags:
            facts.append({"title": "Status", "value": " • ".join(flags)})
        return facts

    def send(self, formatted: dict[str, Any], credentials: dict[str, Any]) -> bool:
        """POST the Adaptive Card envelope to the Teams Workflows webhook.

        Args:
            formatted: The payload from :meth:`format`.
            credentials: Dict containing ``webhook_url``.

        Returns:
            True when Teams accepts the message (2xx).

        Raises:
            ValueError: If ``webhook_url`` is missing.
            RuntimeError: If the request fails or times out.
        """
        webhook_url = credentials.get("webhook_url")
        if not webhook_url:
            raise ValueError("Missing 'webhook_url' in credentials")

        try:
            response = requests.post(webhook_url, json=formatted, timeout=self.timeout)
            response.raise_for_status()
            return True
        except requests.exceptions.Timeout:
            logger.error("Teams request timed out", extra={"timeout": self.timeout})
            raise RuntimeError("Teams request timed out") from None
        except requests.exceptions.RequestException as e:
            # The Workflows webhook URL is a bearer secret and is embedded in
            # requests exception messages; log only the type and raise
            # `from None` so the URL-bearing cause isn't chained to a caller
            # that logs with exc_info.
            logger.error(
                "Failed to send message to Teams",
                extra={"error_type": type(e).__name__},
            )
            raise RuntimeError("Failed to send notification to Teams") from None

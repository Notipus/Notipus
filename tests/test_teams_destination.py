"""Tests for the TeamsDestinationPlugin.

This module tests the TeamsDestinationPlugin class that converts
RichNotification objects into Microsoft Teams Adaptive Cards and delivers
them via a Power Automate Workflows incoming webhook.
"""

from unittest.mock import MagicMock, patch

import pytest
from plugins.base import PluginCapability, PluginType
from plugins.destinations.base import BaseDestinationPlugin
from plugins.destinations.teams import (
    ADAPTIVE_CARD_CONTENT_TYPE,
    ADAPTIVE_CARD_VERSION,
    TeamsDestinationPlugin,
)
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


@pytest.fixture
def plugin() -> TeamsDestinationPlugin:
    """Create a TeamsDestinationPlugin instance."""
    return TeamsDestinationPlugin()


@pytest.fixture
def basic_notification() -> RichNotification:
    """Create a basic payment RichNotification for testing."""
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
            status_flags=["vip"],
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


class TestTeamsPluginMetadata:
    """Test TeamsDestinationPlugin metadata and registration."""

    def test_plugin_metadata_name(self) -> None:
        """The plugin advertises the name 'teams'."""
        assert TeamsDestinationPlugin.get_metadata().name == "teams"

    def test_plugin_metadata_display_name(self) -> None:
        """The plugin advertises a human-readable display name."""
        assert TeamsDestinationPlugin.get_metadata().display_name == "Microsoft Teams"

    def test_plugin_metadata_type(self) -> None:
        """The plugin is a destination plugin."""
        assert TeamsDestinationPlugin.get_metadata().plugin_type == (
            PluginType.DESTINATION
        )

    def test_plugin_metadata_capabilities(self) -> None:
        """The plugin declares rich formatting and action capabilities."""
        capabilities = TeamsDestinationPlugin.get_metadata().capabilities
        assert PluginCapability.RICH_FORMATTING in capabilities
        assert PluginCapability.ACTIONS in capabilities

    def test_plugin_instance(self) -> None:
        """The plugin is a BaseDestinationPlugin subclass instance."""
        plugin = TeamsDestinationPlugin()
        assert isinstance(plugin, TeamsDestinationPlugin)
        assert isinstance(plugin, BaseDestinationPlugin)

    def test_plugin_name(self) -> None:
        """The instance reports its plugin name."""
        assert TeamsDestinationPlugin().get_plugin_name() == "teams"

    def test_plugin_custom_timeout(self) -> None:
        """A custom timeout is stored on the instance."""
        assert TeamsDestinationPlugin(timeout=60).timeout == 60


class TestTeamsFormatEnvelope:
    """Test the Workflows webhook envelope structure."""

    def test_format_returns_message_envelope(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """format() wraps the card in the Workflows message envelope."""
        result = plugin.format(basic_notification)
        assert result["type"] == "message"
        assert isinstance(result["attachments"], list)
        assert len(result["attachments"]) == 1

    def test_attachment_uses_adaptive_card_content_type(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """The attachment declares the Adaptive Card content type."""
        attachment = plugin.format(basic_notification)["attachments"][0]
        assert attachment["contentType"] == ADAPTIVE_CARD_CONTENT_TYPE

    def test_card_is_adaptive_card_1_2(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """The card targets Adaptive Card schema 1.2 (mobile-safe)."""
        card = plugin.format(basic_notification)["attachments"][0]["content"]
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == ADAPTIVE_CARD_VERSION == "1.2"
        assert card["$schema"].startswith("http://adaptivecards.io/schemas/")

    def test_card_body_is_a_list_of_elements(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """The card body is a non-empty list of card elements."""
        card = plugin.format(basic_notification)["attachments"][0]["content"]
        assert isinstance(card["body"], list)
        assert len(card["body"]) > 0


class TestTeamsFormatContent:
    """Test the rendered card content."""

    def _card(self, plugin: TeamsDestinationPlugin, n: RichNotification) -> dict:
        return plugin.format(n)["attachments"][0]["content"]

    def _text_blocks(self, card: dict) -> list[str]:
        return [b["text"] for b in card["body"] if b.get("type") == "TextBlock"]

    def _facts(self, card: dict) -> dict[str, str]:
        for block in card["body"]:
            if block.get("type") == "FactSet":
                return {f["title"]: f["value"] for f in block["facts"]}
        return {}

    def test_headline_included_with_emoji(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """The headline TextBlock includes the mapped emoji + headline text."""
        card = self._card(plugin, basic_notification)
        headline = card["body"][0]
        assert "💰" in headline["text"]
        assert "$299.00 from Acme Inc" in headline["text"]
        assert headline["weight"] == "Bolder"

    def test_unknown_headline_icon_uses_default_bell(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """An unmapped headline icon falls back to the bell emoji."""
        basic_notification.headline_icon = "does-not-exist"
        card = self._card(plugin, basic_notification)
        assert "🔔" in card["body"][0]["text"]

    def test_subtitle_includes_provider(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """A subtitle TextBlock names the provider."""
        card = self._card(plugin, basic_notification)
        assert any("Stripe" in t for t in self._text_blocks(card))

    def test_insight_rendered_when_present(
        self,
        plugin: TeamsDestinationPlugin,
        notification_with_insight: RichNotification,
    ) -> None:
        """An insight is rendered as its own TextBlock with an emoji."""
        card = self._card(plugin, notification_with_insight)
        assert any("Crossed $5,000 lifetime!" in t for t in self._text_blocks(card))
        assert any("🎉" in t for t in self._text_blocks(card))

    def test_payment_facts_rendered(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Payment details appear as FactSet rows."""
        facts = self._facts(self._card(plugin, basic_notification))
        assert "299" in facts["Amount"]
        assert facts["Plan"] == "Enterprise"
        # The underscore is a Markdown emphasis char, so it is backslash-escaped
        # (renders identically in Teams, but can't start an italic run).
        assert facts["Subscription"] == "sub\\_123"
        assert "Visa" in facts["Payment"]
        assert "4242" in facts["Payment"]

    def test_failure_reason_rendered(self, plugin: TeamsDestinationPlugin) -> None:
        """A payment failure reason appears in the FactSet."""
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
        facts = self._facts(self._card(plugin, notification))
        assert facts["Reason"] == "Card declined - insufficient funds"
        assert "❌" in self._card(plugin, notification)["body"][0]["text"]

    def test_company_facts_rendered(
        self,
        plugin: TeamsDestinationPlugin,
        notification_with_company: RichNotification,
    ) -> None:
        """Company enrichment appears in the FactSet."""
        facts = self._facts(self._card(plugin, notification_with_company))
        assert facts["Company"] == "Acme Corporation"
        assert facts["Industry"] == "Technology"

    def test_customer_facts_rendered(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Customer details (identity, tenure, LTV, status) appear as facts."""
        facts = self._facts(self._card(plugin, basic_notification))
        assert facts["Customer"] == "alice@acme.com"
        assert facts["Since"] == "Since Mar 2024"
        assert facts["LTV"] == "$1.5k"
        assert "VIP" in facts["Status"]


class TestTeamsFormatActions:
    """Test action-button rendering."""

    def _card(self, plugin: TeamsDestinationPlugin, n: RichNotification) -> dict:
        return plugin.format(n)["attachments"][0]["content"]

    def test_no_actions_key_when_no_actions(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """A card with no actions omits the 'actions' key entirely."""
        assert "actions" not in self._card(plugin, basic_notification)

    def test_actions_rendered_as_open_url(
        self,
        plugin: TeamsDestinationPlugin,
        notification_with_actions: RichNotification,
    ) -> None:
        """Actions render exclusively as fire-and-forget Action.OpenUrl."""
        actions = self._card(plugin, notification_with_actions)["actions"]
        assert len(actions) == 2
        assert all(a["type"] == "Action.OpenUrl" for a in actions)
        assert actions[0]["title"] == "View in Stripe"
        assert actions[0]["url"] == "https://stripe.com?utm_source=notipus"

    def test_actions_without_url_are_dropped(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Actions lacking a URL can't be OpenUrl buttons and are dropped."""
        basic_notification.actions = [
            ActionButton(text="No link", url="", style="default"),
            ActionButton(text="Has link", url="https://ok.com", style="primary"),
        ]
        actions = self._card(plugin, basic_notification)["actions"]
        assert len(actions) == 1
        assert actions[0]["title"] == "Has link"

    def test_actions_capped_at_six(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """No more than six action buttons are emitted."""
        basic_notification.actions = [
            ActionButton(text=f"Link {i}", url=f"https://x.com/{i}", style="default")
            for i in range(10)
        ]
        assert len(self._card(plugin, basic_notification)["actions"]) == 6


class TestTeamsMarkdownEscaping:
    """Untrusted payload text is escaped for the Adaptive Card Markdown subset.

    Teams renders a Markdown subset (emphasis, links) inside TextBlock and
    FactSet fields, so raw webhook values must not be able to inject links or
    formatting — mirroring Slack's ``safe_mrkdwn`` and Telegram's HTML escaping.
    """

    def _card(self, plugin: TeamsDestinationPlugin, n: RichNotification) -> dict:
        return plugin.format(n)["attachments"][0]["content"]

    def _facts(self, card: dict) -> dict[str, str]:
        for block in card["body"]:
            if block.get("type") == "FactSet":
                return {f["title"]: f["value"] for f in block["facts"]}
        return {}

    def test_escape_md_helper(self) -> None:
        """The helper backslash-escapes each Markdown control character."""
        from plugins.destinations.teams import _escape_md

        assert _escape_md(None) == ""
        assert _escape_md("") == ""
        assert _escape_md("plain text 4242") == "plain text 4242"
        assert _escape_md("*bold*") == "\\*bold\\*"
        assert _escape_md("[x](https://e)") == "\\[x\\]\\(https://e\\)"

    def test_headline_link_injection_is_neutralized(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """A Markdown link in the headline renders literally, not as a link."""
        basic_notification.headline = "[Update billing](https://evil/login)"
        text = self._card(plugin, basic_notification)["body"][0]["text"]
        # The raw link syntax must be broken by escaping.
        assert "](https://evil" not in text
        assert "\\[Update billing\\]\\(https://evil/login\\)" in text

    def test_fact_value_emphasis_is_escaped(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """Markdown emphasis in a payload-derived fact value is escaped."""
        basic_notification.payment.plan_name = "*Enterprise*"  # type: ignore[union-attr]
        facts = self._facts(self._card(plugin, basic_notification))
        assert facts["Plan"] == "\\*Enterprise\\*"

    def test_customer_name_is_escaped(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """A customer identity carrying Markdown is escaped in the FactSet."""
        basic_notification.customer.email = None  # type: ignore[union-attr]
        basic_notification.customer.name = "[a](http://x)"  # type: ignore[union-attr]
        facts = self._facts(self._card(plugin, basic_notification))
        assert facts["Customer"] == "\\[a\\]\\(http://x\\)"


class TestTeamsSend:
    """Test delivering the card to the Workflows webhook."""

    WEBHOOK = "https://prod-1.westus.logic.azure.com/workflows/abc"

    def test_send_requires_webhook_url(
        self, plugin: TeamsDestinationPlugin, basic_notification: RichNotification
    ) -> None:
        """send() raises without a webhook_url credential."""
        formatted = plugin.format(basic_notification)
        with pytest.raises(ValueError, match="webhook_url"):
            plugin.send(formatted, {})

    @patch("plugins.destinations.teams.requests.post")
    def test_send_posts_envelope_to_webhook(
        self,
        mock_post: MagicMock,
        plugin: TeamsDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """send() POSTs the JSON envelope to the configured webhook URL."""
        mock_post.return_value.raise_for_status.return_value = None
        formatted = plugin.format(basic_notification)

        result = plugin.send(formatted, {"webhook_url": self.WEBHOOK})

        assert result is True
        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == self.WEBHOOK
        assert mock_post.call_args[1]["json"] == formatted

    @patch("plugins.destinations.teams.requests.post")
    def test_send_handles_timeout(
        self,
        mock_post: MagicMock,
        plugin: TeamsDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """A timeout surfaces as a clear RuntimeError."""
        import requests

        mock_post.side_effect = requests.exceptions.Timeout()
        formatted = plugin.format(basic_notification)

        with pytest.raises(RuntimeError, match="timed out"):
            plugin.send(formatted, {"webhook_url": self.WEBHOOK})

    @patch("plugins.destinations.teams.requests.post")
    def test_send_handles_request_exception(
        self,
        mock_post: MagicMock,
        plugin: TeamsDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """A connection failure surfaces as a RuntimeError."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError()
        formatted = plugin.format(basic_notification)

        with pytest.raises(RuntimeError, match="Failed to send"):
            plugin.send(formatted, {"webhook_url": self.WEBHOOK})

    @patch("plugins.destinations.teams.requests.post")
    def test_send_handles_http_error(
        self,
        mock_post: MagicMock,
        plugin: TeamsDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """A non-2xx response (raise_for_status) surfaces as a RuntimeError."""
        import requests

        mock_post.return_value.raise_for_status.side_effect = (
            requests.exceptions.HTTPError("400 Bad Request")
        )
        formatted = plugin.format(basic_notification)

        with pytest.raises(RuntimeError, match="Failed to send"):
            plugin.send(formatted, {"webhook_url": self.WEBHOOK})

    @patch("plugins.destinations.teams.requests.post")
    def test_send_failure_never_leaks_webhook_url(
        self,
        mock_post: MagicMock,
        plugin: TeamsDestinationPlugin,
        basic_notification: RichNotification,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A request failure must not leak the webhook URL into logs or errors.

        The Workflows webhook URL is a bearer secret (anyone with it can post
        to the channel) and requests exception messages embed the request URL,
        so neither the raised error nor anything logged may contain it.
        """
        import logging

        import requests

        secret = "https://prod-1.westus.logic.azure.com/workflows/SUPERSECRETsig"
        mock_post.side_effect = requests.exceptions.ConnectionError(
            f"Max retries exceeded with url: {secret}"
        )
        formatted = plugin.format(basic_notification)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(RuntimeError) as exc_info:
                plugin.send(formatted, {"webhook_url": secret})

        assert "SUPERSECRETsig" not in str(exc_info.value)
        assert "SUPERSECRETsig" not in caplog.text


class TestTeamsFormatAndSend:
    """Test the format_and_send convenience method."""

    @patch("plugins.destinations.teams.requests.post")
    def test_format_and_send(
        self,
        mock_post: MagicMock,
        plugin: TeamsDestinationPlugin,
        basic_notification: RichNotification,
    ) -> None:
        """format_and_send formats then delivers in one call."""
        mock_post.return_value.raise_for_status.return_value = None

        result = plugin.format_and_send(
            basic_notification,
            {"webhook_url": "https://prod-1.westus.logic.azure.com/workflows/abc"},
        )

        assert result is True
        mock_post.assert_called_once()

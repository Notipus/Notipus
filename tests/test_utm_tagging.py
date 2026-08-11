"""Tests for UTM tagging of outbound notification links.

Covers the shared ``tag_url`` helper and its wiring into each of the
three destination plugins, so every link Notipus posts carries
``utm_source=notipus`` and every notification ends with the attribution
footer.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from plugins.destinations.slack import SlackDestinationPlugin
from plugins.destinations.teams import TeamsDestinationPlugin
from plugins.destinations.telegram import TelegramDestinationPlugin
from plugins.destinations.utm import (
    ATTRIBUTION_LABEL,
    UTM_SOURCE,
    attribution_url,
    tag_url,
)
from webhooks.models.rich_notification import (
    ActionButton,
    CompanyInfo,
    NotificationSeverity,
    NotificationType,
    PersonInfo,
    RichNotification,
)


def _utm_source_of(url: str) -> list[str]:
    """Return the ``utm_source`` values carried by ``url``.

    Args:
        url: URL to inspect.

    Returns:
        List of values, empty when the parameter is absent.
    """
    return parse_qs(urlsplit(url).query).get("utm_source", [])


class TestTagUrl:
    """Behaviour of the shared tag_url helper."""

    def test_appends_utm_source_to_bare_url(self) -> None:
        """A URL with no query string gains utm_source."""
        assert (
            tag_url("https://acme.com") == f"https://acme.com?utm_source={UTM_SOURCE}"
        )

    def test_preserves_existing_query_parameters(self) -> None:
        """Existing parameters survive tagging."""
        tagged = tag_url("https://acme.com/pricing?plan=pro")
        assert tagged is not None
        query = parse_qs(urlsplit(tagged).query)
        assert query["plan"] == ["pro"]
        assert query["utm_source"] == [UTM_SOURCE]

    def test_preserves_fragment(self) -> None:
        """A fragment stays at the end, after the added query string."""
        tagged = tag_url("https://acme.com/docs#billing")
        assert tagged == f"https://acme.com/docs?utm_source={UTM_SOURCE}#billing"

    def test_leaves_existing_utm_source_untouched(self) -> None:
        """A link that already names its source is not relabelled."""
        url = "https://acme.com?utm_source=partner"
        assert tag_url(url) == url

    def test_existing_utm_source_check_is_case_insensitive(self) -> None:
        """UTM_SOURCE is not duplicated when the existing key is uppercase."""
        url = "https://acme.com?UTM_SOURCE=partner"
        assert tag_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "mailto:peter@initech.com",
            "tel:+15551234567",
        ],
    )
    def test_leaves_non_http_schemes_untouched(self, url: str) -> None:
        """Schemes with no query string are returned unchanged."""
        assert tag_url(url) == url

    @pytest.mark.parametrize("url", [None, ""])
    def test_passes_through_empty_values(self, url: str | None) -> None:
        """None and empty string round-trip unchanged."""
        assert tag_url(url) == url

    def test_attribution_url_is_tagged(self) -> None:
        """The footer link carries the UTM source."""
        assert _utm_source_of(attribution_url()) == [UTM_SOURCE]


@pytest.fixture
def enriched_notification() -> RichNotification:
    """A notification carrying a company domain, socials, and an action."""
    return RichNotification(
        type=NotificationType.PAYMENT_SUCCESS,
        severity=NotificationSeverity.SUCCESS,
        headline="$4,999.00 from Initrode",
        headline_icon="money",
        provider="stripe",
        provider_display="Stripe",
        company=CompanyInfo(
            name="Initrode",
            domain="initrode.com",
            linkedin_url="https://linkedin.com/company/initrode",
        ),
        person=PersonInfo(
            email="peter.gibbons@initrode.com",
            first_name="Peter",
            last_name="Gibbons",
            linkedin_url="https://linkedin.com/in/pgibbons",
            twitter_handle="pgibbons",
            github_handle="pgibbons",
        ),
        actions=[
            ActionButton(
                text="Stripe Dashboard", url="https://dashboard.stripe.com/c/1"
            )
        ],
    )


class TestSlackTagging:
    """UTM tagging in the Slack destination."""

    def test_all_posted_links_carry_utm_source(
        self, enriched_notification: RichNotification
    ) -> None:
        """Every http(s) URL in the payload is tagged."""
        payload = SlackDestinationPlugin().format(enriched_notification)
        blob = json.dumps(payload)
        # Slack mrkdwn links render as <url|label>; every http(s) URL in the
        # payload must carry the parameter.
        assert "https://initrode.com?utm_source=notipus" in blob
        assert "utm_source=notipus" in blob
        assert blob.count("utm_source=notipus") >= 4

    def test_action_button_url_is_tagged(
        self, enriched_notification: RichNotification
    ) -> None:
        """Action button URLs are tagged."""
        payload = SlackDestinationPlugin().format(enriched_notification)
        buttons = [
            element
            for block in payload["attachments"][0]["blocks"]
            if block.get("type") == "actions"
            for element in block["elements"]
        ]
        assert buttons
        assert _utm_source_of(buttons[0]["url"]) == [UTM_SOURCE]

    def test_attribution_footer_is_last_block(
        self, enriched_notification: RichNotification
    ) -> None:
        """The footer is present, muted, and last."""
        payload = SlackDestinationPlugin().format(enriched_notification)
        last = payload["attachments"][0]["blocks"][-1]
        assert last["type"] == "context"
        assert ATTRIBUTION_LABEL in last["elements"][0]["text"]
        assert UTM_SOURCE in last["elements"][0]["text"]

    def test_mailto_action_is_not_tagged(self) -> None:
        """A mailto action keeps its bare address."""
        n = RichNotification(
            type=NotificationType.PAYMENT_FAILURE,
            severity=NotificationSeverity.ERROR,
            headline="Payment failed",
            headline_icon="warning",
            provider="stripe",
            provider_display="Stripe",
            actions=[
                ActionButton(text="Contact Customer", url="mailto:peter@initech.com")
            ],
        )
        payload = SlackDestinationPlugin().format(n)
        buttons = [
            element
            for block in payload["attachments"][0]["blocks"]
            if block.get("type") == "actions"
            for element in block["elements"]
        ]
        assert buttons[0]["url"] == "mailto:peter@initech.com"


class TestTelegramTagging:
    """UTM tagging in the Telegram destination."""

    def test_company_links_are_tagged(
        self, enriched_notification: RichNotification
    ) -> None:
        """Website and LinkedIn hrefs carry the parameter."""
        text = TelegramDestinationPlugin().format(enriched_notification)["text"]
        assert "https://initrode.com?utm_source=notipus" in text
        assert "linkedin.com/company/initrode?utm_source=notipus" in text

    def test_inline_keyboard_url_is_tagged(
        self, enriched_notification: RichNotification
    ) -> None:
        """Inline keyboard button URLs are tagged."""
        payload = TelegramDestinationPlugin().format(enriched_notification)
        url = payload["reply_markup"]["inline_keyboard"][0][0]["url"]
        assert _utm_source_of(url) == [UTM_SOURCE]

    def test_attribution_footer_present(
        self, enriched_notification: RichNotification
    ) -> None:
        """The footer link closes the message."""
        text = TelegramDestinationPlugin().format(enriched_notification)["text"]
        assert ATTRIBUTION_LABEL in text
        assert text.rstrip().endswith("</a>")


class TestTeamsTagging:
    """UTM tagging in the Teams destination."""

    def test_open_url_action_is_tagged(
        self, enriched_notification: RichNotification
    ) -> None:
        """Action.OpenUrl targets carry the parameter."""
        payload = TeamsDestinationPlugin().format(enriched_notification)
        card = payload["attachments"][0]["content"]
        assert _utm_source_of(card["actions"][0]["url"]) == [UTM_SOURCE]

    def test_attribution_footer_is_last_body_block(
        self, enriched_notification: RichNotification
    ) -> None:
        """The footer is the final body block and links out tagged."""
        payload = TeamsDestinationPlugin().format(enriched_notification)
        last = payload["attachments"][0]["content"]["body"][-1]
        assert last["type"] == "TextBlock"
        assert last["isSubtle"] is True
        assert ATTRIBUTION_LABEL in last["text"]
        assert UTM_SOURCE in last["text"]

"""End-to-end test of 👍 / 👎 feedback on industry classifications.

Drives the whole loop through real HTTP entry points:

1. A signed Shopify webhook arrives at the workspace endpoint.
2. The real router, event processor, enrichment service (Brandfetch and the
   LLM module are the only stand-ins), notification builder and Slack plugin
   produce a message; the outgoing Slack HTTP call is captured.
3. The 👍 / 👎 URLs are taken from that Slack payload, exactly as a user
   would click them, and followed with the test client: GET, then POST.
4. The vote lands in the database for the workspace that got the message.
5. The next webhook for the same company produces a Slack message that
   reflects the feedback, without calling the LLM again.
"""

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest
from core.models import ClassificationFeedback, Company, Integration, Workspace
from django.conf import settings
from django.test import Client

from tests.test_llm_enrichment import brandfetch_plugin, build_service, make_fake

SHOPIFY_SECRET = "e2e-shopify-secret"
SLACK_WEBHOOK = "https://hooks.slack.com/services/T000/B000/e2e"


@pytest.fixture
def workspace(db: Any) -> Workspace:
    """Workspace with a Shopify source and a Slack destination."""
    workspace = Workspace.objects.create(
        name="E2E Workspace", shop_domain="e2e.myshopify.com"
    )
    Integration.objects.create(
        workspace=workspace,
        integration_type="shopify",
        webhook_secret=SHOPIFY_SECRET,
        is_active=True,
    )
    Integration.objects.create(
        workspace=workspace,
        integration_type="slack_notifications",
        is_active=True,
        oauth_credentials={"incoming_webhook": {"url": SLACK_WEBHOOK}},
    )
    return workspace


@pytest.fixture
def llm() -> Any:
    """The LLM module stand-in (records each prompt it is sent)."""
    return make_fake("anthropic")


@pytest.fixture
def enrichment(llm: Any) -> Iterator[None]:
    """Use the real enrichment service with Brandfetch and LLM stand-ins."""
    service = build_service(
        [
            brandfetch_plugin(
                {"name": "Acme", "brand_info": {"description": "Payroll tools"}}
            ),
            llm,
        ]
    )
    with patch.object(settings.EVENT_PROCESSOR, "enrichment_service", service):
        yield


@pytest.fixture
def slack_posts() -> Iterator[list[dict[str, Any]]]:
    """Capture every message posted to Slack's incoming webhook."""
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: Any) -> MagicMock:
        assert url == SLACK_WEBHOOK
        posts.append(json)
        return MagicMock(status_code=200)

    with patch("plugins.destinations.slack.requests.post", side_effect=fake_post):
        yield posts


def send_order(client: Client, workspace: Workspace, order_id: int) -> None:
    """POST a correctly signed Shopify orders/paid webhook.

    Args:
        client: Django test client.
        workspace: Receiving workspace.
        order_id: Unique order id (so the webhook isn't deduplicated).
    """
    body = json.dumps(
        {
            "id": order_id,
            # String on purpose: an integer order_number currently crashes Slack
            # formatting on master too (fixed separately).
            "order_number": str(order_id % 10000),
            "customer": {"id": 77, "email": "jane.doe@acme.com"},
            "total_price": "99.00",
            "currency": "USD",
            "created_at": "2026-11-14T10:00:00Z",
        }
    ).encode()
    signature = base64.b64encode(
        hmac.new(SHOPIFY_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    response = client.post(
        f"/webhook/customer/{workspace.uuid}/shopify/",
        data=body,
        content_type="application/json",
        HTTP_X_SHOPIFY_TOPIC="orders/paid",
        HTTP_X_SHOPIFY_HMAC_SHA256=signature,
        HTTP_X_SHOPIFY_SHOP_DOMAIN="e2e.myshopify.com",
    )
    assert response.status_code == 200, response.content


def blocks(post: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all Block Kit blocks of a Slack message."""
    return [b for att in post["attachments"] for b in att.get("blocks", [])]


def buttons(post: dict[str, Any]) -> dict[str, str]:
    """Map button label to URL for a Slack message."""
    return {
        el["text"]["text"]: el["url"]
        for b in blocks(post)
        if b["type"] == "actions"
        for el in b["elements"]
    }


def message_text(post: dict[str, Any]) -> str:
    """Concatenate all text in a Slack message."""
    return json.dumps(blocks(post), ensure_ascii=False)


def local_path(url: str) -> str:
    """Turn an absolute Notipus URL from Slack into a test-client path."""
    assert url.startswith(settings.BASE_URL), url
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


@pytest.mark.django_db
@pytest.mark.usefixtures("enrichment")
class TestFeedbackEndToEnd:
    """Webhook → Slack → click → vote → next Slack message."""

    def test_downvote_with_correction(
        self,
        client: Client,
        workspace: Workspace,
        slack_posts: list[dict[str, Any]],
        llm: Any,
    ) -> None:
        """A 👎 with the right industry fixes every later notification."""
        send_order(client, workspace, 1001)

        assert len(slack_posts) == 1
        first = slack_posts[0]
        # Slack mrkdwn escapes "&" (displayed as "&" in Slack).
        assert "Software &amp; SaaS" in message_text(first)
        links = buttons(first)
        assert {"👍 Industry right", "👎 Wrong industry"} <= set(links)

        # The customer's email never reached the LLM.
        assert "jane.doe@acme.com" not in llm.calls[0]

        # Clicking the link only shows the page.
        down = local_path(links["👎 Wrong industry"])
        page = client.get(down)
        assert page.status_code == 200
        assert b"Correct industry" in page.content
        assert not ClassificationFeedback.objects.exists()

        # Submitting the form records the vote and the correction.
        submitted = client.post(
            down,
            {
                "vote": "down",
                "industry": "Financial Services & Fintech",
                "sub_industry": "Payroll",
            },
        )
        assert submitted.status_code == 200
        assert b"industry updated" in submitted.content

        feedback = ClassificationFeedback.objects.get()
        assert feedback.workspace == workspace
        assert feedback.vote == "down"
        assert feedback.industry == "Software & SaaS"
        assert feedback.provider == "anthropic"
        assert feedback.corrected_industry == "Financial Services & Fintech"
        company = Company.objects.get(domain="acme.com")
        assert company.brand_info["industry"] == "Financial Services & Fintech"

        # The next order shows the corrected industry, no rating buttons,
        # and is served from the permanent cache (no second LLM call).
        send_order(client, workspace, 1002)

        assert len(slack_posts) == 2
        second = slack_posts[1]
        assert "Financial Services &amp; Fintech › Payroll" in message_text(second)
        assert "👎 Wrong industry" not in buttons(second)
        assert len(llm.calls) == 1

    def test_upvote(
        self,
        client: Client,
        workspace: Workspace,
        slack_posts: list[dict[str, Any]],
    ) -> None:
        """A 👍 is counted and the classification stays as it is."""
        send_order(client, workspace, 2001)
        up = local_path(buttons(slack_posts[0])["👍 Industry right"])

        client.get(up)
        assert not ClassificationFeedback.objects.exists()
        response = client.post(up, {"vote": "up"})
        assert response.status_code == 200
        assert b"Thanks for confirming" in response.content

        feedback = ClassificationFeedback.objects.get()
        assert (feedback.workspace, feedback.vote) == (workspace, "up")
        classification = Company.objects.get(domain="acme.com").brand_info[
            "industry_classification"
        ]
        assert classification["feedback"] == {"up": 1, "down": 0}
        assert classification["industry"] == "Software & SaaS"

        # Still rateable on the next notification.
        send_order(client, workspace, 2002)
        assert "👍 Industry right" in buttons(slack_posts[1])

    def test_downvote_without_correction_then_change_of_mind(
        self,
        client: Client,
        workspace: Workspace,
        slack_posts: list[dict[str, Any]],
    ) -> None:
        """'Not sure' flags the classification; a later 👍 replaces the vote."""
        send_order(client, workspace, 3001)
        links = buttons(slack_posts[0])

        client.post(local_path(links["👎 Wrong industry"]), {"vote": "down"})
        company = Company.objects.get(domain="acme.com")
        assert company.brand_info["industry_classification"]["disputed"] is True

        client.post(local_path(links["👍 Industry right"]), {"vote": "up"})
        company.refresh_from_db()
        classification = company.brand_info["industry_classification"]
        assert ClassificationFeedback.objects.count() == 1
        assert classification["feedback"] == {"up": 1, "down": 0}
        assert classification["disputed"] is False

    def test_tampered_link_is_rejected(
        self,
        client: Client,
        workspace: Workspace,
        slack_posts: list[dict[str, Any]],
    ) -> None:
        """Editing the token in a Slack link gets a 404 and records nothing."""
        send_order(client, workspace, 4001)
        path = local_path(buttons(slack_posts[0])["👎 Wrong industry"])
        token = path.split("/")[3]
        forged = path.replace(
            token, token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
        )

        assert client.post(forged, {"vote": "down"}).status_code == 404
        assert not ClassificationFeedback.objects.exists()

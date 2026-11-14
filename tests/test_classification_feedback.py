"""Tests for 👍 / 👎 feedback on LLM industry classifications.

Covers:
- Signed feedback tokens
- Feedback links on notifications (and their absence)
- The feedback page: GET never votes, POST records, corrections apply
- Human corrections and vote counts surviving refresh and backfill
- Permanent enrichment caching and the automatic profile backfill
"""

from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from core.models import ClassificationFeedback, Company, Workspace
from core.services.classification_feedback import (
    carry_over_feedback,
    feedback_urls,
    make_token,
    read_token,
    record_feedback,
)
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from plugins.destinations.slack import SlackDestinationPlugin
from webhooks.services.notification_builder import NotificationBuilder

from tests.test_llm_enrichment import (
    VALID_RESULT,
    brandfetch_plugin,
    build_service,
    make_fake,
)

CLASSIFICATION: dict[str, Any] = {
    "industry": "Software & SaaS",
    "sub_industry": "Developer Tools",
    "business_model": "B2B",
    "target_market": "Mid-market",
    "keywords": [],
    "confidence": "medium",
    "provider": "openai",
    "model": "gpt-5-mini",
    "grounded": True,
}


@pytest.fixture
def company() -> Company:
    """A company with an LLM classification."""
    return Company.objects.create(
        domain="acme.com",
        name="Acme",
        brand_info={
            "name": "Acme",
            "industry": "Software & SaaS",
            "industry_classification": dict(CLASSIFICATION),
            "_blended_at": "2026-11-01T00:00:00+00:00",
            "_sources": {
                "brandfetch": {
                    "fetched_at": "2026-11-01T00:00:00+00:00",
                    "raw": {"name": "Acme", "brand_info": {"description": "Pay"}},
                }
            },
        },
    )


@pytest.fixture
def workspace() -> Workspace:
    """A workspace receiving notifications."""
    return Workspace.objects.create(name="Sales team")


def feedback_path(company: Company, workspace: Workspace | None, vote: str) -> str:
    """Return the feedback page path for a vote.

    Args:
        company: Company being rated.
        workspace: Voting workspace.
        vote: "up" or "down".

    Returns:
        URL path of the feedback page.
    """
    return reverse(
        "core:industry_feedback", args=[make_token(company, workspace), vote]
    )


def notification_for(company: Company, workspace: Workspace | None) -> Any:
    """Build a notification for a payment from ``company``.

    Args:
        company: Enriched company.
        workspace: Receiving workspace.

    Returns:
        RichNotification.
    """
    return NotificationBuilder().build(
        {"type": "payment_success", "provider": "stripe", "amount": 10},
        {"email": "jane@acme.com"},
        company=company,
        workspace=workspace,
    )


# ============================================================================
# Tokens and links
# ============================================================================


@pytest.mark.django_db
class TestTokens:
    """Feedback links carry a signed company + workspace token."""

    def test_round_trip(self, company: Company, workspace: Workspace) -> None:
        """A token decodes to its company and workspace."""
        assert read_token(make_token(company, workspace)) == (
            company.pk,
            workspace.pk,
        )

    def test_without_workspace(self, company: Company) -> None:
        """A token works without a workspace."""
        assert read_token(make_token(company, None)) == (company.pk, None)

    def test_tampered_token_is_rejected(self, company: Company) -> None:
        """Changing any character invalidates the token."""
        token = make_token(company, None)
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert read_token(tampered) is None
        assert read_token("garbage") is None

    def test_no_links_without_classification(self) -> None:
        """Companies without an LLM classification get no links."""
        plain = Company.objects.create(domain="plain.com", brand_info={"name": "P"})
        assert feedback_urls(plain, None) is None


@pytest.mark.django_db
class TestNotificationButtons:
    """Notifications carry 👍 / 👎 links when there is something to rate."""

    def test_buttons_added(self, company: Company, workspace: Workspace) -> None:
        """Both links are added and point at the feedback page."""
        actions = notification_for(company, workspace).actions
        texts = [a.text for a in actions]
        assert "👍 Industry right" in texts and "👎 Wrong industry" in texts
        down = next(a for a in actions if a.text == "👎 Wrong industry")
        assert "/feedback/industry/" in down.url and down.url.endswith("/down/")
        assert down.url.startswith("http")

    def test_no_buttons_once_corrected(self, company: Company) -> None:
        """A human-corrected classification isn't offered for rating again."""
        record_feedback(company, None, "down", ("Fintech", ""))
        company.refresh_from_db()
        texts = [a.text for a in notification_for(company, None).actions]
        assert not any("industry" in t.lower() for t in texts)

    def test_no_buttons_without_classification(self) -> None:
        """Brandfetch-only companies get no rating links."""
        plain = Company.objects.create(domain="plain.com", brand_info={"name": "P"})
        texts = [a.text for a in notification_for(plain, None).actions]
        assert not any("industry" in t.lower() for t in texts)

    def test_slack_renders_link_buttons(
        self, company: Company, workspace: Workspace
    ) -> None:
        """Slack renders them as URL buttons, which need no interactivity."""
        message = SlackDestinationPlugin().format(notification_for(company, workspace))
        buttons = [
            el
            for attachment in message["attachments"]
            for block in attachment.get("blocks", [])
            if block.get("type") == "actions"
            for el in block["elements"]
        ]
        labels = [b["text"]["text"] for b in buttons]
        assert "👎 Wrong industry" in labels
        assert all(b.get("url") for b in buttons)


# ============================================================================
# Feedback page
# ============================================================================


@pytest.mark.django_db
class TestFeedbackPage:
    """GET shows the page; POST records the vote."""

    def test_get_does_not_vote(self, company: Company, workspace: Workspace) -> None:
        """Opening the link (or a link prefetcher) casts no vote."""
        response = Client().get(feedback_path(company, workspace, "up"))
        assert response.status_code == 200
        assert b"Software &amp; SaaS" in response.content
        assert ClassificationFeedback.objects.count() == 0

    def test_upvote(self, company: Company, workspace: Workspace) -> None:
        """POSTing an upvote records it and updates the counts."""
        response = Client().post(
            feedback_path(company, workspace, "up"), {"vote": "up"}
        )
        assert response.status_code == 200
        assert b"Thanks for confirming" in response.content
        company.refresh_from_db()
        assert company.brand_info["industry_classification"]["feedback"] == {
            "up": 1,
            "down": 0,
        }
        fb = ClassificationFeedback.objects.get()
        assert (fb.vote, fb.provider, fb.workspace) == ("up", "openai", workspace)

    def test_downvote_with_correction(
        self, company: Company, workspace: Workspace
    ) -> None:
        """A correction replaces the industry and keeps the LLM's answer."""
        response = Client().post(
            feedback_path(company, workspace, "down"),
            {
                "vote": "down",
                "industry": "Financial Services & Fintech",
                "sub_industry": " Payroll ",
            },
        )
        assert response.status_code == 200
        company.refresh_from_db()
        info = company.brand_info
        c = info["industry_classification"]
        assert info["industry"] == "Financial Services & Fintech"
        assert c["industry"] == "Financial Services & Fintech"
        assert c["sub_industry"] == "Payroll"
        assert c["corrected"] is True
        assert c["llm_industry"] == "Software & SaaS"
        assert c["disputed"] is False

    def test_downvote_without_correction_marks_disputed(
        self, company: Company, workspace: Workspace
    ) -> None:
        """'Not sure' records the downvote and flags the classification."""
        Client().post(
            feedback_path(company, workspace, "down"), {"vote": "down", "industry": ""}
        )
        company.refresh_from_db()
        c = company.brand_info["industry_classification"]
        assert c["disputed"] is True
        assert c["industry"] == "Software & SaaS"

    def test_invalid_industry_is_rejected(
        self, company: Company, workspace: Workspace
    ) -> None:
        """Only taxonomy values are accepted as corrections."""
        response = Client().post(
            feedback_path(company, workspace, "down"),
            {"vote": "down", "industry": "<script>"},
        )
        assert response.status_code == 400
        assert ClassificationFeedback.objects.count() == 0

    def test_voting_again_replaces_vote(
        self, company: Company, workspace: Workspace
    ) -> None:
        """One vote per workspace: the latest one counts."""
        client = Client()
        client.post(feedback_path(company, workspace, "down"), {"vote": "down"})
        client.post(feedback_path(company, workspace, "up"), {"vote": "up"})
        company.refresh_from_db()
        assert ClassificationFeedback.objects.count() == 1
        assert company.brand_info["industry_classification"]["feedback"] == {
            "up": 1,
            "down": 0,
        }

    @pytest.mark.parametrize("vote", ["up", "down"])
    def test_bad_token_404(self, vote: str) -> None:
        """Forged links are rejected."""
        url = reverse("core:industry_feedback", args=["forged:token", vote])
        assert Client().get(url).status_code == 404

    def test_bad_vote_404(self, company: Company) -> None:
        """Only up/down are valid votes."""
        url = reverse("core:industry_feedback", args=[make_token(company, None), "meh"])
        assert Client().get(url).status_code == 404

    def test_page_is_noindex(self, company: Company) -> None:
        """Feedback pages are kept out of search engines."""
        response = Client().get(feedback_path(company, None, "down"))
        assert response["X-Robots-Tag"] == "noindex"


# ============================================================================
# Feedback survives regeneration; permanent caching
# ============================================================================


@pytest.mark.django_db
class TestFeedbackIsKept:
    """Human input is never overwritten by the LLM."""

    def test_correction_survives_refresh(self, company: Company) -> None:
        """refresh_enrichment regenerates everything except the correction."""
        record_feedback(company, None, "down", ("Gaming", ""))
        service = build_service(
            [brandfetch_plugin({"name": "Acme"}), make_fake("openai")]
        )

        refreshed = service.refresh_enrichment("acme.com")

        assert refreshed is not None
        assert refreshed.brand_info["industry"] == "Gaming"
        assert refreshed.brand_info["industry_classification"]["corrected"] is True
        # The rest of the profile was regenerated.
        assert refreshed.brand_info["description"] == VALID_RESULT["description"]

    def test_correction_survives_backfill(self, company: Company) -> None:
        """classify_companies --force never overwrites a correction."""
        record_feedback(company, None, "down", ("Gaming", ""))
        company.refresh_from_db()
        service = build_service([make_fake("openai")])
        assert service.classify_company(company, use_cache=False) is True
        company.refresh_from_db()
        assert company.brand_info["industry"] == "Gaming"

    def test_vote_counts_carry_over(self) -> None:
        """Without a correction, counts move onto the new classification."""
        old = {
            "industry_classification": {
                "industry": "A",
                "feedback": {"up": 2, "down": 1},
            }
        }
        new = {"industry": "B", "industry_classification": {"industry": "B"}}
        result = carry_over_feedback(old, new)
        assert result["industry"] == "B"
        assert result["industry_classification"]["feedback"] == {"up": 2, "down": 1}

    def test_disputed_command_reruns_only_disputed(
        self, company: Company, workspace: Workspace
    ) -> None:
        """--disputed re-runs downvoted, uncorrected classifications only."""
        other = Company.objects.create(
            domain="fine.com",
            brand_info={"industry_classification": dict(CLASSIFICATION)},
        )
        record_feedback(company, workspace, "down")
        llm = make_fake("openai")
        service = build_service([llm])
        out = StringIO()
        with patch(
            "core.management.commands.classify_companies.DomainEnrichmentService",
            return_value=service,
        ):
            call_command("classify_companies", "--disputed", stdout=out)

        assert "acme.com" in out.getvalue()
        assert other.domain not in out.getvalue()
        assert len(llm.calls) == 1


@pytest.mark.django_db
class TestPermanentCache:
    """Enrichment is kept forever; old records are upgraded once."""

    def test_old_record_is_not_refetched(self, company: Company) -> None:
        """Years-old enrichment is reused without calling providers."""
        company.brand_info["_blended_at"] = "2016-01-01T00:00:00+00:00"
        company.save()
        bf = brandfetch_plugin({"name": "New"})
        service = build_service([bf, make_fake("openai")])

        service.enrich_domain("acme.com")

        bf.enrich_domain.assert_not_called()

    def test_brandfetch_only_record_gets_profile_once(self) -> None:
        """A company enriched before LLMs existed is profiled from stored data."""
        Company.objects.create(
            domain="legacy.com",
            name="Legacy",
            brand_info={
                "name": "Legacy",
                "_blended_at": "2025-01-01T00:00:00+00:00",
                "_sources": {
                    "brandfetch": {
                        "fetched_at": "2025-01-01T00:00:00+00:00",
                        "raw": {"name": "Legacy"},
                    }
                },
            },
        )
        bf = brandfetch_plugin({"name": "Legacy"})
        llm = make_fake("anthropic")
        service = build_service([bf, llm])

        first = service.enrich_domain("legacy.com")
        service.enrich_domain("legacy.com")

        bf.enrich_domain.assert_not_called()
        assert len(llm.calls) == 1
        assert first is not None
        first.refresh_from_db()
        assert first.brand_info["industry_classification"]["industry"] == (
            "Software & SaaS"
        )

"""Regression tests for the code-review findings on LLM enrichment.

Each class covers one finding:
- Slack mrkdwn injection through LLM / human classification text
- Provider outages: not cached, retried, and never erasing previous data
- Admin refresh / purge actually fetching fresh data
- Votes cast during re-enrichment are not lost
- Corrections rebuilt from feedback rows (withdrawal, multiple workspaces)
- Links for deleted workspaces
- Round robin reusing an answer cached by another module
"""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from core.models import ClassificationFeedback, Company, Workspace
from core.services.classification_feedback import (
    feedback_urls,
    make_token,
    record_feedback,
)
from core.services.enrichment import DomainEnrichmentService
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from plugins.destinations.slack import SlackDestinationPlugin
from plugins.enrichment import ProviderUnavailableError
from webhooks.services.notification_builder import NotificationBuilder

from tests.test_llm_enrichment import (
    LOCMEM_CACHE,
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
}


@pytest.fixture(autouse=True)
def _real_cache(settings: Any) -> Iterator[None]:
    """Use an in-memory cache so caching behaviour is real."""
    settings.CACHES = LOCMEM_CACHE
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def workspace() -> Workspace:
    """A workspace receiving notifications."""
    return Workspace.objects.create(name="Review fixes")


@pytest.fixture
def company() -> Company:
    """A company with an LLM classification and Brandfetch data."""
    return Company.objects.create(
        domain="acme.com",
        name="Acme",
        brand_info={
            "name": "Acme",
            "description": "Payroll tools",
            "industry": "Software & SaaS",
            "industry_classification": dict(CLASSIFICATION),
            "_blended_at": "2026-11-01T00:00:00+00:00",
            "_sources": {
                "brandfetch": {
                    "fetched_at": "2026-11-01T00:00:00+00:00",
                    "raw": {"name": "Acme", "brand_info": {"description": "Payroll"}},
                }
            },
        },
    )


def slack_text(company: Company, workspace: Workspace | None = None) -> str:
    """Format a notification for ``company`` and return its Slack JSON."""
    notification = NotificationBuilder().build(
        {"type": "payment_success", "provider": "stripe", "amount": 10},
        {"email": "jane@acme.com"},
        company=company,
        workspace=workspace,
    )
    return json.dumps(SlackDestinationPlugin().format(notification))


@pytest.mark.django_db
class TestSlackInjection:
    """Classification text can't inject Slack mentions or links."""

    def test_correction_cannot_ping_channel_or_link(
        self, company: Company, workspace: Workspace
    ) -> None:
        """A malicious sub-industry renders as inert text."""
        response = Client().post(
            reverse(
                "core:industry_feedback", args=[make_token(company, workspace), "down"]
            ),
            {
                "vote": "down",
                "industry": "Gaming",
                "sub_industry": "<!channel> <https://evil.example|Verify payment>",
            },
        )
        assert response.status_code == 200
        company.refresh_from_db()
        text = slack_text(company)
        assert "<!channel>" not in text
        assert "<https://evil.example" not in text

    def test_llm_text_cannot_inject(self) -> None:
        """A prompt-injected model answer is stripped of Slack syntax."""
        llm = make_fake(
            "openai", {**VALID_RESULT, "sub_industry": "<!here> <https://x.io|click>"}
        )
        data = llm.enrich_domain("acme.com")
        sub = data["brand_info"]["industry_classification"]["sub_industry"]
        assert "<" not in sub and ">" not in sub


@pytest.mark.django_db
class TestProviderOutages:
    """Transient failures are not cached and never erase data."""

    def test_error_is_not_cached(self) -> None:
        """A Brandfetch timeout is retried on the next enrichment."""
        bf = brandfetch_plugin({"name": "Acme"})
        bf.enrich_domain.side_effect = [
            ProviderUnavailableError("timeout"),
            {"name": "Acme"},
        ]
        service = build_service([bf])

        service.enrich_domain("acme.com")
        company = service.enrich_domain("acme.com")

        assert bf.enrich_domain.call_count == 2
        assert company is not None and company.name == "Acme"

    def test_llm_only_profile_is_retried_daily(self) -> None:
        """A profile built while Brandfetch was down isn't kept forever."""
        bf = brandfetch_plugin({})
        bf.enrich_domain.side_effect = ProviderUnavailableError("down")
        service = build_service([bf, make_fake("openai")])
        company = service.enrich_domain("acme.com")
        assert company is not None
        assert list(company.brand_info["_sources"]) == ["openai"]

        # Same day: not retried.
        service.enrich_domain("acme.com")
        assert bf.enrich_domain.call_count == 1

        # A day later Brandfetch is back and its data is added.
        company.brand_info["_blended_at"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
        company.save()
        bf.enrich_domain.side_effect = None
        bf.enrich_domain.return_value = {"name": "Acme", "logo_url": None}
        company = service.enrich_domain("acme.com")

        assert company is not None
        assert "brandfetch" in company.brand_info["_sources"]

    def test_refresh_keeps_previous_data_when_provider_fails(
        self, company: Company
    ) -> None:
        """A refresh during an outage keeps the previous Brandfetch data."""
        bf = brandfetch_plugin({})
        bf.enrich_domain.side_effect = ProviderUnavailableError("down")
        service = build_service([bf, make_fake("openai")])

        refreshed = service.refresh_enrichment("acme.com")

        assert refreshed is not None
        sources = refreshed.brand_info["_sources"]
        assert set(sources) == {"brandfetch", "openai"}
        assert sources["brandfetch"]["fetched_at"] == "2026-11-01T00:00:00+00:00"


@pytest.mark.django_db
class TestAdminRefresh:
    """Admin refresh / purge bypass the response cache."""

    def test_refresh_request_bypasses_cache(self) -> None:
        """After request_refresh, providers are called again."""
        bf = brandfetch_plugin({"name": "Acme"})
        llm = make_fake("openai")
        service = build_service([bf, llm])
        service.enrich_domain("acme.com")
        Company.objects.filter(domain="acme.com").update(brand_info={})

        DomainEnrichmentService.request_refresh("acme.com")
        service.enrich_domain("acme.com")

        assert bf.enrich_domain.call_count == 2
        assert len(llm.calls) == 2

    def test_purge_then_reenrich_restores_correction(
        self, company: Company, workspace: Workspace
    ) -> None:
        """Corrections live in feedback rows, so a purge can't erase them."""
        record_feedback(company, workspace, "down", ("Gaming", ""))
        Company.objects.filter(pk=company.pk).update(brand_info={})

        service = build_service(
            [brandfetch_plugin({"name": "Acme"}), make_fake("openai")]
        )
        refreshed = service.enrich_domain("acme.com")

        assert refreshed is not None
        assert refreshed.brand_info["industry"] == "Gaming"
        assert refreshed.brand_info["industry_classification"]["corrected"] is True


@pytest.mark.django_db
class TestConcurrentVote:
    """A vote cast while the LLM runs is not overwritten."""

    def test_vote_during_backfill_survives(
        self, company: Company, workspace: Workspace
    ) -> None:
        """The correction recorded mid-call is applied to the saved result."""
        llm = make_fake("openai")
        original = llm.complete_json

        def vote_then_answer(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            record_feedback(company, workspace, "down", ("Gaming", "Esports"))
            return original(*args, **kwargs)

        llm.complete_json = vote_then_answer  # type: ignore[method-assign]
        service = build_service([llm])

        assert service.classify_company(company, use_cache=False) is True
        company.refresh_from_db()
        assert company.brand_info["industry"] == "Gaming"
        assert (
            company.brand_info["industry_classification"]["sub_industry"] == "Esports"
        )


@pytest.mark.django_db
class TestCorrectionsFromRows:
    """The classification reflects the current feedback rows."""

    def test_withdrawn_correction_reverts(
        self, company: Company, workspace: Workspace
    ) -> None:
        """Changing a 👎+correction to 👍 restores the model's answer."""
        record_feedback(company, workspace, "down", ("Gaming", "Esports"))
        record_feedback(company, workspace, "up")
        company.refresh_from_db()
        c = company.brand_info["industry_classification"]
        assert company.brand_info["industry"] == "Software & SaaS"
        assert c["sub_industry"] == "Developer Tools"
        assert "corrected" not in c
        assert c["feedback"] == {"up": 1, "down": 0}

    def test_latest_correction_wins_and_falls_back(
        self, company: Company, workspace: Workspace
    ) -> None:
        """With two workspaces, the newest correction applies.

        Withdrawing it brings back the other workspace's correction.
        """
        other = Workspace.objects.create(name="Other")
        record_feedback(company, workspace, "down", ("Gaming", ""))
        record_feedback(company, other, "down", ("Financial Services & Fintech", ""))
        company.refresh_from_db()
        assert company.brand_info["industry"] == "Financial Services & Fintech"

        record_feedback(company, other, "up")
        company.refresh_from_db()
        assert company.brand_info["industry"] == "Gaming"
        assert company.brand_info["industry_classification"]["feedback"] == {
            "up": 1,
            "down": 1,
        }


@pytest.mark.django_db
class TestDeletedWorkspace:
    """Links need a live workspace."""

    def test_no_links_without_workspace(self, company: Company) -> None:
        """Notifications built without a workspace carry no rating links."""
        assert feedback_urls(company, None) is None

    def test_link_for_deleted_workspace_404s(
        self, company: Company, workspace: Workspace
    ) -> None:
        """After the workspace is deleted its links stop working."""
        url = reverse(
            "core:industry_feedback", args=[make_token(company, workspace), "down"]
        )
        workspace.delete()
        response = Client().post(url, {"vote": "down"})
        assert response.status_code == 404
        assert not ClassificationFeedback.objects.exists()


@pytest.mark.django_db
class TestRoundRobinCache:
    """A cached answer from any module is reused before paying again."""

    def test_second_module_not_called_for_cached_input(self) -> None:
        """Rotation moves on, but the first module's cached answer is used."""
        first = make_fake("openai")
        second = make_fake("anthropic")
        service = build_service([first, second])

        service._classify_with_llm("acme.com", {})
        service._classify_with_llm("acme.com", {})

        assert len(first.calls) + len(second.calls) == 1

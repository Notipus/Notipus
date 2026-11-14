"""Feedback on LLM industry classifications.

Notifications carry 👍 / 👎 links. Each link holds a signed token naming the
company and the workspace that received the notification, so no login is
needed and the link cannot be forged for another company. Votes are stored
in ``ClassificationFeedback`` and summarised on the company's classification:

- ``feedback``: ``{"up": n, "down": n}`` vote counts.
- ``disputed``: more downvotes than upvotes and no correction yet.
- ``corrected``: a voter supplied the right industry. A corrected
  classification is never overwritten by the LLM again.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from core.models import ClassificationFeedback, Company, Workspace
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.urls import reverse
from plugins.enrichment.base_llm import INDUSTRIES

logger = logging.getLogger(__name__)

_TOKEN_SALT = "core.classification-feedback"
# Notifications stay in channels for a long time; keep their links working.
TOKEN_MAX_AGE_SECONDS = 365 * 24 * 3600
MAX_SUB_INDUSTRY_CHARS = 80


def make_token(company: Company, workspace: Workspace | None) -> str:
    """Sign a feedback token for a company and workspace.

    Args:
        company: Company whose classification is shown.
        workspace: Workspace that receives the notification, if known.

    Returns:
        URL-safe signed token.
    """
    payload = {"c": company.pk, "w": workspace.pk if workspace else None}
    return signing.dumps(payload, salt=_TOKEN_SALT, compress=True)


def read_token(token: str) -> tuple[int, int | None] | None:
    """Verify a feedback token.

    Args:
        token: Token from the feedback URL.

    Returns:
        ``(company_id, workspace_id)``, or None if invalid or expired.
    """
    try:
        payload = signing.loads(token, salt=_TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("c"), int):
        return None
    workspace_id = payload.get("w")
    if workspace_id is not None and not isinstance(workspace_id, int):
        return None
    return payload["c"], workspace_id


def feedback_urls(
    company: Company, workspace: Workspace | None
) -> tuple[str, str] | None:
    """Build absolute 👍 / 👎 URLs for a company's classification.

    Args:
        company: Saved company with an LLM classification.
        workspace: Workspace receiving the notification.

    Returns:
        ``(up_url, down_url)``, or None when there is nothing to vote on.
    """
    brand_info = company.brand_info or {}
    if not company.pk or not isinstance(
        brand_info.get("industry_classification"), dict
    ):
        return None
    token = make_token(company, workspace)
    base = settings.BASE_URL
    return (
        base + reverse("core:industry_feedback", args=[token, "up"]),
        base + reverse("core:industry_feedback", args=[token, "down"]),
    )


def clean_correction(industry: Any, sub_industry: Any) -> tuple[str, str] | None:
    """Validate a correction submitted with a downvote.

    Args:
        industry: Industry chosen in the form.
        sub_industry: Optional free-text sub-industry.

    Returns:
        ``(industry, sub_industry)``, or None if no valid industry was given.
    """
    if not isinstance(industry, str) or industry not in INDUSTRIES:
        return None
    sub = " ".join(str(sub_industry or "").split())[:MAX_SUB_INDUSTRY_CHARS]
    return industry, sub


@transaction.atomic
def record_feedback(
    company: Company,
    workspace: Workspace | None,
    vote: str,
    correction: tuple[str, str] | None = None,
) -> ClassificationFeedback:
    """Store a vote and update the company's classification summary.

    Args:
        company: Company being judged.
        workspace: Voting workspace, if known.
        vote: ``"up"`` or ``"down"``.
        correction: Validated ``(industry, sub_industry)`` for a downvote.

    Returns:
        The stored feedback row.

    Raises:
        ValueError: If ``vote`` is not a known value.
    """
    if vote not in (ClassificationFeedback.VOTE_UP, ClassificationFeedback.VOTE_DOWN):
        raise ValueError(f"Unknown vote: {vote}")
    if vote == ClassificationFeedback.VOTE_UP:
        correction = None

    company = Company.objects.select_for_update().get(pk=company.pk)
    brand_info = dict(company.brand_info or {})
    classification = dict(brand_info.get("industry_classification") or {})

    feedback, _ = ClassificationFeedback.objects.update_or_create(
        company=company,
        workspace=workspace,
        defaults={
            "vote": vote,
            "industry": str(classification.get("industry") or "")[:80],
            "provider": str(classification.get("provider") or "")[:40],
            "model": str(classification.get("model") or "")[:80],
            "corrected_industry": correction[0] if correction else "",
            "corrected_sub_industry": correction[1] if correction else "",
        },
    )

    votes = ClassificationFeedback.objects.filter(company=company)
    up = votes.filter(vote=ClassificationFeedback.VOTE_UP).count()
    down = votes.filter(vote=ClassificationFeedback.VOTE_DOWN).count()
    classification["feedback"] = {"up": up, "down": down}

    if correction:
        industry, sub_industry = correction
        if not classification.get("corrected"):
            classification["llm_industry"] = classification.get("industry")
        classification["industry"] = industry
        if sub_industry:
            classification["sub_industry"] = sub_industry
        classification["corrected"] = True
        classification["corrected_at"] = datetime.now(timezone.utc).isoformat()
        classification["confidence"] = "high"
        brand_info["industry"] = industry

    classification["disputed"] = bool(not classification.get("corrected") and down > up)
    brand_info["industry_classification"] = classification
    company.brand_info = brand_info
    company.save(update_fields=["brand_info"])

    logger.info(
        "Industry feedback for company %s: %s%s",
        company.pk,
        vote,
        " with correction" if correction else "",
    )
    return feedback


def carry_over_feedback(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Keep human feedback when enrichment data is regenerated.

    A corrected classification replaces the newly generated one entirely;
    otherwise vote counts are carried over onto the new classification.

    Args:
        old: The company's previous ``brand_info``.
        new: Freshly blended ``brand_info`` (modified in place).

    Returns:
        ``new``, with feedback preserved.
    """
    previous = (old or {}).get("industry_classification")
    if not isinstance(previous, dict):
        return new
    if previous.get("corrected"):
        new["industry_classification"] = previous
        new["industry"] = previous.get("industry")
        return new
    current = new.get("industry_classification")
    if isinstance(current, dict) and previous.get("feedback"):
        current["feedback"] = previous["feedback"]
    return new

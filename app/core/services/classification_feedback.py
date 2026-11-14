"""Feedback on LLM industry classifications.

Notifications carry 👍 / 👎 links. Each link holds a signed token naming the
company and the workspace that received the notification, so no login is
needed and the link cannot be forged for another company. Votes are stored
in ``ClassificationFeedback`` and summarised on the company's classification:

- ``feedback``: ``{"up": n, "down": n}`` vote counts.
- ``disputed``: more downvotes than upvotes and no correction yet.
- ``corrected``: a voter supplied the right industry. A corrected
  classification is never overwritten by the LLM again.

The ``ClassificationFeedback`` rows are the source of truth. Whenever a
company's data is regenerated (refresh, backfill, purge then re-enrich),
:func:`apply_feedback` re-derives the counts and the correction from them,
so human input survives anything that rewrites ``brand_info``.
"""

import logging
from typing import Any

from core.models import ClassificationFeedback, Company, Workspace
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.urls import reverse
from plugins.enrichment.base_llm import INDUSTRIES, clean_text

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
        workspace: Workspace receiving the notification. Votes are always
            tied to a workspace, so there are no links without one.

    Returns:
        ``(up_url, down_url)``, or None when there is nothing to vote on.
    """
    brand_info = company.brand_info or {}
    if (
        workspace is None
        or not workspace.pk
        or not company.pk
        or not isinstance(brand_info.get("industry_classification"), dict)
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
    return industry, clean_text(sub_industry, MAX_SUB_INDUSTRY_CHARS) or ""


def apply_feedback(company_id: int, brand_info: dict[str, Any]) -> dict[str, Any]:
    """Re-derive vote counts and any correction from the feedback rows.

    Args:
        company_id: Company whose feedback to apply.
        brand_info: The company's ``brand_info`` (modified in place).

    Returns:
        ``brand_info``, with ``industry`` and ``industry_classification``
        reflecting the stored feedback.
    """
    classification = brand_info.get("industry_classification")
    if not isinstance(classification, dict):
        return brand_info
    votes = ClassificationFeedback.objects.filter(company_id=company_id)
    up = votes.filter(vote=ClassificationFeedback.VOTE_UP).count()
    down = votes.filter(vote=ClassificationFeedback.VOTE_DOWN).count()
    if not (up or down) and not classification.get("corrected"):
        return brand_info

    classification = dict(classification)
    # The model's own answer, kept so a withdrawn correction can be undone.
    if classification.get("corrected"):
        llm_industry = classification.get("llm_industry")
        llm_sub_industry = classification.get("llm_sub_industry")
    else:
        llm_industry = classification.get("industry")
        llm_sub_industry = classification.get("sub_industry")

    latest = (
        votes.filter(vote=ClassificationFeedback.VOTE_DOWN)
        .exclude(corrected_industry="")
        .order_by("-updated_at", "-pk")
        .first()
    )
    if latest is not None:
        classification.update(
            {
                "industry": latest.corrected_industry,
                "sub_industry": latest.corrected_sub_industry or llm_sub_industry,
                "llm_industry": llm_industry,
                "llm_sub_industry": llm_sub_industry,
                "corrected": True,
                "corrected_at": latest.updated_at.isoformat(),
                "confidence": "high",
            }
        )
    elif classification.get("corrected"):
        # The correction was withdrawn (the voter changed their vote).
        classification["industry"] = llm_industry
        classification["sub_industry"] = llm_sub_industry
        for key in ("corrected", "corrected_at", "llm_industry", "llm_sub_industry"):
            classification.pop(key, None)

    classification["feedback"] = {"up": up, "down": down}
    classification["disputed"] = bool(not classification.get("corrected") and down > up)
    brand_info["industry_classification"] = classification
    if classification.get("industry"):
        brand_info["industry"] = classification["industry"]
    return brand_info


@transaction.atomic
def record_feedback(
    company: Company,
    workspace: Workspace,
    vote: str,
    correction: tuple[str, str] | None = None,
) -> ClassificationFeedback:
    """Store a workspace's vote and update the company's classification.

    Args:
        company: Company being judged.
        workspace: Voting workspace (one vote per workspace and company).
        vote: ``"up"`` or ``"down"``.
        correction: Validated ``(industry, sub_industry)`` for a downvote.

    Returns:
        The stored feedback row.

    Raises:
        ValueError: If ``vote`` is unknown or ``workspace`` is missing.
    """
    if vote not in (ClassificationFeedback.VOTE_UP, ClassificationFeedback.VOTE_DOWN):
        raise ValueError(f"Unknown vote: {vote}")
    if workspace is None or not workspace.pk:
        raise ValueError("Feedback must belong to a workspace")
    if vote == ClassificationFeedback.VOTE_UP:
        correction = None

    company = Company.objects.select_for_update().get(pk=company.pk)
    brand_info = dict(company.brand_info or {})
    classification = brand_info.get("industry_classification") or {}

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

    company.brand_info = apply_feedback(company.pk, brand_info)
    company.save(update_fields=["brand_info"])

    logger.info(
        "Industry feedback for company %s: %s%s",
        company.pk,
        vote,
        " with correction" if correction else "",
    )
    return feedback


def carry_over_feedback(company_id: int | None, new: dict[str, Any]) -> dict[str, Any]:
    """Keep human feedback when enrichment data is regenerated.

    Args:
        company_id: The company being regenerated (None for a new record).
        new: Freshly blended ``brand_info`` (modified in place).

    Returns:
        ``new``, with stored corrections and vote counts applied.
    """
    if not company_id:
        return new
    return apply_feedback(company_id, new)

"""Industry classification feedback, reached from notification links.

The 👍 / 👎 links in a notification open this page. It needs no login: the
signed token in the URL names the company and the workspace that received
the notification. GET only shows the page; the vote is stored on POST, so
link previewers and security scanners that prefetch URLs cannot cast votes.
"""

from typing import Any

from core.models import ClassificationFeedback, Company, Workspace
from core.services.classification_feedback import (
    clean_correction,
    read_token,
    record_feedback,
)
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from plugins.enrichment.base_llm import INDUSTRIES

_VOTES = (ClassificationFeedback.VOTE_UP, ClassificationFeedback.VOTE_DOWN)


@require_http_methods(["GET", "POST"])
def industry_feedback(request: HttpRequest, token: str, vote: str) -> HttpResponse:
    """Show and record feedback on a company's industry classification.

    Args:
        request: The HTTP request.
        token: Signed token naming the company and workspace.
        vote: ``"up"`` or ``"down"`` from the link that was clicked.

    Returns:
        The feedback page.

    Raises:
        Http404: For an unknown vote, a bad or expired token, or a company
            without a classification.
    """
    if vote not in _VOTES:
        raise Http404("Unknown vote")
    ids = read_token(token)
    if ids is None:
        raise Http404("Invalid or expired link")
    company_id, workspace_id = ids
    company = Company.objects.filter(pk=company_id).first()
    classification = (
        (company.brand_info or {}).get("industry_classification") if company else None
    )
    if company is None or not isinstance(classification, dict):
        raise Http404("Nothing to rate")
    workspace = (
        Workspace.objects.filter(pk=workspace_id).first() if workspace_id else None
    )

    context: dict[str, Any] = {
        "company": company,
        "classification": classification,
        "vote": vote,
        "industries": INDUSTRIES,
        "token": token,
        "recorded": False,
        "corrected": False,
        "error": "",
    }

    if request.method == "POST":
        posted_vote = request.POST.get("vote", vote)
        if posted_vote not in _VOTES:
            posted_vote = vote
        correction = None
        if posted_vote == ClassificationFeedback.VOTE_DOWN:
            correction = clean_correction(
                request.POST.get("industry"), request.POST.get("sub_industry")
            )
            if request.POST.get("industry") and correction is None:
                context["error"] = "Choose an industry from the list."
                return render(
                    request, "core/industry_feedback.html.j2", context, status=400
                )
        record_feedback(company, workspace, posted_vote, correction)
        company.refresh_from_db()
        context.update(
            {
                "classification": company.brand_info.get("industry_classification"),
                "vote": posted_vote,
                "recorded": True,
                "corrected": correction is not None,
            }
        )

    response = render(request, "core/industry_feedback.html.j2", context)
    response["X-Robots-Tag"] = "noindex"
    return response

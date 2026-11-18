"""Sales snapshot page: weekly and monthly statistics for the workspace."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from ..services.sales_stats import (
    BAND_LABELS,
    PERIOD_MONTH,
    PERIOD_WEEK,
    PERIODS,
    Period,
    compare,
    compute_snapshot,
    current_period,
    format_money_map,
    ledger_covers,
    period_containing,
    stored_or_computed,
    workspace_tz,
)
from .integrations.base import require_workspace


def _requested_period(request: HttpRequest, tz: Any) -> Period:
    """Return the period the query string asks for (default: this week).

    Args:
        request: The HTTP request.
        tz: Workspace timezone.

    Returns:
        The requested Period; never in the future.
    """
    kind = request.GET.get("period", PERIOD_WEEK)
    if kind not in PERIODS:
        kind = PERIOD_WEEK
    current = current_period(kind, tz)
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        return current
    requested = period_containing(kind, start, tz)
    return current if requested.start > current.start else requested


def _change(value: float | None) -> dict[str, Any] | None:
    """Return a display-ready percentage change, or None."""
    if value is None:
        return None
    tone = "success" if value > 0 else "danger" if value < 0 else "neutral"
    return {"text": f"{value:+.0f}%", "tone": tone}


def _present(
    snapshot: dict[str, Any], changes: dict[str, Any], industry: str
) -> dict[str, Any]:
    """Turn a snapshot into strings and numbers the template can render.

    Args:
        snapshot: Output of compute_snapshot (or a stored snapshot).
        changes: Output of compare().
        industry: The industry being drilled into, if any.

    Returns:
        Template context for the snapshot.
    """
    primary = snapshot["primary_currency"]
    totals = snapshot["totals"]

    def money(value: dict[str, Any] | None) -> str:
        return format_money_map(value, primary)

    industries = []
    selected = None
    for row in snapshot["industries"]:
        item = {
            "industry": row["industry"],
            "customers": row["customers"],
            "new_customers": row["new_customers"],
            "existing_customers": row["existing_customers"],
            "revenue": money(row["revenue"]),
            "share": row["share"],
            "avg": format_money_map({primary: row["avg_per_customer"]}, primary),
            "change": _change(changes["industry_revenue_pct"].get(row["industry"])),
            "bands": row["bands"],
        }
        industries.append(item)
        if row["industry"] == industry and "people" in row:
            selected = {
                "industry": row["industry"],
                "people": [
                    {**person, "revenue": money(person["revenue"])}
                    for person in row["people"]
                ],
            }

    recovery = snapshot["recovery"]
    attention = []
    for item in recovery.get("attention", []):
        next_at = item["next_attempt_at"]
        attention.append(
            {
                **item,
                "next_attempt_at": datetime.fromisoformat(next_at) if next_at else None,
            }
        )

    return {
        "label": snapshot["period"]["label"],
        "primary": primary,
        "has_data": bool(
            totals["customers"]
            or totals["cancellations"]
            or totals["refund_count"]
            or totals["trials_started"]
            or any(recovery[s]["count"] for s in ("recovered", "at_risk", "lost"))
        ),
        "revenue": money(totals["revenue"]),
        "revenue_change": _change(changes["revenue_pct"]),
        "customers": totals["customers"],
        "new_customers": totals["new_customers"],
        "new_revenue": money(totals["new_revenue"]),
        "new_customers_diff": changes["new_customers_diff"],
        "existing_customers": totals["existing_customers"],
        "existing_revenue": money(totals["existing_revenue"]),
        "existing_change": _change(changes["existing_revenue_pct"]),
        "unknown_customers": totals["unknown_customers"],
        "unknown_revenue": money(totals["unknown_revenue"]),
        "cancellations": totals["cancellations"],
        "refunds": money(totals["refunds"]),
        "refund_count": totals["refund_count"],
        "trials_started": totals["trials_started"],
        "industries": industries,
        "bands": snapshot.get("bands", list(BAND_LABELS)),
        "recovery": {
            "rate": recovery["rate"],
            "recovered": recovery["recovered"]["count"],
            "recovered_amount": money(recovery["recovered"]["amount"]),
            "at_risk": recovery["at_risk"]["count"],
            "at_risk_amount": money(recovery["at_risk"]["amount"]),
            "lost": recovery["lost"]["count"],
            "lost_amount": money(recovery["lost"]["amount"]),
            "median_days": (
                round(recovery["median_hours_to_recover"] / 24, 1)
                if recovery["median_hours_to_recover"] is not None
                else None
            ),
            "attention": attention,
        },
        "highlights": snapshot.get("highlights", []),
        "selected": selected,
    }


@login_required
def sales_stats(request: HttpRequest) -> HttpResponse:
    """Weekly / monthly sales snapshot with an industry breakdown.

    Any workspace member may view it. Customer names are decrypted from
    the ledger for the drill-down and the at-risk list; they are never
    stored in snapshots or sent in digests.

    Args:
        request: The HTTP request.

    Returns:
        The rendered page.
    """
    workspace, redirect_response = require_workspace(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    tz = workspace_tz(workspace)
    period = _requested_period(request, tz)
    now = timezone.now()
    current = current_period(period.kind, tz, now)

    partial = False
    snapshot: dict[str, Any] | None
    if ledger_covers(period, now):
        snapshot = compute_snapshot(workspace, period, include_people=True, now=now)
    else:
        snapshot, partial = stored_or_computed(workspace, period, now)

    previous, _ = stored_or_computed(workspace, period.previous(), now)
    industry = request.GET.get("industry", "")

    context: dict[str, Any] = {
        "workspace": workspace,
        "period": period,
        "period_kind": period.kind,
        "is_current": period.start == current.start,
        "noun": "week" if period.kind == PERIOD_WEEK else "month",
        "previous_start": period.previous().start.isoformat(),
        "next_start": period.following().start.isoformat()
        if period.start < current.start
        else "",
        "partial": partial,
        "tz_name": str(tz),
        "PERIOD_WEEK": PERIOD_WEEK,
        "PERIOD_MONTH": PERIOD_MONTH,
        "industry": industry,
        "snapshot": _present(snapshot, compare(snapshot, previous), industry)
        if snapshot
        else None,
        "has_previous": previous is not None,
    }
    return render(request, "core/sales_stats.html.j2", context)

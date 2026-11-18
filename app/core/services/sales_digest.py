"""Weekly and monthly sales digest delivered to a workspace's destinations.

The digest is built as an ordinary :class:`RichNotification` (type CUSTOM,
detail sections only), so every destination plugin - Slack, Teams,
Telegram - formats it natively with no digest-specific code.

Scheduling: ``send_sales_digest`` is meant to run hourly. A workspace's
digest is *due* once its local time passes 09:00 on Monday (weekly) or on
the 1st (monthly), and is sent once per period: the stored
:class:`core.models.SalesSnapshot` records ``digest_sent_at``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from core.models import SalesSnapshot
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from webhooks.models.rich_notification import (
    ActionButton,
    NotificationSeverity,
    NotificationType,
    RichNotification,
)

from .sales_stats import (
    INDUSTRY_CONSUMER,
    INDUSTRY_UNKNOWN,
    PERIOD_MONTH,
    PERIOD_WEEK,
    Period,
    compare,
    current_period,
    deserialize_money,
    format_money_map,
    store_snapshot,
    stored_or_computed,
    workspace_tz,
)

if TYPE_CHECKING:
    from core.models import Workspace

logger = logging.getLogger(__name__)

DIGEST_HOUR = 9  # local time


def _pct(value: float | None) -> str:
    """Format a percentage change, e.g. ``+12.4%``; empty without baseline."""
    if value is None:
        return ""
    return f"{value:+.1f}%"


def _diff(value: int | None, noun: str) -> str:
    """Format a count change, e.g. ``+5 customers``."""
    if value is None or value == 0:
        return ""
    return f"{value:+d} {noun}"


def _with_change(text: str, change: str) -> str:
    """Append a change in parentheses when there is one."""
    return f"{text} ({change})" if change else text


def build_digest(
    workspace: Workspace,
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None,
) -> RichNotification:
    """Build the digest notification for a period's snapshot.

    Only aggregates go into the digest - industries, totals and counts.
    Customer names stay on the dashboard, where workspace membership is
    checked.

    Args:
        workspace: The workspace.
        snapshot: The period's snapshot (from :func:`compute_snapshot`).
        previous: The previous period's snapshot, for comparison.

    Returns:
        A RichNotification any destination plugin can format.
    """
    period = snapshot["period"]
    primary = snapshot["primary_currency"]
    totals = snapshot["totals"]
    changes = compare(snapshot, previous)
    noun = "week" if period["kind"] == PERIOD_WEEK else "month"
    title = "Weekly" if period["kind"] == PERIOD_WEEK else "Monthly"

    notification = RichNotification(
        type=NotificationType.CUSTOM,
        severity=NotificationSeverity.INFO,
        headline=f"📊 {title} sales snapshot · {period['label']}",
        headline_icon="chart",
        provider="notipus",
        provider_display="Notipus",
    )

    summary = notification.add_detail_section(
        "Summary" + (f" vs previous {noun}" if previous else ""), icon="chart"
    )
    summary.add_field(
        "Revenue",
        _with_change(
            format_money_map(totals["revenue"], primary), _pct(changes["revenue_pct"])
        ),
    )
    summary.add_field(
        "New customers",
        _with_change(
            f"{totals['new_customers']} · "
            f"{format_money_map(totals['new_revenue'], primary)}",
            _diff(changes["new_customers_diff"], "customers"),
        ),
    )
    summary.add_field(
        "Existing customers",
        _with_change(
            f"{totals['existing_customers']} · "
            f"{format_money_map(totals['existing_revenue'], primary)}",
            _pct(changes["existing_revenue_pct"]),
        ),
    )
    summary.add_field(
        "Churned / refunds",
        f"{totals['cancellations']} · {format_money_map(totals['refunds'], primary)}",
    )
    if totals["unknown_customers"]:
        summary.add_field(
            "Unclassified",
            f"{totals['unknown_customers']} customers · "
            f"{format_money_map(totals['unknown_revenue'], primary)} "
            "(provider sent no new/existing signal)",
        )

    named = [
        row
        for row in snapshot["industries"]
        if row["industry"] not in (INDUSTRY_CONSUMER, INDUSTRY_UNKNOWN)
        and row["new_customers"]
    ]
    named.sort(
        key=lambda r: (
            -r["new_customers"],
            -deserialize_money(r["new_revenue"]).get(primary, Decimal("0")),
        )
    )
    if named:
        lines = []
        for index, row in enumerate(named[:3], start=1):
            line = (
                f"{index}. {row['industry']}: {row['new_customers']} new · "
                f"{format_money_map(row['new_revenue'], primary)}"
            )
            change = changes["industry_revenue_pct"].get(row["industry"])
            if change is not None:
                line += f" ({_pct(change)} revenue)"
            lines.append(line)
        notification.add_detail_section(
            "Top industries for new business", icon="trophy", text="\n".join(lines)
        )

    recovery = snapshot["recovery"]
    if any(recovery[s]["count"] for s in ("recovered", "at_risk", "lost")):
        rate = recovery["rate"]
        lines = [
            f"✅ {recovery['recovered']['count']} recovered "
            f"({format_money_map(recovery['recovered']['amount'], primary)})",
            f"⚠️ {recovery['at_risk']['count']} still at risk "
            f"({format_money_map(recovery['at_risk']['amount'], primary)})",
            f"❌ {recovery['lost']['count']} lost "
            f"({format_money_map(recovery['lost']['amount'], primary)})",
        ]
        if recovery["median_hours_to_recover"] is not None:
            days = recovery["median_hours_to_recover"] / 24
            lines.append(f"Median time to recover: {days:.1f} days")
        notification.add_detail_section(
            "Payment recovery"
            + (f" · {rate:.0f}% recovered" if rate is not None else ""),
            icon="warning",
            text="\n".join(lines),
        )

    for highlight in snapshot.get("highlights", [])[:3]:
        notification.add_detail_section("Highlight", icon="new", text=highlight)

    base_url = getattr(settings, "BASE_URL", "").rstrip("/")
    if base_url:
        url = (
            f"{base_url}{reverse('core:sales_stats')}"
            f"?period={period['kind']}&start={period['start']}"
        )
        notification.actions.append(
            ActionButton(text="Open full snapshot", url=url, style="primary")
        )
    return notification


def is_due(workspace: Workspace, kind: str, now: datetime | None = None) -> bool:
    """Return whether this workspace's digest for ``kind`` should go out now.

    Args:
        workspace: The workspace.
        kind: ``"week"`` or ``"month"``.
        now: Reference time.

    Returns:
        True from 09:00 local on Monday / the 1st onwards, for the rest of
        that day (an hourly job that misses one run still sends).
    """
    local = (now or timezone.now()).astimezone(workspace_tz(workspace))
    if local.hour < DIGEST_HOUR:
        return False
    if kind == PERIOD_WEEK:
        return local.weekday() == 0
    return local.day == 1


def last_closed_period(
    workspace: Workspace, kind: str, now: datetime | None = None
) -> Period:
    """Return the most recent fully elapsed period.

    Args:
        workspace: The workspace.
        kind: ``"week"`` or ``"month"``.
        now: Reference time.

    Returns:
        The period before the current one.
    """
    return current_period(kind, workspace_tz(workspace), now).previous()


def send_digest(
    workspace: Workspace,
    kind: str,
    *,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Store the last closed period's snapshot and deliver its digest.

    Args:
        workspace: The workspace.
        kind: ``"week"`` or ``"month"``.
        now: Reference time.
        force: Send even if this period's digest already went out.
        dry_run: Build and log, but neither store nor send.

    Returns:
        One of ``"sent"``, ``"already_sent"``, ``"no_destinations"``,
        ``"failed"`` or ``"dry_run"``.
    """
    from plugins import PluginRegistry, PluginType
    from plugins.destinations.base import BaseDestinationPlugin
    from webhooks.services.destination_credentials import collect_destinations

    now = now or timezone.now()
    period = last_closed_period(workspace, kind, now)
    existing = SalesSnapshot.objects.filter(
        workspace=workspace, period=kind, start=period.start
    ).first()
    if existing is not None and existing.digest_sent_at and not force:
        return "already_sent"

    if dry_run:
        from .sales_stats import compute_snapshot

        snapshot = compute_snapshot(workspace, period, now=now)
        previous, _ = stored_or_computed(workspace, period.previous(), now)
        notification = build_digest(workspace, snapshot, previous)
        logger.info("Dry run digest for %s: %s", workspace.uuid, notification.headline)
        return "dry_run"

    stored = store_snapshot(workspace, period, now)
    previous, _ = stored_or_computed(workspace, period.previous(), now)
    notification = build_digest(workspace, stored.data, previous)

    destinations = collect_destinations(workspace)
    if not destinations:
        return "no_destinations"

    registry = PluginRegistry.instance()
    delivered = 0
    for name, credentials in destinations:
        plugin = registry.get(PluginType.DESTINATION, name)
        if plugin is None or not isinstance(plugin, BaseDestinationPlugin):
            logger.error("Digest destination plugin %s not available", name)
            continue
        try:
            plugin.send(plugin.format(notification), credentials)
            delivered += 1
        except Exception:
            logger.exception(
                "Failed to deliver %s sales digest to %s for workspace %s",
                kind,
                name,
                workspace.uuid,
            )
    if not delivered:
        return "failed"
    stored.digest_sent_at = now
    stored.save(update_fields=["digest_sent_at", "updated_at"])
    return "sent"


def due_digests(
    workspaces: Any, now: datetime | None = None
) -> list[tuple[Workspace, str]]:
    """Return (workspace, kind) pairs whose digest is due now.

    Args:
        workspaces: Iterable of workspaces to consider.
        now: Reference time.

    Returns:
        Pairs for opted-in workspaces whose local time says it's time.
    """
    result: list[tuple[Workspace, str]] = []
    for workspace in workspaces:
        if workspace.sales_digest_weekly and is_due(workspace, PERIOD_WEEK, now):
            result.append((workspace, PERIOD_WEEK))
        if workspace.sales_digest_monthly and is_due(workspace, PERIOD_MONTH, now):
            result.append((workspace, PERIOD_MONTH))
    return result

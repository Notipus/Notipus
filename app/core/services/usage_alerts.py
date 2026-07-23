"""Billing alert emails to workspace owners and admins.

Implements the notification side of soft limit enforcement: workspace
owners and admins get a warning email when usage approaches the plan
limit, an "exceeded" email when usage crosses it (delivery continues
inside the grace window), and a "paused" email when the hard cap is
reached.

Each usage alert fires exactly once per month without any persisted
state: the usage counter increments atomically by one, so exactly one
request observes each threshold value.

Also home to the trial-ending alert, sent when Stripe fires
customer.subscription.trial_will_end (~3 days before the workspace's
own Notipus trial converts to a paid subscription).
"""

import logging
from decimal import ROUND_CEILING, Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

# How long a plan's grace multiplier may be served from cache. Bounds
# the Plan-table load during sustained over-limit traffic while letting
# database edits take effect within minutes.
GRACE_MULTIPLIER_CACHE_TTL = 300

# Percentage of the plan limit at which the "approaching your limit"
# warning email fires.
WARNING_THRESHOLD_PERCENT = 80

# Grace factor applied when a plan has no Plan row in the database (or
# the row cannot be read); per-plan policy lives in Plan.grace_multiplier.
DEFAULT_GRACE_MULTIPLIER = Decimal("2.00")


def warning_count(limit: int) -> int:
    """Return the usage count at which the warning email fires.

    Args:
        limit: Monthly event limit for the plan.

    Returns:
        Usage count that triggers the approaching-limit warning: the
        first count at or above the threshold percentage, computed with
        integer ceiling division so it never fires below it.
    """
    return max(1, -(-limit * WARNING_THRESHOLD_PERCENT // 100))


def grace_multiplier_for(plan_name: str) -> Decimal:
    """Return the soft-limit grace factor configured for a plan.

    Args:
        plan_name: Internal plan identifier (Plan.name).

    Returns:
        The plan's grace_multiplier, or the default when the plan row is
        missing or unreadable — rate limiting must degrade gracefully
        rather than fail on a database problem. Served from a short-TTL
        cache so sustained over-limit traffic does not hammer the Plan
        table.
    """
    from core.models import Plan

    cache_key = f"plan_grace_multiplier:{plan_name}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return Decimal(cached)
    except Exception:
        # A cache outage must not block the DB read; the rate limiter
        # already logs cache failures loudly.
        pass

    try:
        plan = (
            Plan.objects.filter(name=plan_name, is_active=True)
            .only("grace_multiplier")
            .first()
        )
    except Exception:
        logger.exception("Could not read grace multiplier for plan %s", plan_name)
        # Do not cache: the DB failure is transient and the fallback
        # must not outlive it.
        return DEFAULT_GRACE_MULTIPLIER

    multiplier = (
        DEFAULT_GRACE_MULTIPLIER if plan is None else Decimal(plan.grace_multiplier)
    )
    try:
        cache.set(cache_key, str(multiplier), GRACE_MULTIPLIER_CACHE_TTL)
    except Exception:
        pass
    return multiplier


def hard_limit(limit: int, plan_name: str) -> int:
    """Return the hard cap at which webhook processing stops.

    Args:
        limit: Monthly event limit for the plan.
        plan_name: Internal plan identifier (Plan.name).

    Returns:
        Usage count at which delivery is paused, never below the plan
        limit itself. Computed in Decimal with ceiling rounding so
        neither float error nor truncation can shrink the configured cap.
    """
    product = Decimal(limit) * grace_multiplier_for(plan_name)
    return max(int(product.to_integral_value(rounding=ROUND_CEILING)), limit)


def maybe_send_usage_alerts(
    workspace: Any, new_usage: int, limit: int, hard_at: int | None = None
) -> None:
    """Send threshold-crossing alert emails for a usage increment.

    Safe to call on every webhook: it only sends when ``new_usage``
    lands exactly on a threshold, and it never raises — alert delivery
    must not break webhook processing. The per-plan hard cap is only
    looked up once usage reaches the plan limit, so the common
    under-limit path adds no database query.

    Args:
        workspace: Workspace whose usage was just incremented.
        new_usage: Usage count after the atomic increment.
        limit: Monthly event limit for the workspace's plan.
        hard_at: Pre-computed hard cap, to reuse a value the caller
            already fetched instead of re-querying the plan row.
    """
    try:
        _dispatch_crossing_alert(workspace, new_usage, limit, hard_at)
    except Exception:
        logger.exception(
            "Failed to send usage alert for workspace %s at usage %s/%s",
            getattr(workspace, "uuid", "unknown"),
            new_usage,
            limit,
        )


def _dispatch_crossing_alert(
    workspace: Any, new_usage: int, limit: int, hard_at: int | None
) -> None:
    """Send the alert matching the threshold ``new_usage`` landed on, if any."""
    warn_at = warning_count(limit)
    if new_usage > limit:
        if hard_at is None:
            hard_at = hard_limit(limit, workspace.subscription_plan)
        # The cap crossing wins when both coincide (hard cap of
        # limit + 1): the pause is the actionable news, and an
        # "exceeded, still delivering" email would be false.
        if new_usage == hard_at:
            _send_paused_alert(workspace, limit, hard_at)
        elif new_usage == limit + 1:
            _send_exceeded_alert(workspace, limit, hard_at)
    elif new_usage == limit:
        if hard_at is None:
            hard_at = hard_limit(limit, workspace.subscription_plan)
        if hard_at == limit:
            # No grace window configured: the cap coincides with the
            # plan limit, so this final allowed event is the paused
            # crossing and would otherwise go unannounced.
            _send_paused_alert(workspace, limit, hard_at)
        elif new_usage == warn_at:
            _send_warning_alert(workspace, new_usage, limit)
    elif new_usage == warn_at:
        _send_warning_alert(workspace, new_usage, limit)


def _admin_emails(workspace: Any) -> list[str]:
    """Return email addresses of the workspace's owners and admins.

    Args:
        workspace: Workspace to collect recipients for.

    Returns:
        Sorted, de-duplicated list of non-empty email addresses.
    """
    from core.models import WorkspaceMember

    members = WorkspaceMember.objects.filter(
        workspace=workspace, role__in=("owner", "admin"), is_active=True
    ).select_related("user")
    return sorted({member.user.email for member in members if member.user.email})


def _billing_url() -> str:
    """Return the absolute URL of the billing dashboard."""
    return f"{settings.BASE_URL}/billing/"


def _send(subject: str, body: str, recipients: list[str]) -> None:
    """Send a plain-text alert email, logging instead of raising on failure.

    Args:
        subject: Email subject line.
        body: Plain-text email body.
        recipients: Recipient email addresses; no-op when empty.
    """
    if not recipients:
        logger.warning("Usage alert '%s' has no recipients; skipping", subject)
        return
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            fail_silently=False,
        )
        logger.info("Sent usage alert '%s' to %s", subject, recipients)
    except Exception as e:
        logger.error("Failed to send usage alert '%s': %s", subject, e, exc_info=True)


def _send_warning_alert(workspace: Any, new_usage: int, limit: int) -> None:
    """Email workspace admins that usage is approaching the plan limit."""
    subject = f"[Notipus] {workspace.name}: {new_usage} of {limit} monthly events used"
    body = f"""Hi,

Your workspace "{workspace.name}" has used {new_usage} of the {limit} events
included in your {workspace.subscription_plan} plan this month.

Notifications keep flowing as normal. If you expect more events this month,
you can upgrade your plan here:
{_billing_url()}

Your event count resets on the first day of next month.

- The Notipus Team
"""
    _send(subject, body, _admin_emails(workspace))


def _send_exceeded_alert(workspace: Any, limit: int, hard_at: int) -> None:
    """Email workspace admins that the plan limit was exceeded."""
    subject = f"[Notipus] {workspace.name} has exceeded its monthly event limit"
    body = f"""Hi,

Your workspace "{workspace.name}" has gone past the {limit} events included
in your {workspace.subscription_plan} plan this month.

Nothing has been cut off — your notifications are still being delivered.
Delivery only pauses if usage reaches {hard_at} events before your count
resets on the first day of next month.

You can upgrade any time here:
{_billing_url()}

- The Notipus Team
"""
    _send(subject, body, _admin_emails(workspace))


def send_trial_ending_alert(
    workspace: Any,
    *,
    ends_on: str | None = None,
    price: str | None = None,
    will_cancel: bool = False,
) -> None:
    """Email workspace admins that their Notipus trial is about to end.

    Only proven payload fields appear in the copy: the end date and
    converted price are omitted when Stripe did not supply them, never
    guessed. Never raises — alert delivery must not break billing
    webhook processing.

    Args:
        workspace: Workspace whose trial is ending.
        ends_on: Human-readable trial end date (e.g. "February 8, 2026"),
            or None when the payload carried no usable trial_end.
        price: Formatted recurring price the trial converts to (e.g.
            "$29.00/month"), or None when not determinable.
        will_cancel: True when the subscription is set to cancel at the
            end of the trial instead of converting.
    """
    try:
        if will_cancel:
            _send_trial_cancel_alert(workspace, ends_on)
        else:
            _send_trial_convert_alert(workspace, ends_on, price)
    except Exception:
        logger.exception(
            "Failed to send trial ending alert for workspace %s",
            getattr(workspace, "uuid", "unknown"),
        )


def _send_trial_convert_alert(
    workspace: Any, ends_on: str | None, price: str | None
) -> None:
    """Email workspace admins that the trial converts to a paid plan."""
    when = f"on {ends_on}" if ends_on else "soon"
    continues = "your subscription continues automatically"
    if price:
        continues = f"{continues} at {price}"
    subject = f"[Notipus] {workspace.name}: your trial ends {ends_on or 'soon'}"
    body = f"""Hi,

The {workspace.subscription_plan} plan trial for your workspace
"{workspace.name}" ends {when}. After that, {continues} — no action
needed to keep your notifications flowing.

If you'd like to change or cancel your plan, you can do that any time here:
{_billing_url()}

- The Notipus Team
"""
    _send(subject, body, _admin_emails(workspace))


def _send_trial_cancel_alert(workspace: Any, ends_on: str | None) -> None:
    """Email workspace admins that delivery stops when the trial ends.

    Sent when the subscription has cancel_at_period_end set, so the
    trial will not convert — the concrete consequence is that Notipus
    stops delivering notifications for the workspace.
    """
    when = f"on {ends_on}" if ends_on else "soon"
    subject = (
        f"[Notipus] {workspace.name}: your trial ends {ends_on or 'soon'} "
        f"and delivery will stop"
    )
    body = f"""Hi,

The {workspace.subscription_plan} plan trial for your workspace
"{workspace.name}" ends {when}, and your subscription is set to cancel
at that point. When it does, Notipus stops delivering notifications for
this workspace.

To keep your notifications flowing, reactivate your subscription before
the trial ends:
{_billing_url()}

- The Notipus Team
"""
    _send(subject, body, _admin_emails(workspace))


def _send_paused_alert(workspace: Any, limit: int, hard_at: int) -> None:
    """Email workspace admins that delivery is paused at the hard cap."""
    subject = f"[Notipus] {workspace.name}: notification delivery paused"
    body = f"""Hi,

Your workspace "{workspace.name}" has reached {hard_at} events this month —
that's the grace cap for your {workspace.subscription_plan} plan (limit:
{limit}). New events will not be delivered until your count resets on the
first day of next month, or immediately after you upgrade:
{_billing_url()}

- The Notipus Team
"""
    _send(subject, body, _admin_emails(workspace))

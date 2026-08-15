"""Reminder for workspaces that stalled halfway through setup.

The welcome email catches people at minute zero. It does not catch the
person who came back, connected a notification channel, hit the part that
needs work in someone else's dashboard, and left - which is the shape of
the drop-off we actually see. Such a workspace has somewhere to send
notifications and nothing to send about, so it sits silent and looks
broken.

This finds those workspaces and mails their owners and admins once. Sent
once, ever: ``Workspace.onboarding_nudge_sent_at`` is stamped on the way
out, so a job that runs daily cannot turn into a daily reminder.
"""

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from django.template.loader import render_to_string
from django.utils import timezone

from .mail import send_email
from .recipients import admin_emails

logger = logging.getLogger(__name__)

SUBJECT = "Your Notipus workspace is not receiving events yet"

#: How long to leave a workspace alone before nudging it. Long enough that
#: someone mid-setup, who steps away to open their Stripe dashboard and
#: comes back, is never interrupted by an email telling them to do the
#: thing they are already doing.
DEFAULT_MIN_AGE_HOURS = 24


def find_stalled_workspaces(min_age_hours: int = DEFAULT_MIN_AGE_HOURS) -> QuerySet:
    """Return workspaces that have a destination but no event source.

    Args:
        min_age_hours: Only consider workspaces created at least this long
            ago, so setup still in progress is left alone.

    Returns:
        Queryset of workspaces never nudged before, oldest first.
    """
    from core.models import Integration, Workspace

    cutoff = timezone.now() - timedelta(hours=min_age_hours)

    stalled: QuerySet = (
        Workspace.objects.filter(
            created_at__lte=cutoff,
            onboarding_nudge_sent_at__isnull=True,
            integrations__is_active=True,
            integrations__integration_type__in=(
                Integration.DESTINATION_INTEGRATION_TYPES
            ),
        )
        .exclude(
            integrations__is_active=True,
            integrations__integration_type__in=Integration.SOURCE_INTEGRATION_TYPES,
        )
        .distinct()
        .order_by("created_at")
    )
    return stalled


def send_nudge(workspace: Any) -> bool:
    """Email a stalled workspace's owners and admins, once.

    Args:
        workspace: Workspace to nudge.

    Returns:
        True when a message was sent and the workspace stamped.
    """
    recipients = admin_emails(workspace)
    if not recipients:
        logger.warning(
            "Workspace %s has no admin address to nudge; skipping", workspace.uuid
        )
        return False

    integrations_url = f"{settings.BASE_URL}/integrations/"
    context = {
        "workspace_name": workspace.name,
        "integrations_url": integrations_url,
        "support_email": settings.SUPPORT_EMAIL,
    }

    text_body = f"""Hi,

Your Notipus workspace "{workspace.name}" is connected to a notification
channel, but no billing tool is sending it events yet - so there is
nothing for us to post.

Connect your billing tool:
{integrations_url}

Notipus watches Stripe, Shopify, or Maxio (Chargify) and posts every
payment, failed charge, upgrade, and cancellation into your channel with
the customer's company attached. Setup takes about two minutes: add a
webhook in your billing tool's dashboard and paste the signing secret
into Notipus.

Stuck on that step? Reply to this email and a person will help. If you
have decided Notipus is not for you, no reply is needed - this is the
only reminder we will send.

- The Notipus Team
"""

    html_body = render_to_string("core/emails/onboarding_nudge.html.j2", context)

    if not send_email(
        subject=SUBJECT,
        text_body=text_body,
        recipients=recipients,
        html_body=html_body,
    ):
        return False

    # Stamp only after a successful hand-off, so a mail outage means the
    # workspace is retried on the next run rather than silently skipped.
    workspace.onboarding_nudge_sent_at = timezone.now()
    workspace.save(update_fields=["onboarding_nudge_sent_at"])
    logger.info("Nudged workspace %s (%s)", workspace.name, workspace.uuid)
    return True


def send_pending_nudges(
    min_age_hours: int = DEFAULT_MIN_AGE_HOURS, limit: int | None = None
) -> int:
    """Nudge every workspace that qualifies.

    Args:
        min_age_hours: Passed to :func:`find_stalled_workspaces`.
        limit: Stop after this many sends, so a first run against a
            backlog can be tried small before letting it loose.

    Returns:
        Number of workspaces successfully nudged.
    """
    workspaces = find_stalled_workspaces(min_age_hours)
    if limit is not None:
        workspaces = workspaces[:limit]

    return sum(1 for workspace in workspaces if send_nudge(workspace))

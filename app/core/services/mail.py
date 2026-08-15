"""Outbound product email.

One place to send mail from, so every message picks up the same envelope:
a monitored ``Reply-To``, a plain-text body, and an optional HTML
alternative. ``django.core.mail.send_mail`` cannot set ``Reply-To``, which
is why this wraps ``EmailMultiAlternatives`` instead.

Delivery failures are logged and reported through the return value rather
than raised. Email is never the point of the request that triggers it, so a
refused SMTP connection must not take down a signup, an invitation, or a
webhook.
"""

import logging
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def workspace_admin_emails(workspace: Any) -> list[str]:
    """Return email addresses of a workspace's owners and admins.

    Falls back to the ``UserProfile`` link when a workspace predates
    ``WorkspaceMember``: a message with no recipient is silently dropped,
    which is unacceptable for anything obligatory.

    Args:
        workspace: Workspace to collect recipients for.

    Returns:
        Sorted, de-duplicated list of non-empty email addresses.
    """
    from core.models import UserProfile, WorkspaceMember

    members = WorkspaceMember.objects.filter(
        workspace=workspace, role__in=("owner", "admin"), is_active=True
    ).select_related("user")
    emails = {member.user.email for member in members if member.user.email}

    if not emails:
        profiles = UserProfile.objects.filter(workspace=workspace).select_related(
            "user"
        )
        emails = {profile.user.email for profile in profiles if profile.user.email}

    return sorted(emails)


def send_email(
    subject: str,
    text_body: str,
    recipients: list[str],
    html_body: str | None = None,
    attachments: list[tuple[str, str, str]] | None = None,
) -> bool:
    """Send one message to ``recipients``.

    Args:
        subject: Subject line.
        text_body: Plain-text body, always sent as the primary part.
        recipients: Recipient addresses; a no-op when empty.
        html_body: Optional HTML alternative.
        attachments: Optional ``(filename, content, mimetype)`` tuples.

    Returns:
        True when the message was handed to the mail backend, False when
        there were no recipients or the backend raised.
    """
    if not recipients:
        logger.warning("Email '%s' has no recipients; skipping", subject)
        return False

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
        reply_to=[settings.SUPPORT_EMAIL],
    )
    if html_body:
        message.attach_alternative(html_body, "text/html")
    for filename, content, mimetype in attachments or []:
        message.attach(filename, content, mimetype)

    try:
        message.send(fail_silently=False)
    except Exception as e:
        logger.error("Failed to send '%s': %s", subject, e, exc_info=True)
        return False

    logger.info("Sent '%s' to %d recipient(s)", subject, len(recipients))
    return True

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

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def send_email(
    subject: str,
    text_body: str,
    recipients: list[str],
    html_body: str | None = None,
) -> bool:
    """Send one message to ``recipients``.

    Args:
        subject: Subject line.
        text_body: Plain-text body, always sent as the primary part.
        recipients: Recipient addresses; a no-op when empty.
        html_body: Optional HTML alternative.

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

    try:
        message.send(fail_silently=False)
    except Exception as e:
        logger.error("Failed to send '%s': %s", subject, e, exc_info=True)
        return False

    logger.info("Sent '%s' to %d recipient(s)", subject, len(recipients))
    return True

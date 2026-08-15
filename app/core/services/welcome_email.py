"""Welcome email, sent once when an account is created.

Notipus has exactly one activation moment: connecting an event source.
Until a workspace has one, no webhook ever arrives, so the dashboard stays
empty and not a single notification fires - the product looks broken
through no fault of the user. Connecting Slack alone does not get anyone
there, and that is precisely where new signups have been stopping.

So this email drives one action and no others: connect a payment provider.
No feature tour, no secondary links competing for the click.

Called from each of the three signup paths (allauth email/social via the
``user_signed_up`` signal, the custom Slack OIDC flow, and passkey
registration) rather than a single signal, because the latter two create
their users without going through allauth.
"""

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.template.loader import render_to_string

from .mail import send_email

logger = logging.getLogger(__name__)

SUBJECT = "Welcome to Notipus"


def _first_name(user: User) -> str:
    """Return a greeting name for ``user``.

    Args:
        user: The newly created user.

    Returns:
        The first name when known, otherwise the username, otherwise a
        neutral fallback so the greeting never renders as "Hi ,".
    """
    return (user.first_name or user.username or "there").strip()


def send_welcome_email(user: User) -> bool:
    """Send the welcome email to a newly created user.

    Args:
        user: The user that was just created.

    Returns:
        True when the message was handed to the mail backend.
    """
    if not user.email:
        logger.warning("Skipping welcome email: user %s has no address", user.pk)
        return False

    integrations_url = f"{settings.BASE_URL}/integrations/"
    context = {
        "first_name": _first_name(user),
        "integrations_url": integrations_url,
        "support_email": settings.SUPPORT_EMAIL,
    }

    text_body = f"""Hi {context["first_name"]},

Welcome to Notipus. Your workspace is ready.

One thing left: connect a payment provider. Notipus watches Stripe,
Shopify, or Maxio (Chargify) and posts every payment, failed charge,
upgrade, and cancellation into Slack, enriched with who the customer is
and what they are worth. Until a provider is connected there is nothing
for us to tell you about.

Connect your first payment source:
{integrations_url}

It takes about two minutes: add a webhook in your provider's dashboard
and paste the signing secret into Notipus.

Reply to this email if you get stuck. A person reads it, and we answer
within two business days.

- The Notipus Team
"""

    html_body = render_to_string("core/emails/welcome.html.j2", context)

    return send_email(
        subject=SUBJECT,
        text_body=text_body,
        recipients=[user.email],
        html_body=html_body,
    )

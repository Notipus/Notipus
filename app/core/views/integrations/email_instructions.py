"""Email integration setup instructions to a colleague.

The Notipus admin configuring an integration is often not the person
with access to the payment provider's dashboard. This view lets the
admin email the manual webhook-setup steps (endpoint URL, events to
select, where to find the signing secret) to whoever does have access,
so that person can create the webhook and hand the signing secret back
to the admin to finish the connection.

Only providers with a manual webhook setup are supported: Shopify uses
OAuth and registers its webhooks automatically, so there are no
instructions to relay - the right tool there is a workspace invitation.
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.template.loader import render_to_string

from ...models import Workspace
from .base import require_admin_role, require_post_method
from .chargify import DISPLAY_NAME as CHARGIFY_DISPLAY_NAME
from .stripe import DISPLAY_NAME as STRIPE_DISPLAY_NAME
from .stripe import STRIPE_WEBHOOK_EVENTS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SetupStep:
    """One numbered step in the emailed instructions.

    Attributes:
        title: Short step heading.
        description: What to do. May contain a ``{requester}``
            placeholder, replaced with the requesting admin's name.
    """

    title: str
    description: str


@dataclass(frozen=True)
class ProviderInstructions:
    """Emailable setup instructions for a manual-webhook provider.

    Attributes:
        display_name: Provider name shown to the recipient.
        url_slug: Path segment of the workspace webhook URL.
        integrate_route: Route name of the provider's setup page, used
            as the redirect target after sending.
        steps: Ordered setup steps.
        webhook_events: Events the recipient must select, if the
            provider requires choosing events (empty when not).
    """

    display_name: str
    url_slug: str
    integrate_route: str
    steps: list[SetupStep]
    webhook_events: list[str] = field(default_factory=list)


PROVIDER_INSTRUCTIONS: dict[str, ProviderInstructions] = {
    "stripe": ProviderInstructions(
        display_name=STRIPE_DISPLAY_NAME,
        url_slug="stripe",
        integrate_route="core:integrate_stripe",
        # Copied so mutation through either alias cannot silently desync
        # the setup page from the emailed instructions.
        webhook_events=list(STRIPE_WEBHOOK_EVENTS),
        steps=[
            SetupStep(
                title="Open the Stripe webhooks page",
                description=(
                    "In the Stripe Dashboard, go to Developers → Webhooks "
                    "(https://dashboard.stripe.com/webhooks/create) and "
                    'click "Add destination".'
                ),
            ),
            SetupStep(
                title="Select events",
                description=(
                    'Under "Events from", choose "Your account", then '
                    "search for and select each event listed below. "
                    'Click "Continue".'
                ),
            ),
            SetupStep(
                title="Choose destination type",
                description='Select "Webhook endpoint" and click "Continue".',
            ),
            SetupStep(
                title="Set the endpoint URL",
                description=(
                    "Paste the webhook URL below into the "
                    '"Endpoint URL" field and click "Create destination".'
                ),
            ),
            SetupStep(
                title="Send back the signing secret",
                description=(
                    'Stripe now shows a "Signing secret" starting with '
                    '"whsec_". Share it with {requester} over a secure '
                    "channel (such as a password manager) - not by "
                    "replying to this email. They will paste it into "
                    "Notipus to finish the connection."
                ),
            ),
        ],
    ),
    "chargify": ProviderInstructions(
        display_name=CHARGIFY_DISPLAY_NAME,
        url_slug="chargify",
        integrate_route="core:integrate_chargify",
        steps=[
            SetupStep(
                title="Open your webhook settings",
                description=(
                    "Log in to your Chargify dashboard (or Maxio Advanced "
                    "Billing) and go to Settings → Webhooks."
                ),
            ),
            SetupStep(
                title="Add the webhook URL",
                description=("Add the webhook URL below as a new webhook endpoint."),
            ),
            SetupStep(
                title="Send back the webhook secret",
                description=(
                    "Copy the webhook secret shown on the same page and "
                    "share it with {requester} over a secure channel "
                    "(such as a password manager) - not by replying to "
                    "this email. They will paste it into Notipus to "
                    "finish the connection."
                ),
            ),
            SetupStep(
                title="Send a test webhook",
                description=(
                    "Once {requester} has finished the setup, send a test "
                    "webhook from Chargify/Maxio to verify the connection."
                ),
            ),
        ],
    ),
}


def send_setup_instructions_email(
    recipient: str,
    workspace: Workspace,
    requester_name: str,
    instructions: ProviderInstructions,
) -> bool:
    """Send the provider setup instructions to a colleague.

    Args:
        recipient: Email address of the colleague with provider access.
        workspace: The workspace the integration belongs to.
        requester_name: Display name of the admin requesting help.
        instructions: The provider's instruction set.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    webhook_url = (
        f"{settings.BASE_URL}/webhook/customer/"
        f"{workspace.uuid}/{instructions.url_slug}/"
    )
    # .replace() rather than .format(): instruction copy may grow
    # literal braces (JSON snippets), which format() would choke on.
    steps = [
        SetupStep(
            title=step.title,
            description=step.description.replace("{requester}", requester_name),
        )
        for step in instructions.steps
    ]

    subject = (
        f"Help connect {instructions.display_name} to Notipus for {workspace.name}"
    )

    text_lines = [
        "Hi,",
        "",
        f"{requester_name} is setting up {instructions.display_name} "
        f"notifications for {workspace.name} on Notipus and needs help "
        f"from someone with access to the "
        f"{instructions.display_name} account.",
        "",
    ]
    for index, step in enumerate(steps, start=1):
        text_lines.append(f"{index}. {step.title}")
        text_lines.append(f"   {step.description}")
    text_lines += ["", f"Webhook URL: {webhook_url}"]
    if instructions.webhook_events:
        text_lines += ["", "Events to select:"]
        text_lines += [f"- {event}" for event in instructions.webhook_events]
    text_lines += [
        "",
        "If you weren't expecting this email, you can safely ignore it.",
        "",
        "- The Notipus Team",
    ]
    text_message = "\n".join(text_lines)

    html_message = render_to_string(
        "core/emails/setup_instructions.html.j2",
        {
            "provider_name": instructions.display_name,
            "workspace_name": workspace.name,
            "requester_name": requester_name,
            "steps": steps,
            "webhook_url": webhook_url,
            "webhook_events": instructions.webhook_events,
        },
    )

    try:
        sent_count = send_mail(
            subject=subject,
            message=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            html_message=html_message,
            fail_silently=False,
        )
    except Exception:
        logger.exception(f"Failed to send setup instructions to {recipient}")
        return False
    if sent_count == 0:
        logger.error(
            f"Email backend accepted no messages sending setup "
            f"instructions to {recipient}"
        )
        return False
    logger.info(f"{instructions.display_name} setup instructions sent to {recipient}")
    return True


@login_required
def email_setup_instructions(
    request: HttpRequest, provider: str
) -> HttpResponseRedirect:
    """Email a provider's webhook setup instructions to a colleague.

    Args:
        request: The HTTP request object. POST with ``recipient_email``.
        provider: Provider key from the URL (e.g. "stripe").

    Returns:
        Redirect to the provider's setup page (or integrations overview
        on invalid provider/method).
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    instructions = PROVIDER_INSTRUCTIONS.get(provider)
    if instructions is None:
        messages.error(
            request, "Setup instructions are not available for this integration."
        )
        return redirect("core:integrations")

    recipient = request.POST.get("recipient_email", "").strip()
    try:
        validate_email(recipient)
    except ValidationError:
        messages.error(request, "Please enter a valid email address.")
        return redirect(instructions.integrate_route)

    requester_name = request.user.get_full_name() or request.user.email  # type: ignore[union-attr]
    sent = send_setup_instructions_email(
        recipient, workspace, requester_name, instructions
    )
    if sent:
        messages.success(
            request,
            f"Setup instructions sent to {recipient}. Once they send you "
            "the secret, paste it here to finish the connection.",
        )
    else:
        messages.warning(
            request,
            f"Could not send the email to {recipient}. Please try again, "
            "or copy the instructions manually.",
        )
    return redirect(instructions.integrate_route)

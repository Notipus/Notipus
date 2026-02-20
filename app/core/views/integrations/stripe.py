"""Stripe integration views.

Handles Stripe webhook configuration for receiving payment events.
"""

import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render

from ...models import Integration, Workspace
from .base import require_admin_role, require_post_method

logger = logging.getLogger(__name__)

# Integration metadata
INTEGRATION_TYPE = "stripe_customer"
DISPLAY_NAME = "Stripe"

# Stripe webhook events to subscribe to (shown to user for manual configuration)
# Note: customer.subscription.updated and customer.subscription.deleted are not
# available in Stripe's webhook UI, but the code still handles them if they arrive
# via programmatic webhook creation.
STRIPE_WEBHOOK_EVENTS = [
    "customer.subscription.created",
    "customer.subscription.trial_will_end",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
    "invoice.paid",
    "invoice.payment_action_required",
    "checkout.session.completed",
]


@login_required
def integrate_stripe(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Stripe integration page.

    Args:
        request: The HTTP request object.

    Returns:
        Stripe integration page or redirect to workspace creation.
    """
    # Require admin role for integration configuration
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Check if Stripe is already connected
    existing_integration = Integration.objects.filter(
        workspace=workspace, integration_type=INTEGRATION_TYPE, is_active=True
    ).first()

    if request.method == "POST":
        return _handle_stripe_connect(request, workspace, existing_integration)

    # Generate webhook URL for this workspace
    webhook_url = f"{settings.BASE_URL}/webhook/customer/{workspace.uuid}/stripe/"

    context: dict[str, Any] = {
        "workspace": workspace,
        "existing_integration": existing_integration,
        "webhook_url": webhook_url,
        "webhook_events": STRIPE_WEBHOOK_EVENTS,
    }
    return render(request, "core/integrate_stripe.html.j2", context)


def _handle_stripe_connect(
    request: HttpRequest,
    workspace: Workspace,
    existing_integration: Integration | None,
) -> HttpResponseRedirect:
    """Handle POST request to connect/update Stripe integration.

    Args:
        request: The HTTP request object.
        workspace: The user's workspace.
        existing_integration: Existing integration if any.

    Returns:
        Redirect to integrations page.
    """
    webhook_secret = request.POST.get("webhook_secret", "").strip()

    if not webhook_secret:
        messages.error(request, "Please provide a webhook signing secret.")
        return redirect("core:integrate_stripe")

    # Validate webhook secret format (Stripe secrets start with "whsec_")
    if not webhook_secret.startswith("whsec_"):
        messages.error(
            request,
            "Invalid webhook secret format. "
            "Stripe webhook secrets start with 'whsec_'.",
        )
        return redirect("core:integrate_stripe")

    if existing_integration:
        # Update existing integration
        existing_integration.webhook_secret = webhook_secret
        existing_integration.webhook_verified_at = None
        existing_integration.save()
        logger.info(f"Stripe integration updated for workspace {workspace.name}")
        messages.success(request, "Stripe integration updated successfully!")
    else:
        # Create new integration
        Integration.objects.create(
            workspace=workspace,
            integration_type=INTEGRATION_TYPE,
            webhook_secret=webhook_secret,
            is_active=True,
        )
        logger.info(f"Stripe integration created for workspace {workspace.name}")
        messages.success(request, "Stripe integration connected successfully!")

    return redirect("core:integrations")


@login_required
def disconnect_stripe(request: HttpRequest) -> HttpResponseRedirect:
    """Disconnect Stripe integration.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to integrations page.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    # Require admin role for disconnection
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Find the active Stripe integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        messages.warning(request, "No active Stripe integration found")
        return redirect("core:integrations")

    # Deactivate the integration
    integration.is_active = False
    integration.save()

    logger.info(f"Stripe integration disconnected for workspace {workspace.name}")
    messages.success(
        request,
        "Stripe disconnected successfully! "
        "Remember to remove the webhook from your Stripe Dashboard.",
    )
    return redirect("core:integrations")

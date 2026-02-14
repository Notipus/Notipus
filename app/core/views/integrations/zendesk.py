"""Zendesk integration views.

Handles Zendesk webhook configuration for receiving support ticket events.
"""

import logging
import re
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
INTEGRATION_TYPE = "zendesk"
DISPLAY_NAME = "Zendesk"

# Zendesk webhook events to subscribe to (shown to user for manual configuration)
ZENDESK_WEBHOOK_EVENTS = [
    "Ticket Created",
    "Ticket Updated",
    "Ticket Solved",
    "Ticket Reopened",
    "New Comment Added",
    "Ticket Assigned",
    "Priority Changed",
]


@login_required
def integrate_zendesk(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Zendesk integration page.

    Args:
        request: The HTTP request object.

    Returns:
        Zendesk integration page or redirect to workspace creation.
    """
    # Require admin role for integration configuration
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Check if Zendesk is already connected
    existing_integration = Integration.objects.filter(
        workspace=workspace, integration_type=INTEGRATION_TYPE, is_active=True
    ).first()

    if request.method == "POST":
        return _handle_zendesk_connect(request, workspace, existing_integration)

    # Generate webhook URL for this workspace
    webhook_url = f"{settings.BASE_URL}/webhook/customer/{workspace.uuid}/zendesk/"

    context: dict[str, Any] = {
        "workspace": workspace,
        "existing_integration": existing_integration,
        "webhook_url": webhook_url,
        "webhook_events": ZENDESK_WEBHOOK_EVENTS,
    }
    return render(request, "core/integrate_zendesk.html.j2", context)


def _handle_zendesk_connect(
    request: HttpRequest,
    workspace: Workspace,
    existing_integration: Integration | None,
) -> HttpResponseRedirect:
    """Handle POST request to connect/update Zendesk integration.

    Args:
        request: The HTTP request object.
        workspace: The user's workspace.
        existing_integration: Existing integration if any.

    Returns:
        Redirect to integrations page.
    """
    webhook_secret = request.POST.get("webhook_secret", "").strip()
    zendesk_subdomain = request.POST.get("zendesk_subdomain", "").strip()

    if not webhook_secret:
        messages.error(request, "Please provide a webhook signing secret.")
        return redirect("core:integrate_zendesk")

    # Basic validation - Zendesk secrets are typically alphanumeric
    if len(webhook_secret) < 16:
        messages.error(
            request,
            "Invalid webhook secret. The secret should be at least 16 characters long.",
        )
        return redirect("core:integrate_zendesk")

    if not zendesk_subdomain:
        messages.error(
            request,
            "Please provide your Zendesk subdomain "
            "(e.g., 'mycompany' from mycompany.zendesk.com).",
        )
        return redirect("core:integrate_zendesk")

    # Clean up subdomain - remove .zendesk.com if user entered full domain
    zendesk_subdomain = zendesk_subdomain.lower().strip()
    if zendesk_subdomain.endswith(".zendesk.com"):
        zendesk_subdomain = zendesk_subdomain.replace(".zendesk.com", "")

    # Validate subdomain format (alphanumeric and hyphens only, min 2 chars)
    if len(zendesk_subdomain) < 2:
        messages.error(
            request,
            "Subdomain must be at least 2 characters long.",
        )
        return redirect("core:integrate_zendesk")

    if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]{2}$", zendesk_subdomain):
        messages.error(
            request,
            "Invalid subdomain format. Use only letters, numbers, and hyphens.",
        )
        return redirect("core:integrate_zendesk")

    # Store subdomain in settings
    integration_settings = {"zendesk_subdomain": zendesk_subdomain}

    if existing_integration:
        # Update existing integration
        existing_integration.webhook_secret = webhook_secret
        existing_integration.settings = integration_settings
        existing_integration.save()
        logger.info(f"Zendesk integration updated for workspace {workspace.name}")
        messages.success(request, "Zendesk integration updated successfully!")
    else:
        # Create new integration
        Integration.objects.create(
            workspace=workspace,
            integration_type=INTEGRATION_TYPE,
            webhook_secret=webhook_secret,
            settings=integration_settings,
            is_active=True,
        )
        logger.info(f"Zendesk integration created for workspace {workspace.name}")
        messages.success(request, "Zendesk integration connected successfully!")

    return redirect("core:integrations")


@login_required
def disconnect_zendesk(request: HttpRequest) -> HttpResponseRedirect:
    """Disconnect Zendesk integration.

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

    # Find the active Zendesk integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        messages.warning(request, "No active Zendesk integration found")
        return redirect("core:integrations")

    # Deactivate the integration
    integration.is_active = False
    integration.save()

    logger.info(f"Zendesk integration disconnected for workspace {workspace.name}")
    messages.success(
        request,
        "Zendesk disconnected successfully! "
        "Remember to remove the webhook from your Zendesk Admin Center.",
    )
    return redirect("core:integrations")

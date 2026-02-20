"""Hunter.io integration views.

Handles Hunter.io email enrichment configuration for workspaces.
Requires Pro or Enterprise plan to use this feature.

Privacy Note: Enabling this integration means customer emails will be
sent to Hunter.io for enrichment. Users must understand and consent
to this data sharing.
"""

import logging
from typing import Any

from core.permissions import has_plan_or_higher
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from plugins.enrichment.hunter import HunterPlugin

from ...models import Integration, Workspace
from .base import require_admin_role, require_post_method

logger = logging.getLogger(__name__)

# Integration metadata
INTEGRATION_TYPE = "hunter_enrichment"
DISPLAY_NAME = "Hunter.io"
MIN_PLAN = "pro"


@login_required
def integrate_hunter(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Hunter.io integration page.

    Allows Pro/Enterprise users to configure their Hunter.io API key
    for email enrichment.

    Args:
        request: The HTTP request object.

    Returns:
        Hunter integration page or redirect.
    """
    # Require admin role for integration configuration
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Check billing tier
    if not has_plan_or_higher(workspace, MIN_PLAN):
        messages.warning(
            request,
            "Hunter.io email enrichment requires a Pro or Enterprise plan. "
            "Please upgrade to access this feature.",
        )
        return redirect("core:integrations")

    # Check if Hunter integration exists (active or inactive)
    existing_integration = Integration.objects.filter(
        workspace=workspace, integration_type=INTEGRATION_TYPE
    ).first()

    if request.method == "POST":
        assert workspace is not None
        return _handle_hunter_connect(request, workspace, existing_integration)

    # Check if actively connected with API key
    is_connected = bool(
        existing_integration
        and existing_integration.is_active
        and existing_integration.integration_settings.get("api_key")
    )

    context: dict[str, Any] = {
        "workspace": workspace,
        "existing_integration": existing_integration if is_connected else None,
        "has_api_key": is_connected,
    }
    return render(request, "core/integrate_hunter.html.j2", context)


def _handle_hunter_connect(
    request: HttpRequest,
    workspace: Workspace,
    existing_integration: Integration | None,
) -> HttpResponseRedirect:
    """Handle POST request to connect/update Hunter integration.

    Args:
        request: The HTTP request object.
        workspace: The user's workspace.
        existing_integration: Existing integration if any.

    Returns:
        Redirect to integrations page.
    """
    api_key = request.POST.get("api_key", "").strip()

    if not api_key:
        messages.error(request, "Please provide a Hunter.io API key.")
        return redirect("core:integrate_hunter")

    # Basic validation - Hunter API keys are typically 40 character hex strings
    if len(api_key) < 20:
        messages.error(
            request,
            "Invalid API key format. Please check your Hunter.io API key.",
        )
        return redirect("core:integrate_hunter")

    # Verify API key with Hunter.io
    hunter = HunterPlugin()
    is_valid, message = hunter.verify_api_key(api_key)

    if not is_valid:
        messages.error(request, f"Could not verify API key: {message}")
        return redirect("core:integrate_hunter")

    if existing_integration:
        # Update existing integration (reactivate if needed)
        existing_integration.integration_settings["api_key"] = api_key
        existing_integration.is_active = True
        existing_integration.save()
        logger.info(f"Hunter integration updated for workspace {workspace.name}")
        messages.success(request, "Hunter.io integration connected successfully!")
    else:
        # Create new integration
        Integration.objects.create(
            workspace=workspace,
            integration_type=INTEGRATION_TYPE,
            integration_settings={"api_key": api_key},
            is_active=True,
        )
        logger.info(f"Hunter integration created for workspace {workspace.name}")
        messages.success(request, "Hunter.io integration connected successfully!")

    return redirect("core:integrations")


@login_required
def disconnect_hunter(request: HttpRequest) -> HttpResponseRedirect:
    """Disconnect Hunter.io integration.

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

    # Find the active Hunter integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        messages.warning(request, "No active Hunter.io integration found")
        return redirect("core:integrations")

    # Deactivate the integration (don't delete - keeps history)
    integration.is_active = False
    integration.save()

    assert workspace is not None
    logger.info(f"Hunter integration disconnected for workspace {workspace.name}")
    messages.success(request, "Hunter.io disconnected successfully!")
    return redirect("core:integrations")

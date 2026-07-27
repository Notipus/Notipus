"""Microsoft Teams integration views.

Teams notifications use a Power Automate **Workflows** incoming webhook (the
successor to the retiring Office 365 connector webhooks): the user creates a
"Post to a channel when a webhook request is received" workflow in Teams and
pastes its HTTPS URL here. Unlike Slack there's no OAuth — it's a single
bearer-secret URL, so we treat it like the Slack webhook (never rendered back
into the form, never logged).
"""

import logging
from typing import cast

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render

from ...models import Integration, Workspace
from .base import (
    DEFAULT_API_TIMEOUT,
    get_user_workspace,
    require_admin_role,
    require_post_method,
)

logger = logging.getLogger(__name__)

INTEGRATION_TYPE = "teams_notifications"
DISPLAY_NAME = "Microsoft Teams"


def _is_valid_webhook_url(url: str) -> bool:
    """Cheap sanity check for a Teams Workflows webhook URL.

    The Workflows webhook has no validation endpoint, so we only require an
    HTTPS URL here and rely on the "Test" button for real verification.

    Args:
        url: The candidate webhook URL.

    Returns:
        True if the URL is a plausible HTTPS webhook.
    """
    return url.startswith("https://")


@login_required
def connect_teams(request: HttpRequest) -> HttpResponse:
    """Display the Teams connection form or process a submission.

    Args:
        request: The HTTP request object.

    Returns:
        The form on GET, a redirect on POST.
    """
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    if request.method == "GET":
        existing = Integration.objects.filter(
            workspace=workspace,
            integration_type=INTEGRATION_TYPE,
            is_active=True,
        ).first()
        return render(
            request,
            "core/teams_connect.html.j2",
            {"existing_integration": existing, "workspace": workspace},
        )

    webhook_url = request.POST.get("webhook_url", "").strip()
    if not webhook_url:
        messages.error(request, "Webhook URL is required")
        return redirect("core:teams_connect")
    if not _is_valid_webhook_url(webhook_url):
        messages.error(request, "Enter a valid https:// Teams webhook URL")
        return redirect("core:teams_connect")

    integration, created = Integration.objects.get_or_create(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        defaults={
            "oauth_credentials": {"webhook_url": webhook_url},
            "is_active": True,
        },
    )
    if not created:
        integration.oauth_credentials = {"webhook_url": webhook_url}
        integration.is_active = True
        integration.save()
        messages.success(request, "Microsoft Teams connection updated successfully!")
    else:
        messages.success(request, "Microsoft Teams connected successfully!")

    return redirect("core:integrations")


@login_required
def disconnect_teams(request: HttpRequest) -> HttpResponseRedirect:
    """Deactivate the Teams integration.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to the integrations page.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()
    if integration:
        integration.is_active = False
        integration.save()
        messages.success(request, "Microsoft Teams disconnected successfully!")
    else:
        messages.warning(request, "No active Microsoft Teams integration found")

    return redirect("core:integrations")


@login_required
def test_teams(request: HttpRequest) -> HttpResponseRedirect:
    """Post a test Adaptive Card to the connected Teams channel.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to the integrations page with a status message.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    # Posting to the channel is a mutation, so gate on admin/owner role.
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()
    if not integration:
        messages.error(request, "No active Microsoft Teams integration found")
        return redirect("core:integrations")

    webhook_url = integration.oauth_credentials.get("webhook_url")
    if not webhook_url:
        messages.error(request, "Teams integration is missing its webhook URL")
        return redirect("core:integrations")

    try:
        response = requests.post(
            webhook_url,
            json=_build_test_card(request, workspace),
            timeout=DEFAULT_API_TIMEOUT,
        )
        response.raise_for_status()
        messages.success(request, "Test message sent successfully!")
    except requests.exceptions.Timeout:
        logger.error("Teams test message timed out")
        messages.error(request, "Request timed out. Please try again.")
    except requests.exceptions.RequestException as e:
        # The webhook URL is a bearer secret embedded in requests exception
        # messages; log only the exception type.
        logger.error(f"Teams test message failed: {type(e).__name__}")
        messages.error(request, "Failed to send test message. Please try again.")

    return redirect("core:integrations")


def _build_test_card(request: HttpRequest, workspace: Workspace) -> dict:
    """Build the Adaptive Card envelope for the test message.

    Args:
        request: The HTTP request object.
        workspace: The user's workspace.

    Returns:
        The Teams webhook payload for a simple confirmation card.
    """
    username = cast(User, request.user).username
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "🐙 Test message from Notipus!",
                            "weight": "Bolder",
                            "size": "Large",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": (
                                "Your Microsoft Teams integration is working. "
                                "You'll receive payment and subscription "
                                "notifications here."
                            ),
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": f"Sent by {username} from {workspace.name}",
                            "wrap": True,
                            "isSubtle": True,
                            "spacing": "Small",
                        },
                    ],
                },
            }
        ],
    }


@login_required
def get_teams_status(request: HttpRequest) -> JsonResponse:
    """Return the current Teams integration status as JSON.

    Args:
        request: The HTTP request object.

    Returns:
        JSON with the connection status.
    """
    workspace = get_user_workspace(request)
    if not workspace:
        return JsonResponse({"error": "User profile not found"}, status=400)

    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
    ).first()
    if not integration:
        return JsonResponse({"connected": False, "is_active": False})

    return JsonResponse({"connected": True, "is_active": integration.is_active})

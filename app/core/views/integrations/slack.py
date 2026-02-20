"""Slack integration views.

Handles Slack OAuth connection for receiving notifications in Slack channels.
"""

import json
import logging
from typing import cast

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect

from ...models import Integration, Workspace
from .base import (
    DEFAULT_API_TIMEOUT,
    get_user_workspace,
    require_admin_role,
    require_post_method,
)

logger = logging.getLogger(__name__)

# Integration metadata
INTEGRATION_TYPE = "slack_notifications"
DISPLAY_NAME = "Slack"


@login_required
def integrate_slack(request: HttpRequest) -> HttpResponseRedirect:
    """Start Slack integration flow.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to Slack connect.
    """
    return redirect("core:slack_connect")


def slack_connect(request: HttpRequest) -> HttpResponseRedirect:
    """Initialize Slack connection for workspace notifications.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to Slack OAuth authorization.
    """
    scopes = "incoming-webhook,chat:write,channels:read"
    auth_url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={settings.SLACK_CLIENT_ID}"
        f"&scope={scopes}"
        f"&redirect_uri={settings.SLACK_CONNECT_REDIRECT_URI}"
        f"&response_type=code"
    )
    return redirect(auth_url)


def slack_connect_callback(
    request: HttpRequest,
) -> HttpResponse | HttpResponseRedirect:
    """Handle Slack OAuth callback for workspace notifications.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to integrations page on success, error response on failure.
    """
    code = request.GET.get("code")
    if not code:
        return HttpResponse("Authorization failed: No code provided", status=400)

    # Exchange code for token
    try:
        response = requests.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": settings.SLACK_CLIENT_ID,
                "client_secret": settings.SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.SLACK_CONNECT_REDIRECT_URI,
            },
            timeout=DEFAULT_API_TIMEOUT,
        )
        data = response.json()
    except requests.exceptions.Timeout:
        logger.error("Slack OAuth token exchange timed out")
        return HttpResponse("Slack connection timed out. Please try again.", status=504)
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack OAuth request failed: {e!s}")
        return HttpResponse("Slack connection failed. Please try again.", status=502)

    if not data.get("ok"):
        return HttpResponse(f"Slack connection failed: {data.get('error')}", status=400)

    # Get user's workspace (require admin role for modifications)
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Store or update Slack integration
    integration, created = Integration.objects.get_or_create(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        defaults={
            "oauth_credentials": {
                "access_token": data["access_token"],
                "team": data["team"],
                "incoming_webhook": data.get("incoming_webhook", {}),
            },
            "integration_settings": {
                "channel": data.get("incoming_webhook", {}).get("channel", "#general"),
                "team_id": data["team"]["id"],
            },
            "is_active": True,
        },
    )

    if not created:
        # Update existing integration
        integration.oauth_credentials = {
            "access_token": data["access_token"],
            "team": data["team"],
            "incoming_webhook": data.get("incoming_webhook", {}),
        }
        integration.integration_settings = {
            "channel": data.get("incoming_webhook", {}).get("channel", "#general"),
            "team_id": data["team"]["id"],
        }
        integration.is_active = True
        integration.save()

    messages.success(request, "Slack connected successfully!")
    return redirect("core:integrations")


@login_required
def disconnect_slack(request: HttpRequest) -> HttpResponseRedirect:
    """Disconnect Slack integration.

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

    # Find and deactivate the Slack integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if integration:
        integration.is_active = False
        integration.save()
        messages.success(request, "Slack disconnected successfully!")
    else:
        messages.warning(request, "No active Slack integration found")

    return redirect("core:integrations")


@login_required
def test_slack(request: HttpRequest) -> HttpResponseRedirect:
    """Send a test message to the connected Slack workspace.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to integrations page with status message.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    workspace = get_user_workspace(request)
    if not workspace:
        return redirect("core:create_workspace")

    # Find the active Slack integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        messages.error(request, "No active Slack integration found")
        return redirect("core:integrations")

    # Get webhook URL from integration
    webhook_url = integration.oauth_credentials.get("incoming_webhook", {}).get("url")

    if not webhook_url:
        # Try to use chat:write with the access token instead
        _send_test_via_api(request, integration, workspace)
    else:
        # Use incoming webhook
        _send_test_via_webhook(request, webhook_url, workspace)

    return redirect("core:integrations")


def _send_test_via_api(
    request: HttpRequest, integration: Integration, workspace: Workspace
) -> None:
    """Send test message via Slack API (chat:write).

    Args:
        request: The HTTP request object.
        integration: The Slack integration.
        workspace: The user's workspace.
    """
    access_token = integration.oauth_credentials.get("access_token")
    channel = integration.integration_settings.get("channel", "#general")

    if not access_token:
        messages.error(request, "Slack integration is missing credentials")
        return

    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {access_token}"},
            json=_build_test_message(request, workspace, channel),
            timeout=DEFAULT_API_TIMEOUT,
        )
        data = response.json()
        if data.get("ok"):
            messages.success(request, f"Test message sent to {channel} successfully!")
        else:
            error = data.get("error", "Unknown error")
            messages.error(request, f"Failed to send test message: {error}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack test message failed: {e!s}")
        messages.error(request, "Failed to send test message. Please try again.")


def _send_test_via_webhook(
    request: HttpRequest, webhook_url: str, workspace: Workspace
) -> None:
    """Send test message via incoming webhook.

    Args:
        request: The HTTP request object.
        webhook_url: The webhook URL.
        workspace: The user's workspace.
    """
    try:
        response = requests.post(
            webhook_url,
            json=_build_test_message(request, workspace),
            timeout=DEFAULT_API_TIMEOUT,
        )
        response.raise_for_status()
        messages.success(request, "Test message sent successfully!")
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack webhook test failed: {e!s}")
        messages.error(request, "Failed to send test message. Please try again.")


def _build_test_message(
    request: HttpRequest, workspace: Workspace, channel: str | None = None
) -> dict:
    """Build the test message payload.

    Args:
        request: The HTTP request object.
        workspace: The user's workspace.
        channel: Optional channel to post to.

    Returns:
        The message payload dict.
    """
    payload = {
        "text": "🐙 *Test message from Notipus!*",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "🐙 *Test message from Notipus!*\n\n"
                        "Your Slack integration is working perfectly. "
                        "You'll receive payment and subscription "
                        "notifications here."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Sent by {cast(User, request.user).username} "
                        f"from {workspace.name}",
                    }
                ],
            },
        ],
    }
    if channel:
        payload["channel"] = channel
    return payload


@login_required
def get_slack_channels(request: HttpRequest) -> JsonResponse:
    """Fetch available Slack channels for configuration.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with list of channels or error.
    """
    workspace = get_user_workspace(request)
    if not workspace:
        return JsonResponse({"error": "User profile not found"}, status=400)

    # Find the active Slack integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        return JsonResponse({"error": "No active Slack integration found"}, status=404)

    access_token = integration.oauth_credentials.get("access_token")
    if not access_token:
        return JsonResponse({"error": "Slack credentials missing"}, status=400)

    try:
        # Fetch public channels
        response = requests.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "types": "public_channel",
                "exclude_archived": "true",
                "limit": "200",
            },
            timeout=DEFAULT_API_TIMEOUT,
        )
        data = response.json()

        if not data.get("ok"):
            error = data.get("error", "Unknown error")
            logger.error(f"Failed to fetch Slack channels: {error}")
            return JsonResponse(
                {"error": f"Failed to fetch channels: {error}"}, status=400
            )

        channels = [
            {"id": ch["id"], "name": ch["name"]} for ch in data.get("channels", [])
        ]

        # Sort channels alphabetically
        channels.sort(key=lambda x: x["name"])

        # Get current channel
        current_channel = integration.integration_settings.get("channel", "#general")

        return JsonResponse(
            {
                "channels": channels,
                "current_channel": current_channel,
            }
        )

    except requests.exceptions.Timeout:
        logger.error("Slack channels fetch timed out")
        return JsonResponse({"error": "Request timed out"}, status=504)
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack channels fetch failed: {e!s}")
        return JsonResponse({"error": "Failed to fetch channels"}, status=502)


@login_required
def configure_slack(request: HttpRequest) -> JsonResponse:
    """Update Slack integration channel configuration.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with success status or error.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    workspace = get_user_workspace(request)
    if not workspace:
        return JsonResponse({"error": "User profile not found"}, status=400)

    # Find the active Slack integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        return JsonResponse({"error": "No active Slack integration found"}, status=404)

    try:
        data = json.loads(request.body)
        channel = data.get("channel")

        if not channel:
            return JsonResponse({"error": "Channel is required"}, status=400)

        # Update the channel in integration settings
        integration.integration_settings["channel"] = channel
        integration.save()

        logger.info(
            f"Slack channel updated to {channel} for workspace {workspace.name}"
        )

        return JsonResponse(
            {
                "success": True,
                "channel": channel,
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

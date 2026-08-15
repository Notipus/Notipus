"""Telegram integration views.

Handles Telegram bot connection for receiving notifications in Telegram chats.
Unlike Slack (which uses OAuth), Telegram uses direct bot token + chat ID configuration.
"""

import html
import json
import logging
from typing import cast

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from ...models import Integration, Workspace
from .base import (
    DEFAULT_API_TIMEOUT,
    get_user_workspace,
    require_admin_role,
    require_post_method,
)

logger = logging.getLogger(__name__)

# Integration metadata
INTEGRATION_TYPE = "telegram_notifications"
DISPLAY_NAME = "Telegram"

# Telegram Bot API base URL
TELEGRAM_API_BASE = "https://api.telegram.org/bot"


def _validate_bot_token(bot_token: str) -> dict | None:
    """Validate Telegram bot token by calling getMe API.

    Args:
        bot_token: The Telegram bot token to validate.

    Returns:
        Bot info dict if valid, None if invalid.
    """
    try:
        response = requests.get(
            f"{TELEGRAM_API_BASE}{bot_token}/getMe",
            timeout=DEFAULT_API_TIMEOUT,
        )
        data = response.json()

        if data.get("ok"):
            return cast("dict | None", data.get("result"))
        return None

    except (requests.exceptions.RequestException, ValueError) as e:
        # ValueError covers response.json() decode failures (non-JSON body,
        # e.g. an HTML error page from a proxy) — treat as invalid. Log only
        # the exception type: requests messages embed the /bot<token>/ URL.
        logger.error(f"Telegram bot validation failed: {type(e).__name__}")
        return None


def _validate_chat_id(bot_token: str, chat_id: str) -> bool:
    """Validate chat ID by attempting to get chat info.

    Args:
        bot_token: The Telegram bot token.
        chat_id: The chat ID to validate.

    Returns:
        True if chat ID is valid and bot has access, False otherwise.
    """
    try:
        response = requests.get(
            f"{TELEGRAM_API_BASE}{bot_token}/getChat",
            params={"chat_id": chat_id},
            timeout=DEFAULT_API_TIMEOUT,
        )
        data = response.json()
        return bool(data.get("ok", False))

    except (requests.exceptions.RequestException, ValueError) as e:
        # ValueError covers response.json() decode failures (non-JSON body).
        # Log only the exception type: requests messages embed the token URL.
        logger.error(f"Telegram chat validation failed: {type(e).__name__}")
        return False


@login_required
def connect_telegram(request: HttpRequest) -> HttpResponse:
    """Display Telegram connection form or process form submission.

    For GET: Display form to enter bot token and chat ID.
    For POST: Validate credentials and save integration.

    Args:
        request: The HTTP request object.

    Returns:
        Form page on GET, redirect on POST success/failure.
    """
    # Require admin role for modifications
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    if request.method == "GET":
        # Check if already connected
        existing = Integration.objects.filter(
            workspace=workspace,
            integration_type=INTEGRATION_TYPE,
            is_active=True,
        ).first()

        return render(
            request,
            "core/telegram_connect.html.j2",
            {
                "existing_integration": existing,
                "workspace": workspace,
            },
        )

    # POST: Process form submission
    bot_token = request.POST.get("bot_token", "").strip()
    chat_id = request.POST.get("chat_id", "").strip()

    if not bot_token or not chat_id:
        messages.error(request, "Bot token and Chat ID are required")
        return redirect("core:telegram_connect")

    # Validate bot token
    bot_info = _validate_bot_token(bot_token)
    if not bot_info:
        messages.error(
            request,
            "Invalid bot token. Please check your token from @BotFather.",
        )
        return redirect("core:telegram_connect")

    # Validate chat ID (optional but recommended)
    if not _validate_chat_id(bot_token, chat_id):
        messages.warning(
            request,
            "Could not verify chat ID. Make sure the bot has been added to "
            "the chat/channel. Connection saved anyway - you can test it below.",
        )

    # Store or update Telegram integration
    integration, created = Integration.objects.get_or_create(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        defaults={
            "oauth_credentials": {
                "bot_token": bot_token,
                "chat_id": chat_id,
                "bot_info": bot_info,
            },
            "integration_settings": {
                "bot_username": bot_info.get("username"),
                "bot_id": bot_info.get("id"),
            },
            "is_active": True,
        },
    )

    if not created:
        # Update existing integration
        integration.oauth_credentials = {
            "bot_token": bot_token,
            "chat_id": chat_id,
            "bot_info": bot_info,
        }
        integration.integration_settings = {
            "bot_username": bot_info.get("username"),
            "bot_id": bot_info.get("id"),
        }
        integration.is_active = True
        integration.save()
        messages.success(request, "Telegram connection updated successfully!")
    else:
        messages.success(request, "Telegram connected successfully!")

    return redirect("core:integrations")


@login_required
def disconnect_telegram(request: HttpRequest) -> HttpResponseRedirect:
    """Disconnect Telegram integration.

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

    # Find and deactivate the Telegram integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if integration:
        integration.is_active = False
        integration.save()
        messages.success(request, "Telegram disconnected successfully!")
    else:
        messages.warning(request, "No active Telegram integration found")

    return redirect("core:integrations")


@login_required
def test_telegram(request: HttpRequest) -> HttpResponseRedirect:
    """Send a test message to the connected Telegram chat.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to integrations page with status message.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    # Sending a test message hits the workspace's chat, so gate it on
    # admin/owner role — otherwise any member could spam the channel.
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    # Find the active Telegram integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        messages.error(request, "No active Telegram integration found")
        return redirect("core:integrations")

    # Get credentials from integration
    bot_token = integration.oauth_credentials.get("bot_token")
    chat_id = integration.oauth_credentials.get("chat_id")

    if not bot_token or not chat_id:
        messages.error(request, "Telegram integration is missing credentials")
        return redirect("core:integrations")

    # Send test message
    try:
        response = requests.post(
            f"{TELEGRAM_API_BASE}{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": _build_test_message(request, workspace),
                "parse_mode": "HTML",
                "link_preview_options": json.dumps({"is_disabled": True}),
            },
            timeout=DEFAULT_API_TIMEOUT,
        )
        data = response.json()

        if data.get("ok"):
            messages.success(request, "Test message sent successfully!")
        else:
            error = data.get("description", "Unknown error")
            messages.error(request, f"Failed to send test message: {error}")

    except requests.exceptions.Timeout:
        logger.error("Telegram test message timed out")
        messages.error(request, "Request timed out. Please try again.")
    except (requests.exceptions.RequestException, ValueError) as e:
        # ValueError covers response.json() decode failures (non-JSON body).
        # Log only the exception type: requests messages embed the token URL.
        logger.error(f"Telegram test message failed: {type(e).__name__}")
        messages.error(request, "Failed to send test message. Please try again.")

    return redirect("core:integrations")


def _build_test_message(request: HttpRequest, workspace: Workspace) -> str:
    """Build the test message HTML.

    Args:
        request: The HTTP request object.
        workspace: The user's workspace.

    Returns:
        HTML formatted test message string.
    """
    # Escape user- and workspace-controlled values: this is an HTML
    # (parse_mode=HTML) message, so an unescaped < or & would break the
    # markup. html.escape emits &lt;/&gt;/&amp;/&quot;, all Telegram-safe.
    username = html.escape(cast(User, request.user).username)
    workspace_name = html.escape(workspace.name)
    # Both interpolated values are html.escape'd above.
    # nosemgrep
    return (
        "🐙 <b>Test message from Notipus!</b>\n\n"
        "Your Telegram integration is working perfectly. "
        "You'll receive payment and subscription notifications here.\n\n"
        f"<i>Sent by {username} from {workspace_name}</i>"
    )


@login_required
@require_http_methods(["POST"])
def configure_telegram(request: HttpRequest) -> JsonResponse:
    """Update Telegram integration settings.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with success status or error.
    """
    # Changing the chat_id re-routes where notifications go, so require
    # admin/owner role (consistent with configure_slack). Return 403 JSON
    # rather than a redirect since this is a fetch endpoint.
    workspace, redirect_response = require_admin_role(request)
    if redirect_response is not None:
        return JsonResponse({"error": "Admin or owner role required"}, status=403)
    assert workspace is not None

    # Find the active Telegram integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        return JsonResponse(
            {"error": "No active Telegram integration found"}, status=404
        )

    try:
        data = json.loads(request.body)
        chat_id = data.get("chat_id")

        if not chat_id:
            return JsonResponse({"error": "Chat ID is required"}, status=400)

        # Validate new chat ID
        bot_token = integration.oauth_credentials.get("bot_token")
        if bot_token and not _validate_chat_id(bot_token, chat_id):
            return JsonResponse(
                {"error": "Invalid chat ID or bot doesn't have access"},
                status=400,
            )

        # Update the chat_id in oauth_credentials
        integration.oauth_credentials["chat_id"] = chat_id
        integration.save()

        logger.info(f"Telegram chat_id updated for workspace {workspace.name}")

        return JsonResponse(
            {
                "success": True,
                "chat_id": chat_id,
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)


@login_required
def get_telegram_status(request: HttpRequest) -> JsonResponse:
    """Get current Telegram integration status.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with integration status.
    """
    workspace = get_user_workspace(request)
    if not workspace:
        return JsonResponse({"error": "User profile not found"}, status=400)

    # Find the Telegram integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
    ).first()

    if not integration:
        return JsonResponse(
            {
                "connected": False,
                "is_active": False,
            }
        )

    bot_info = integration.oauth_credentials.get("bot_info", {})

    return JsonResponse(
        {
            "connected": True,
            "is_active": integration.is_active,
            "bot_username": bot_info.get("username"),
            "chat_id": integration.oauth_credentials.get("chat_id"),
        }
    )

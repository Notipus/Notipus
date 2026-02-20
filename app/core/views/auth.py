"""Authentication views for Slack OAuth login.

This module handles user authentication via Slack OpenID Connect.
"""

import logging
from typing import Any

import requests
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect

from ..models import UserProfile

logger = logging.getLogger(__name__)

# Default timeout for external API requests (seconds)
SLACK_API_TIMEOUT = 30


def home(request: HttpRequest) -> HttpResponse:
    """Render the home page.

    Args:
        request: The HTTP request object.

    Returns:
        Simple welcome message response.
    """
    return HttpResponse("Welcome to the Django Project!")


def landing(request: HttpRequest) -> HttpResponseRedirect:
    """Redirect to appropriate page based on authentication status.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to dashboard if authenticated, otherwise to login page.
    """
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    return redirect("account_login")


def slack_auth(request: HttpRequest) -> HttpResponseRedirect:
    """Redirect to Slack OAuth for user authentication.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to Slack authorization URL.
    """
    scopes = "openid,email,profile"
    auth_url = (
        f"https://slack.com/openid/connect/authorize"
        f"?client_id={settings.SLACK_CLIENT_ID}"
        f"&scope={scopes}"
        f"&redirect_uri={settings.SLACK_REDIRECT_URI}"
        f"&response_type=code"
    )
    return redirect(auth_url)


def _get_slack_token(code: str) -> dict[str, Any] | None:
    """Exchange OAuth code for access token.

    Args:
        code: OAuth authorization code from Slack.

    Returns:
        Token data dictionary, or None on failure.
    """
    try:
        response = requests.post(
            "https://slack.com/api/openid.connect.token",
            data={
                "client_id": settings.SLACK_CLIENT_ID,
                "client_secret": settings.SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.SLACK_REDIRECT_URI,
            },
            timeout=SLACK_API_TIMEOUT,
        )
        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            error = data.get("error", "unknown")
            logger.warning(f"Slack token exchange failed: {error}")
            return None
        return data
    except requests.exceptions.Timeout:
        logger.error("Slack token exchange request timed out")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack token exchange request failed: {e!s}")
        return None


def _get_slack_user_info(access_token: str) -> dict[str, Any] | None:
    """Get user information from Slack.

    Args:
        access_token: Slack OAuth access token.

    Returns:
        User info dictionary, or None on failure.
    """
    try:
        response = requests.get(
            "https://slack.com/api/openid.connect.userInfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=SLACK_API_TIMEOUT,
        )
        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            logger.warning(f"Slack userInfo failed: {data.get('error', 'unknown')}")
            return None
        return data
    except requests.exceptions.Timeout:
        logger.error("Slack userInfo request timed out")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack userInfo request failed: {e!s}")
        return None


def slack_auth_callback(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Handle Slack OAuth callback for user authentication.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to dashboard on success, error response on failure.
    """
    code = request.GET.get("code")
    if not code:
        return HttpResponse("Authorization failed: No code provided", status=400)

    # Exchange code for token
    token_data = _get_slack_token(code)
    if not token_data:
        return HttpResponse("Failed to get access token", status=400)

    # Get user info
    user_info = _get_slack_user_info(token_data["access_token"])
    if not user_info:
        return HttpResponse("Failed to get user information", status=400)

    # Extract user details
    slack_id = user_info.get("sub")
    email = user_info.get("email")
    name = user_info.get("name", "")

    if not slack_id or not email:
        return HttpResponse("Invalid user data from Slack", status=400)

    # Find or create user
    user, created = User.objects.get_or_create(
        email=email, defaults={"username": email, "first_name": name}
    )

    if created:
        logger.info(f"Created new user: {email}")

    # Try to find existing UserProfile
    try:
        profile = UserProfile.objects.get(slack_user_id=slack_id)
        if profile.user != user:
            # Link existing profile to this user account
            profile.user = user
            profile.save()
        user = profile.user
    except UserProfile.DoesNotExist:
        # Check if user already has a profile with different slack_id
        try:
            profile = UserProfile.objects.get(user=user)
            profile.slack_user_id = slack_id
            profile.save()
        except UserProfile.DoesNotExist:
            # No profile exists - this is handled later when joining a team
            pass

    # Log the user in
    login(request, user)

    return redirect("core:dashboard")

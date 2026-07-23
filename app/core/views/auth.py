"""Authentication views for Slack OAuth login.

This module handles user authentication via Slack OpenID Connect.
"""

import logging
import secrets
from typing import Any, cast

import requests
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.db import models
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect

from .. import analytics
from ..models import UserProfile, Workspace

logger = logging.getLogger(__name__)

# Default timeout for external API requests (seconds)
SLACK_API_TIMEOUT = 30

# Session key for the Slack OAuth login state parameter (CSRF protection)
SLACK_AUTH_STATE_SESSION_KEY = "slack_auth_oauth_state"

# Session key for the Slack team name captured at login, used to prefill
# the workspace name during onboarding.
SLACK_TEAM_NAME_SESSION_KEY = "slack_team_name"

# Slack's OpenID Connect userInfo response namespaces its custom claims.
SLACK_TEAM_NAME_CLAIM = "https://slack.com/team_name"

# The captured team name prefills Workspace.name, so cap it to that
# field's length ("or 200" only pacifies max_length's Optional typing).
WORKSPACE_NAME_MAX_LENGTH: int = (
    cast(models.CharField, Workspace._meta.get_field("name")).max_length or 200
)


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
    # Generate a state parameter and store it in the session for CSRF
    # protection. It is validated on callback before the code is exchanged.
    state = secrets.token_urlsafe(32)
    request.session[SLACK_AUTH_STATE_SESSION_KEY] = state

    scopes = "openid,email,profile"
    auth_url = (
        f"https://slack.com/openid/connect/authorize"
        f"?client_id={settings.SLACK_CLIENT_ID}"
        f"&scope={scopes}"
        f"&redirect_uri={settings.SLACK_REDIRECT_URI}"
        f"&response_type=code"
        f"&state={state}"
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

    # Validate the state parameter (CSRF protection) BEFORE exchanging the
    # code. Read (do not pop) the stored state so a forged callback with a
    # wrong/missing state cannot clear the legitimate in-progress state and
    # DoS the real login flow. Only consume it after a successful match.
    state = request.GET.get("state")
    stored_state = request.session.get(SLACK_AUTH_STATE_SESSION_KEY)
    if not state or not stored_state or not secrets.compare_digest(state, stored_state):
        logger.warning("Slack auth OAuth state mismatch - possible CSRF attack")
        return HttpResponse("Invalid OAuth state", status=400)

    # State validated: consume it so it can't be replayed.
    request.session.pop(SLACK_AUTH_STATE_SESSION_KEY, None)

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

    # Reject login when Slack reports the email is not verified. Auto-creating
    # or linking an account on an unverified email would allow account takeover.
    if not user_info.get("email_verified", False):
        # Log a non-PII identifier (Slack sub) instead of the email address.
        logger.warning(f"Slack login rejected: email not verified (sub={slack_id})")
        return HttpResponse("Email address is not verified", status=400)

    user, created = _resolve_user(slack_id, email, name)

    # Log the user in
    analytics.set_login_method(request, "slack")
    login(request, user)
    if created:
        analytics.track_event(request, "sign_up", {"method": "slack"})

    # Remember the Slack team name so onboarding can prefill the workspace
    # name instead of asking the user to retype it. Set after login():
    # logging in as a different user flushes the session. Normalize
    # defensively - the claim is external input destined for
    # Workspace.name.
    team_name = user_info.get(SLACK_TEAM_NAME_CLAIM)
    if isinstance(team_name, str):
        team_name = team_name.strip()[:WORKSPACE_NAME_MAX_LENGTH]
        if team_name:
            request.session[SLACK_TEAM_NAME_SESSION_KEY] = team_name

    return redirect("core:dashboard")


def _resolve_user(slack_id: str, email: str, name: str) -> tuple[User, bool]:
    """Find or create the user and reconcile their Slack profile link.

    Args:
        slack_id: The Slack user identifier (``sub`` claim).
        email: The user's verified email address.
        name: The user's display name.

    Returns:
        Tuple of (resolved Django user, whether the user was created).
    """
    # Find or create user
    user, created = User.objects.get_or_create(
        email=email, defaults={"username": email, "first_name": name}
    )

    if created:
        # Log the non-PII Slack sub instead of the email address.
        logger.info(f"Created new user (sub={slack_id})")

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

    return user, created

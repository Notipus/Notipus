"""Base classes and utilities for integration views.

This module provides the foundation for the plugin-based integration architecture,
including shared utilities, protocols, and helper functions.
"""

import logging
from typing import Protocol

from django.contrib import messages
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect

from ...models import UserProfile, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)

# Default timeout for external API requests (seconds)
DEFAULT_API_TIMEOUT = 30


class IntegrationProtocol(Protocol):
    """Protocol defining the interface for integration handlers.

    Integration handlers can implement any subset of these methods
    depending on their connection flow (OAuth vs manual configuration).
    """

    integration_type: str
    display_name: str

    def start_connect(self, request: HttpRequest) -> HttpResponseRedirect:
        """Start the integration connection flow.

        Args:
            request: The HTTP request object.

        Returns:
            Redirect to OAuth provider or setup page.
        """
        ...

    def handle_callback(self, request: HttpRequest) -> HttpResponseRedirect:
        """Handle OAuth callback from the provider.

        Args:
            request: The HTTP request object.

        Returns:
            Redirect to integrations page with success/error message.
        """
        ...

    def disconnect(self, request: HttpRequest) -> HttpResponseRedirect:
        """Disconnect the integration.

        Args:
            request: The HTTP request object.

        Returns:
            Redirect to integrations page.
        """
        ...


def get_user_workspace(request: HttpRequest) -> Workspace | None:
    """Get the workspace for the authenticated user.

    Args:
        request: The HTTP request object.

    Returns:
        The user's workspace or None if not found.
    """
    if not request.user.is_authenticated:
        return None

    # Try WorkspaceMember first
    member = WorkspaceMember.objects.filter(user=request.user, is_active=True).first()
    if member:
        workspace: Workspace = member.workspace
        return workspace

    # Fall back to UserProfile for backward compatibility
    try:
        user_profile = UserProfile.objects.get(user=request.user)
        workspace = user_profile.workspace
        return workspace
    except UserProfile.DoesNotExist:
        return None


def require_workspace(
    request: HttpRequest,
) -> tuple[Workspace | None, HttpResponseRedirect | None]:
    """Require the user to have a workspace.

    Args:
        request: The HTTP request object.

    Returns:
        Tuple of (workspace, redirect_response).
        If workspace exists, redirect is None.
        If workspace doesn't exist, workspace is None and redirect is set.
    """
    workspace = get_user_workspace(request)
    if workspace is None:
        if not request.user.is_authenticated:
            return None, redirect("account_login")
        return None, redirect("core:create_workspace")
    return workspace, None


def require_admin_role(
    request: HttpRequest,
) -> tuple[Workspace | None, HttpResponseRedirect | None]:
    """Require the user to be an admin or owner of a workspace.

    Use this for views that modify integrations, settings, or billing.

    Args:
        request: The HTTP request object.

    Returns:
        Tuple of (workspace, redirect_response).
        If user is admin/owner, redirect is None.
        If not, workspace is None and redirect is set with error message.
    """
    if not request.user.is_authenticated:
        return None, redirect("account_login")

    # Get workspace membership
    member = WorkspaceMember.objects.filter(user=request.user, is_active=True).first()
    if member is None:
        # Fall back to UserProfile for backward compatibility
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            # UserProfile users are treated as owners for backward compatibility
            return user_profile.workspace, None
        except UserProfile.DoesNotExist:
            return None, redirect("core:create_workspace")

    # Check role
    if member.role not in ("owner", "admin"):
        messages.error(request, "You don't have permission to perform this action.")
        return None, redirect("core:dashboard")

    return member.workspace, None


def handle_disconnect_error(
    request: HttpRequest, integration_name: str
) -> HttpResponseRedirect:
    """Handle common disconnect error scenarios.

    Args:
        request: The HTTP request object.
        integration_name: Display name of the integration.

    Returns:
        Redirect to integrations page with error message.
    """
    messages.warning(request, f"No active {integration_name} integration found")
    return redirect("core:integrations")


def require_post_method(
    request: HttpRequest,
) -> HttpResponseRedirect | None:
    """Ensure request is a POST request.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect with error if not POST, None otherwise.
    """
    if request.method != "POST":
        messages.error(request, "Invalid request method")
        return redirect("core:integrations")
    return None

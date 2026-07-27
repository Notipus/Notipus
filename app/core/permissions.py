"""Permission decorators and checks for workspace access control.

This module provides role-based permission enforcement for workspace operations.

Roles:
- owner: Full access, can manage all aspects of the workspace
- admin: Same as owner, EXCEPT cannot remove owners
- user: View-only access to dashboard and integrations
"""

from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, cast

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

if TYPE_CHECKING:
    from core.models import Workspace, WorkspaceMember


def get_workspace_member(user) -> "WorkspaceMember | None":
    """Get user's active workspace membership.

    Args:
        user: The Django user object.

    Returns:
        The user's active WorkspaceMember instance, or None if not found.
    """
    from core.models import WorkspaceMember

    if not user.is_authenticated:
        return None

    return WorkspaceMember.objects.filter(user=user, is_active=True).first()


def get_workspace_for_user(user) -> "Workspace | None":
    """Get the workspace for a user.

    Args:
        user: The Django user object.

    Returns:
        The user's active Workspace, or None if not found.
    """
    member = get_workspace_member(user)
    return member.workspace if member else None


def require_workspace(view_func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to require an active workspace membership.

    Redirects to workspace creation if user has no workspace.

    Args:
        view_func: The view function to wrap.

    Returns:
        Wrapped view function that checks for workspace membership.
    """

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        member = get_workspace_member(request.user)
        if not member:
            messages.info(request, "Please create or join a workspace first.")
            return redirect("core:create_workspace")

        # Add member and workspace to request for convenience
        request.workspace_member = member  # type: ignore[attr-defined]
        request.workspace = member.workspace  # type: ignore[attr-defined]
        return cast(HttpResponse, view_func(request, *args, **kwargs))

    return wrapper


def require_role(*allowed_roles: str) -> Callable[..., Any]:
    """Decorator factory to require specific roles for a view.

    Args:
        *allowed_roles: Role names that are allowed (e.g., "owner", "admin").

    Returns:
        Decorator that enforces role requirements.

    Example:
        @require_role("owner", "admin")
        def manage_integrations(request):
            ...
    """

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view_func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            member = get_workspace_member(request.user)
            if not member:
                messages.error(request, "You must be a member of a workspace.")
                return redirect("core:create_workspace")

            if member.role not in allowed_roles:
                messages.error(
                    request, "You don't have permission to perform this action."
                )
                return redirect("core:dashboard")

            # Add member and workspace to request for convenience
            request.workspace_member = member  # type: ignore[attr-defined]
            request.workspace = member.workspace  # type: ignore[attr-defined]
            return cast(HttpResponse, view_func(request, *args, **kwargs))

        return wrapper

    return decorator


def admin_required(view_func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator shortcut for views requiring owner or admin role.

    This is the most common permission check - owners and admins can:
    - Manage integrations
    - Manage team members (with restrictions)
    - Access billing
    - Modify workspace settings

    Args:
        view_func: The view function to wrap.

    Returns:
        Wrapped view function that checks for owner or admin role.
    """
    return cast(Callable[..., Any], require_role("owner", "admin")(view_func))


def can_remove_member(actor: "WorkspaceMember", target: "WorkspaceMember") -> bool:
    """Check if actor can remove target member from workspace.

    Rules:
    - Owners can remove anyone
    - Admins can remove admins and users, but NOT owners
    - Users cannot remove anyone

    Args:
        actor: The member attempting the removal.
        target: The member to be removed.

    Returns:
        True if the removal is allowed, False otherwise.
    """
    # Users cannot remove anyone
    if actor.role == "user":
        return False

    # Only owners can remove owners
    if target.role == "owner" and actor.role != "owner":
        return False

    # Owners and admins can remove admins and users
    return actor.role in ("owner", "admin")


def can_change_role(
    actor: "WorkspaceMember", target: "WorkspaceMember", new_role: str
) -> bool:
    """Check if actor can change target's role to new_role.

    Rules:
    - Owners can change anyone's role to anything
    - Admins can change user ↔ admin, but cannot:
      - Demote an owner
      - Promote anyone to owner
    - Users cannot change roles

    Args:
        actor: The member attempting the role change.
        target: The member whose role would change.
        new_role: The proposed new role.

    Returns:
        True if the role change is allowed, False otherwise.
    """
    # Users cannot change roles
    if actor.role == "user":
        return False

    # Owners can do anything
    if actor.role == "owner":
        return True

    # Admins cannot touch owners
    if target.role == "owner":
        return False

    # Admins cannot promote to owner
    if new_role == "owner":
        return False

    # Admins can change between admin and user
    return new_role in ("admin", "user")


def can_invite_user(workspace: "Workspace") -> tuple[bool, str]:
    """Check if the workspace can invite another user based on plan limits.

    Args:
        workspace: The workspace to check.

    Returns:
        Tuple of (allowed, message). If not allowed, message explains why.
    """
    from core.models import Plan, WorkspaceMember

    current_count = WorkspaceMember.objects.filter(
        workspace=workspace, is_active=True
    ).count()

    # Get the plan's max_users
    try:
        plan = Plan.objects.get(name=workspace.subscription_plan, is_active=True)
        max_users = plan.max_users
    except Plan.DoesNotExist:
        # Default to 1 if plan not found (trial or misconfigured)
        max_users = 1

    if current_count >= max_users:
        user_word = "users" if max_users > 1 else "user"
        return (
            False,
            f"Your {workspace.subscription_plan} plan allows up to {max_users} "
            f"{user_word}. Please upgrade to add more team members.",
        )

    return True, ""


def get_remaining_seats(workspace: "Workspace") -> int:
    """Get the number of remaining user seats for a workspace.

    Args:
        workspace: The workspace to check.

    Returns:
        Number of remaining seats (0 if at limit or over).
    """
    from core.models import Plan, WorkspaceMember

    current_count = WorkspaceMember.objects.filter(
        workspace=workspace, is_active=True
    ).count()

    try:
        plan = Plan.objects.get(name=workspace.subscription_plan, is_active=True)
        max_users = plan.max_users
    except Plan.DoesNotExist:
        max_users = 1

    return int(max(0, max_users - current_count))


# Billing tier hierarchy for feature gating
# Higher number = higher tier
TIER_ORDER: dict[str, int] = {
    "free": 0,
    "trial": 0,
    "basic": 1,
    "pro": 2,
    "enterprise": 3,
}


def has_plan_or_higher(workspace: "Workspace", min_plan: str) -> bool:
    """Check if a workspace has at least the specified plan tier.

    Used for feature gating based on subscription plan.

    Args:
        workspace: The workspace to check.
        min_plan: The minimum required plan (e.g., "pro").

    Returns:
        True if workspace plan is at or above the minimum tier.

    Example:
        if has_plan_or_higher(workspace, "pro"):
            # Enable Pro-only feature
    """
    workspace_tier = TIER_ORDER.get(workspace.subscription_plan, 0)
    required_tier = TIER_ORDER.get(min_plan, 0)
    return workspace_tier >= required_tier


def get_plan_tier(workspace: "Workspace") -> int:
    """Get the numeric tier level for a workspace's plan.

    Args:
        workspace: The workspace to check.

    Returns:
        Numeric tier level (0 = free/trial, 1 = basic, 2 = pro, 3 = enterprise).
    """
    return TIER_ORDER.get(workspace.subscription_plan, 0)

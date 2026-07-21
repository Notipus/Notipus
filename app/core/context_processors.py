"""Template context processors for core app."""

from typing import Any

from django.http import HttpRequest

from .models import WorkspaceMember


def workspace_role(request: HttpRequest) -> dict[str, Any]:
    """Expose the current user's workspace role to all templates.

    Lets shared chrome (the nav in base.html.j2) hide links to
    admin-gated views like the members page for regular members.

    Args:
        request: The current HTTP request.

    Returns:
        Dict with ``nav_workspace_role`` ("owner"/"admin"/"user" or None).
    """
    # Error handlers can render before AuthenticationMiddleware ran,
    # so request.user may be absent entirely.
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    member = (
        WorkspaceMember.objects.filter(user=request.user, is_active=True)
        .only("role")
        .first()
    )
    return {"nav_workspace_role": member.role if member else None}

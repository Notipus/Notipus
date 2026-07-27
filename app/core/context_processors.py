"""Template context processors for core app."""

from typing import Any

from django.conf import settings
from django.http import HttpRequest

from .models import WorkspaceMember


def analytics(request: HttpRequest) -> dict[str, Any]:
    """Expose the client-side GA4 measurement id to templates.

    Populated only when GA4_CLIENT_SIDE is enabled and a measurement id
    is configured, so the gtag.js snippet in base.html.j2 renders solely
    when client-side tracking is turned on. The server-side Measurement
    Protocol path (core.analytics) is independent of this.

    Args:
        request: The current HTTP request.

    Returns:
        Dict with ``ga4_measurement_id`` when client-side GA4 is on,
        otherwise empty.
    """
    if settings.GA4_CLIENT_SIDE and settings.GA4_MEASUREMENT_ID:
        return {"ga4_measurement_id": settings.GA4_MEASUREMENT_ID}
    return {}


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

    # Permission decorators already attach the resolved membership on
    # most workspace views — reuse it instead of querying again.
    member = getattr(request, "workspace_member", None)
    if member is not None:
        return {"nav_workspace_role": member.role}

    memberships = WorkspaceMember.objects.filter(user=user, is_active=True)
    workspace = getattr(request, "workspace", None)
    if workspace is not None:
        memberships = memberships.filter(workspace=workspace)
    member = memberships.only("role").first()
    return {"nav_workspace_role": member.role if member else None}

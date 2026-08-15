"""Who on a workspace should receive an operational email.

Owners and admins, because every message that uses this asks someone to
act on the workspace itself - connect a source, upgrade a plan, deal with
a limit - and only those roles can.
"""

from typing import Any


def admin_emails(workspace: Any) -> list[str]:
    """Return email addresses of the workspace's owners and admins.

    Args:
        workspace: Workspace to collect recipients for.

    Returns:
        Sorted, de-duplicated list of non-empty email addresses.
    """
    from core.models import WorkspaceMember

    members = WorkspaceMember.objects.filter(
        workspace=workspace, role__in=("owner", "admin"), is_active=True
    ).select_related("user")
    return sorted({member.user.email for member in members if member.user.email})

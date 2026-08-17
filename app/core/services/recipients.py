"""Who on a workspace should receive an operational email.

Owners and admins, because every message that uses this asks someone to
act on the workspace itself - connect a source, upgrade a plan, deal with
a limit - and only those roles can.
"""

from typing import Any


def admin_emails(workspace: Any) -> list[str]:
    """Return email addresses of the workspace's owners and admins.

    Falls back to the ``UserProfile`` link when a workspace predates
    ``WorkspaceMember`` and so has no membership rows. Returning nothing
    means the message is dropped without trace, which is merely annoying
    for a usage alert and unacceptable for a Shopify customer data
    request - that one carries a legal deadline and arrives once.

    Args:
        workspace: Workspace to collect recipients for.

    Returns:
        Sorted, de-duplicated list of non-empty email addresses.
    """
    from core.models import UserProfile, WorkspaceMember

    members = WorkspaceMember.objects.filter(
        workspace=workspace, role__in=("owner", "admin"), is_active=True
    ).select_related("user")
    emails = {member.user.email for member in members if member.user.email}

    if not emails:
        profiles = UserProfile.objects.filter(workspace=workspace).select_related(
            "user"
        )
        emails = {profile.user.email for profile in profiles if profile.user.email}

    return sorted(emails)

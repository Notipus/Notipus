"""Deduplicate pending workspace invitations.

WorkspaceInvitation had no uniqueness on (workspace, email) for pending
invitations, so repeated invites to the same address piled up duplicate
pending rows. For each (workspace, lowercased email) with multiple
pending invitations (``accepted_at IS NULL``, including expired ones),
keep the most recently created row and delete the rest.

This must run before 0029, which adds the conditional unique constraint;
applying the constraint with duplicates still present would fail.
"""

from django.db import migrations


def dedupe_pending_invitations(apps, schema_editor):
    """Keep only the newest pending invitation per (workspace, email)."""
    invitation_model = apps.get_model("core", "WorkspaceInvitation")

    seen: set[tuple[int, str]] = set()
    duplicate_ids: list[int] = []
    pending = (
        invitation_model.objects.filter(accepted_at__isnull=True)
        .order_by("-created_at", "-id")
        .only("id", "workspace_id", "email")
    )
    for invitation in pending.iterator():
        key = (invitation.workspace_id, invitation.email.lower())
        if key in seen:
            duplicate_ids.append(invitation.id)
        else:
            seen.add(key)

    if duplicate_ids:
        invitation_model.objects.filter(id__in=duplicate_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_scope_person_to_workspace"),
    ]

    operations = [
        migrations.RunPython(
            dedupe_pending_invitations,
            migrations.RunPython.noop,
        ),
    ]

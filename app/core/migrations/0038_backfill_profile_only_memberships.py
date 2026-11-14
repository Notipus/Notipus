"""Give legacy profile-only users a real WorkspaceMember row.

Accounts created before ``WorkspaceMember`` existed are linked to their
workspace only through ``UserProfile.workspace``. Authorization used to
fall back to that link whenever a user had no *active* membership and
treated them as owner, which also applied to a removed member (whose row
is merely deactivated) - so removing a workspace creator never took
their access away.

The fallback is gone. To keep genuine legacy users working, this creates
the membership they were implicitly granted: only for users with no
membership row at all (a deactivated row means they were removed and
must stay removed). They become owner, as the fallback treated them,
unless the workspace already has an active owner, in which case they get
admin - the same access to integrations and settings without a second
owner appearing next to a real one.
"""

from django.db import migrations


def backfill_memberships(apps, schema_editor):
    """Create memberships for users linked only through a UserProfile.

    Args:
        apps: The historical app registry.
        schema_editor: The schema editor (unused).
    """
    UserProfile = apps.get_model("core", "UserProfile")
    WorkspaceMember = apps.get_model("core", "WorkspaceMember")

    users_with_rows = set(WorkspaceMember.objects.values_list("user_id", flat=True))
    profiles = UserProfile.objects.exclude(user_id__in=users_with_rows).order_by("id")
    for profile in profiles:
        has_owner = WorkspaceMember.objects.filter(
            workspace_id=profile.workspace_id, role="owner", is_active=True
        ).exists()
        WorkspaceMember.objects.create(
            user_id=profile.user_id,
            workspace_id=profile.workspace_id,
            role="admin" if has_owner else "owner",
            is_active=True,
        )


def noop_reverse(apps, schema_editor):
    """Leave the created memberships in place on reverse.

    They cannot be told apart from memberships created normally, and
    deleting them would lock the users out of their workspaces.

    Args:
        apps: The historical app registry.
        schema_editor: The schema editor (unused).
    """


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_webauthn_challenge_signup_identity"),
    ]

    operations = [
        migrations.RunPython(backfill_memberships, noop_reverse),
    ]

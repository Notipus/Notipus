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

It also records Slack-verified email addresses for accounts created by
the legacy Slack sign-in, which never wrote an allauth ``EmailAddress``.
Slack sign-in now only matches accounts whose address is verified, so
without this those users would be refused. Slack verified these
addresses; only accounts linked to a Slack user (``slack_user_id``) with
no ``EmailAddress`` row for their email get one.
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


def verify_slack_linked_emails(apps, schema_editor):
    """Mark the email of legacy Slack-linked accounts as verified.

    Args:
        apps: The historical app registry.
        schema_editor: The schema editor (unused).
    """
    UserProfile = apps.get_model("core", "UserProfile")
    EmailAddress = apps.get_model("account", "EmailAddress")

    profiles = (
        UserProfile.objects.exclude(slack_user_id__isnull=True)
        .exclude(slack_user_id="")
        .select_related("user")
        .order_by("id")
    )
    for profile in profiles:
        user = profile.user
        email = (user.email or "").strip()
        if not email:
            continue
        if EmailAddress.objects.filter(user_id=user.pk, email__iexact=email).exists():
            continue
        # Another account already claims this address: leave it alone.
        if EmailAddress.objects.filter(email__iexact=email).exists():
            continue
        has_primary = EmailAddress.objects.filter(user_id=user.pk, primary=True).exists()
        EmailAddress.objects.create(
            user_id=user.pk, email=email, verified=True, primary=not has_primary
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
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [
        migrations.RunPython(backfill_memberships, noop_reverse),
        migrations.RunPython(verify_slack_linked_emails, noop_reverse),
    ]

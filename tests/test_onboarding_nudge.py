"""Tests for the half-finished-setup reminder.

Covers who gets selected, that the reminder is genuinely one-shot, and
that a mail failure leaves the workspace eligible for the next run
instead of silently consuming its only nudge.
"""

from io import StringIO
from unittest.mock import patch

import pytest
from core.models import Integration, Workspace, WorkspaceMember
from core.services.onboarding_nudge import (
    find_stalled_workspaces,
    send_nudge,
    send_pending_nudges,
)
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.utils import timezone


def _workspace(name: str, *, age_hours: int = 48) -> Workspace:
    """Create a workspace backdated by ``age_hours`` with an owner.

    Args:
        name: Workspace name, also used to derive the owner's address.
        age_hours: How long ago the workspace was created.

    Returns:
        The persisted workspace.
    """
    workspace = Workspace.objects.create(name=name)
    # created_at is auto_now_add, so backdate with an explicit update.
    Workspace.objects.filter(pk=workspace.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=age_hours)
    )
    workspace.refresh_from_db()

    owner = User.objects.create_user(
        username=f"owner-{name}", email=f"owner-{name}@example.com"
    )
    WorkspaceMember.objects.create(
        workspace=workspace, user=owner, role="owner", is_active=True
    )
    return workspace


def _integration(workspace: Workspace, integration_type: str) -> Integration:
    """Attach an active integration of ``integration_type`` to ``workspace``.

    Args:
        workspace: Workspace to attach to.
        integration_type: One of Integration.INTEGRATION_TYPES.

    Returns:
        The persisted integration.
    """
    return Integration.objects.create(
        workspace=workspace, integration_type=integration_type, is_active=True
    )


@pytest.mark.django_db
class TestSelection:
    """Which workspaces qualify for a nudge."""

    def test_destination_without_source_qualifies(self) -> None:
        """The exact stall we are targeting."""
        workspace = _workspace("stalled")
        _integration(workspace, "slack_notifications")

        assert list(find_stalled_workspaces()) == [workspace]

    def test_source_connected_does_not_qualify(self) -> None:
        """A workspace receiving events needs no reminder."""
        workspace = _workspace("working")
        _integration(workspace, "slack_notifications")
        _integration(workspace, "stripe_customer")

        assert list(find_stalled_workspaces()) == []

    def test_no_destination_does_not_qualify(self) -> None:
        """Nothing connected at all is a different problem to this one."""
        _workspace("empty")

        assert list(find_stalled_workspaces()) == []

    def test_too_young_is_left_alone(self) -> None:
        """Someone still mid-setup must not be interrupted."""
        workspace = _workspace("fresh", age_hours=2)
        _integration(workspace, "slack_notifications")

        assert list(find_stalled_workspaces()) == []

    def test_already_nudged_is_excluded(self) -> None:
        """The stamp is what makes a daily job safe to run daily."""
        workspace = _workspace("done")
        _integration(workspace, "slack_notifications")
        workspace.onboarding_nudge_sent_at = timezone.now()
        workspace.save(update_fields=["onboarding_nudge_sent_at"])

        assert list(find_stalled_workspaces()) == []

    def test_inactive_destination_does_not_qualify(self) -> None:
        """A disconnected channel is not a destination."""
        workspace = _workspace("disconnected")
        integration = _integration(workspace, "slack_notifications")
        integration.is_active = False
        integration.save(update_fields=["is_active"])

        assert list(find_stalled_workspaces()) == []

    @pytest.mark.parametrize(
        "destination",
        ["slack_notifications", "telegram_notifications", "teams_notifications"],
    )
    def test_every_destination_channel_counts(self, destination: str) -> None:
        """The stall is not Slack-specific, and neither is the query."""
        workspace = _workspace(f"ws-{destination}")
        _integration(workspace, destination)

        assert list(find_stalled_workspaces()) == [workspace]


@pytest.mark.django_db
class TestSending:
    """Delivery, stamping, and one-shot behaviour."""

    def test_emails_the_owner_and_stamps(self) -> None:
        """A successful send marks the workspace so it never repeats."""
        workspace = _workspace("stalled")
        _integration(workspace, "slack_notifications")

        assert send_nudge(workspace) is True

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["owner-stalled@example.com"]
        workspace.refresh_from_db()
        assert workspace.onboarding_nudge_sent_at is not None

    def test_second_run_sends_nothing(self) -> None:
        """Running the job twice does not mail the same workspace twice."""
        workspace = _workspace("stalled")
        _integration(workspace, "slack_notifications")

        assert send_pending_nudges() == 1
        assert send_pending_nudges() == 0
        assert len(mail.outbox) == 1

    def test_failed_send_leaves_workspace_eligible(self) -> None:
        """A mail outage must not consume the workspace's only nudge."""
        workspace = _workspace("stalled")
        _integration(workspace, "slack_notifications")

        with patch("core.services.onboarding_nudge.send_email", return_value=False):
            assert send_nudge(workspace) is False

        workspace.refresh_from_db()
        assert workspace.onboarding_nudge_sent_at is None
        assert list(find_stalled_workspaces()) == [workspace]

    def test_workspace_without_admins_is_skipped(self) -> None:
        """Nobody to write to, so nothing is sent or stamped."""
        workspace = Workspace.objects.create(name="ownerless")
        Workspace.objects.filter(pk=workspace.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=48)
        )
        workspace.refresh_from_db()
        _integration(workspace, "slack_notifications")

        assert send_nudge(workspace) is False
        assert mail.outbox == []
        workspace.refresh_from_db()
        assert workspace.onboarding_nudge_sent_at is None

    def test_copy_names_no_single_channel(self) -> None:
        """The reminder must read the same for Telegram and Teams users."""
        workspace = _workspace("stalled")
        _integration(workspace, "telegram_notifications")

        send_nudge(workspace)

        message = mail.outbox[0]
        for part in (message.body, message.alternatives[0][0]):
            assert "Slack" not in part
            assert "your channel" in part or "notification channel" in part


@pytest.mark.django_db
class TestManagementCommand:
    """The scheduled entry point."""

    def test_dry_run_sends_nothing(self) -> None:
        """--dry-run reports the target list without mailing anyone."""
        workspace = _workspace("stalled")
        _integration(workspace, "slack_notifications")

        out = StringIO()
        call_command("send_onboarding_nudges", "--dry-run", stdout=out)

        assert mail.outbox == []
        assert "owner-stalled@example.com" in out.getvalue()
        assert "1 would be emailed" in out.getvalue()
        workspace.refresh_from_db()
        assert workspace.onboarding_nudge_sent_at is None

    def test_limit_caps_the_send(self) -> None:
        """--limit allows a cautious first run against a backlog."""
        for name in ("a", "b", "c"):
            workspace = _workspace(name)
            _integration(workspace, "slack_notifications")

        call_command("send_onboarding_nudges", "--limit", "2", stdout=StringIO())

        assert len(mail.outbox) == 2

    def test_sends_and_reports(self) -> None:
        """The default invocation mails and reports the count."""
        workspace = _workspace("stalled")
        _integration(workspace, "slack_notifications")

        out = StringIO()
        call_command("send_onboarding_nudges", stdout=out)

        assert len(mail.outbox) == 1
        assert "Nudged 1 workspace" in out.getvalue()

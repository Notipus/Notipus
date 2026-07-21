"""Tests for pending workspace-invitation uniqueness and deduplication.

Covers:
- The conditional unique constraint on pending (workspace, email) invitations
- invite_member handling duplicates gracefully (no 500s)
- Expired pending invitations being replaced on re-invite
- Re-inviting after acceptance being allowed
- The 0028 data migration deduplicating existing pending invitations
"""

from datetime import timedelta
from importlib import import_module
from typing import Callable
from unittest.mock import patch

import pytest
from core.models import Workspace, WorkspaceInvitation
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone


@pytest.mark.django_db
class TestPendingInvitationConstraint:
    """Tests for the uniq_pending_invite_workspace_email DB constraint."""

    def test_duplicate_pending_invitation_rejected(
        self, workspace: Workspace, owner_user: User
    ) -> None:
        """A second pending invitation for the same email is rejected."""
        WorkspaceInvitation.objects.create(
            workspace=workspace, email="dup@example.com", invited_by=owner_user
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            WorkspaceInvitation.objects.create(
                workspace=workspace, email="dup@example.com", invited_by=owner_user
            )

    def test_same_email_allowed_in_different_workspaces(
        self, workspace: Workspace, owner_user: User
    ) -> None:
        """Pending invitations to the same email in two workspaces coexist."""
        other = Workspace.objects.create(name="Other Workspace")
        WorkspaceInvitation.objects.create(
            workspace=workspace, email="dup@example.com", invited_by=owner_user
        )
        WorkspaceInvitation.objects.create(
            workspace=other, email="dup@example.com", invited_by=owner_user
        )

        assert WorkspaceInvitation.objects.filter(email="dup@example.com").count() == 2

    def test_reinvite_allowed_after_acceptance(
        self, workspace: Workspace, owner_user: User
    ) -> None:
        """Once an invitation is accepted, a new pending one may be created."""
        accepted = WorkspaceInvitation.objects.create(
            workspace=workspace, email="again@example.com", invited_by=owner_user
        )
        accepted.accepted_at = timezone.now()
        accepted.save(update_fields=["accepted_at"])

        WorkspaceInvitation.objects.create(
            workspace=workspace, email="again@example.com", invited_by=owner_user
        )

        assert (
            WorkspaceInvitation.objects.filter(
                workspace=workspace, email="again@example.com"
            ).count()
            == 2
        )


@pytest.mark.django_db
class TestInviteMemberViewDedup:
    """Tests for graceful duplicate handling in the invite_member view."""

    def test_duplicate_invite_shows_warning(
        self,
        authenticated_owner_client: Client,
        workspace: Workspace,
        pending_invitation: WorkspaceInvitation,
    ) -> None:
        """Inviting an already-invited email warns instead of duplicating."""
        response = authenticated_owner_client.post(
            reverse("core:invite_member"),
            {"email": pending_invitation.email, "role": "user"},
            follow=True,
        )

        assert response.status_code == 200
        assert (
            WorkspaceInvitation.objects.filter(
                workspace=workspace, email=pending_invitation.email
            ).count()
            == 1
        )
        messages = [str(m) for m in response.context["messages"]]
        assert any("already been sent" in m for m in messages)

    def test_expired_pending_invite_is_replaced(
        self,
        authenticated_owner_client: Client,
        workspace: Workspace,
        owner_user: User,
    ) -> None:
        """An expired pending invitation is replaced by a fresh one."""
        expired = WorkspaceInvitation.objects.create(
            workspace=workspace,
            email="expired@example.com",
            invited_by=owner_user,
            expires_at=timezone.now() - timedelta(days=1),
        )

        response = authenticated_owner_client.post(
            reverse("core:invite_member"),
            {"email": "expired@example.com", "role": "user"},
        )

        assert response.status_code == 302
        invitations = WorkspaceInvitation.objects.filter(
            workspace=workspace, email="expired@example.com"
        )
        assert invitations.count() == 1
        fresh = invitations.get()
        assert fresh.id != expired.id
        assert not fresh.is_expired

    def test_race_duplicate_handled_gracefully(
        self,
        authenticated_owner_client: Client,
        workspace: Workspace,
        pending_invitation: WorkspaceInvitation,
    ) -> None:
        """A concurrent duplicate hitting the DB constraint yields a warning."""
        # Simulate a race: the pre-create existence check misses the
        # concurrently created invitation, so create() hits the constraint.
        with patch.object(
            WorkspaceInvitation.objects,
            "filter",
            return_value=WorkspaceInvitation.objects.none(),
        ):
            response = authenticated_owner_client.post(
                reverse("core:invite_member"),
                {"email": pending_invitation.email, "role": "user"},
                follow=True,
            )

        assert response.status_code == 200
        assert (
            WorkspaceInvitation.objects.filter(
                workspace=workspace, email=pending_invitation.email
            ).count()
            == 1
        )
        messages = [str(m) for m in response.context["messages"]]
        assert any("already been sent" in m for m in messages)


def _get_dedupe() -> Callable[..., None]:
    """Import the dedup function from the 0028 data migration."""
    module = import_module("core.migrations.0028_dedupe_pending_invitations")
    return module.dedupe_pending_invitations  # type: ignore[no-any-return]


@pytest.mark.django_db
class TestDedupePendingInvitationsMigration:
    """Tests for the 0028 data migration deduplicating pending invitations.

    Duplicates are created with case-variant emails because the DB
    constraint (case-sensitive) is already active in the test database,
    while the migration dedupes case-insensitively - mirroring legacy rows
    that predate email normalization.
    """

    def test_keeps_most_recent_pending_per_workspace_email(
        self, workspace: Workspace, owner_user: User
    ) -> None:
        """Only the newest pending invitation per (workspace, email) survives."""
        older = WorkspaceInvitation.objects.create(
            workspace=workspace, email="Dup@Example.com", invited_by=owner_user
        )
        newer = WorkspaceInvitation.objects.create(
            workspace=workspace, email="dup@example.com", invited_by=owner_user
        )
        # Make creation order unambiguous.
        WorkspaceInvitation.objects.filter(id=older.id).update(
            created_at=timezone.now() - timedelta(days=2)
        )

        _get_dedupe()(django_apps, None)

        remaining = WorkspaceInvitation.objects.filter(workspace=workspace)
        assert remaining.count() == 1
        assert remaining.get().id == newer.id

    def test_accepted_invitations_are_untouched(
        self, workspace: Workspace, owner_user: User
    ) -> None:
        """Accepted invitations are history and never deleted by the dedup."""
        accepted = WorkspaceInvitation.objects.create(
            workspace=workspace, email="Keep@Example.com", invited_by=owner_user
        )
        accepted.accepted_at = timezone.now()
        accepted.save(update_fields=["accepted_at"])
        pending = WorkspaceInvitation.objects.create(
            workspace=workspace, email="keep@example.com", invited_by=owner_user
        )

        _get_dedupe()(django_apps, None)

        ids = set(
            WorkspaceInvitation.objects.filter(workspace=workspace).values_list(
                "id", flat=True
            )
        )
        assert ids == {accepted.id, pending.id}

    def test_different_workspaces_not_cross_deduped(
        self, workspace: Workspace, owner_user: User
    ) -> None:
        """Pending invitations in different workspaces are independent."""
        other = Workspace.objects.create(name="Other Workspace")
        WorkspaceInvitation.objects.create(
            workspace=workspace, email="solo@example.com", invited_by=owner_user
        )
        WorkspaceInvitation.objects.create(
            workspace=other, email="solo@example.com", invited_by=owner_user
        )

        _get_dedupe()(django_apps, None)

        assert WorkspaceInvitation.objects.filter(email="solo@example.com").count() == 2

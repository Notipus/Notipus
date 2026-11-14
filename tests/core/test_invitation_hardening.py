"""Tests for invitation seat limits, POST-only joining and token-free logs.

- Pending invitations reserve seats, so a workspace cannot send more
  invitations than it has free seats, and acceptance re-checks the limit.
- Visiting an invitation link (GET) never joins; only the POST confirm does.
- The invitation token, a bearer credential, never reaches the logs.
"""

import logging
from datetime import timedelta

import pytest
from core.models import Plan, Workspace, WorkspaceInvitation, WorkspaceMember
from core.permissions import can_invite_user, get_remaining_seats
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone


def _invite(workspace: Workspace, inviter: User, email: str) -> WorkspaceInvitation:
    """Create an unexpired pending invitation.

    Args:
        workspace: Workspace the invitation is for.
        inviter: User sending it.
        email: Invitee address.

    Returns:
        The invitation.
    """
    return WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email,
        role="user",
        invited_by=inviter,
        expires_at=timezone.now() + timedelta(days=7),
    )


def _login(email: str) -> tuple[Client, User]:
    """Create and log in a user with ``email``.

    Args:
        email: The user's address.

    Returns:
        The logged-in client and the user.
    """
    user = User.objects.create_user(username=email, email=email)
    client = Client()
    client.force_login(user)
    return client, user


@pytest.mark.django_db
def test_pending_invitation_reserves_a_seat(
    starter_workspace: Workspace, starter_plan: Plan, owner_user: User
) -> None:
    """With 1 member and 1 pending invitation, a 2-seat plan is full."""
    WorkspaceMember.objects.create(
        user=owner_user, workspace=starter_workspace, role="owner"
    )
    _invite(starter_workspace, owner_user, "first@example.com")

    allowed, message = can_invite_user(starter_workspace)

    assert allowed is False
    assert "pending invitations" in message
    assert get_remaining_seats(starter_workspace) == 0


@pytest.mark.django_db
def test_expired_invitation_does_not_reserve_a_seat(
    starter_workspace: Workspace, starter_plan: Plan, owner_user: User
) -> None:
    """An expired invitation frees its seat."""
    WorkspaceMember.objects.create(
        user=owner_user, workspace=starter_workspace, role="owner"
    )
    invitation = _invite(starter_workspace, owner_user, "late@example.com")
    WorkspaceInvitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - timedelta(days=1)
    )

    assert can_invite_user(starter_workspace) == (True, "")


@pytest.mark.django_db
def test_acceptance_rechecks_seat_limit(
    starter_workspace: Workspace, starter_plan: Plan, owner_user: User
) -> None:
    """Accepting when the plan is already full does not add a member."""
    WorkspaceMember.objects.create(
        user=owner_user, workspace=starter_workspace, role="owner"
    )
    other = User.objects.create_user(username="other", email="other@example.com")
    WorkspaceMember.objects.create(user=other, workspace=starter_workspace)
    invitation = _invite(starter_workspace, owner_user, "late@example.com")
    client, invitee = _login("late@example.com")

    response = client.post(
        reverse("core:confirm_accept_invitation", args=[invitation.token])
    )

    assert response.status_code == 302
    assert not WorkspaceMember.objects.filter(user=invitee).exists()
    invitation.refresh_from_db()
    assert invitation.accepted_at is None


@pytest.mark.django_db
def test_get_does_not_join(pending_invitation: WorkspaceInvitation) -> None:
    """A signed-in invitee visiting the link sees a confirmation page only."""
    client, invitee = _login(pending_invitation.email)

    response = client.get(
        reverse("core:accept_invitation", args=[pending_invitation.token])
    )

    assert response.status_code == 200
    assert not WorkspaceMember.objects.filter(user=invitee).exists()
    confirm_url = reverse(
        "core:confirm_accept_invitation", args=[pending_invitation.token]
    )
    assert confirm_url in response.content.decode()


@pytest.mark.django_db
def test_post_confirm_joins(pending_invitation: WorkspaceInvitation) -> None:
    """The POST confirmation creates the membership."""
    client, invitee = _login(pending_invitation.email)

    client.post(
        reverse("core:confirm_accept_invitation", args=[pending_invitation.token])
    )

    assert WorkspaceMember.objects.filter(
        user=invitee, workspace=pending_invitation.workspace, is_active=True
    ).exists()


@pytest.mark.django_db
def test_invite_log_omits_token(
    workspace: Workspace,
    owner_user: User,
    owner_member: WorkspaceMember,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The invitation log line carries the id, never the token or URL."""
    Plan.objects.update_or_create(
        name=workspace.subscription_plan,
        defaults={"display_name": "P", "max_users": 10, "is_active": True},
    )
    client = Client()
    client.force_login(owner_user)

    with caplog.at_level(logging.INFO, logger="core.views.members"):
        client.post(
            reverse("core:invite_member"),
            {"email": "new@example.com", "role": "user"},
        )

    invitation = WorkspaceInvitation.objects.get(email="new@example.com")
    assert str(invitation.token) not in caplog.text
    assert f"Invitation {invitation.id} created" in caplog.text

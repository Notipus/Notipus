"""Tests for workspace permissions and member management.

This module tests:
- Permission functions (can_remove_member, can_change_role, can_invite_user)
- Permission decorators (@admin_required, @require_workspace)
- Member management views (list, invite, remove, change role)
"""

import pytest
from core.models import WorkspaceInvitation, WorkspaceMember
from core.permissions import (
    can_change_role,
    can_invite_user,
    can_remove_member,
    get_remaining_seats,
    get_workspace_for_user,
    get_workspace_member,
)
from django.urls import reverse

# =============================================================================
# Permission Function Tests
# =============================================================================


class TestGetWorkspaceMember:
    """Test get_workspace_member function."""

    def test_returns_active_member(self, owner_user, owner_member) -> None:
        """Test returns active member for user."""
        member = get_workspace_member(owner_user)
        assert member is not None
        assert member.id == owner_member.id

    def test_returns_none_for_inactive(self, owner_user, owner_member) -> None:
        """Test returns None for inactive member."""
        owner_member.is_active = False
        owner_member.save()
        member = get_workspace_member(owner_user)
        assert member is None

    def test_returns_none_for_no_membership(self, db) -> None:
        """Test returns None for user without membership."""
        from django.contrib.auth.models import User

        user = User.objects.create_user("nomember", "nomember@test.com", "pass")
        member = get_workspace_member(user)
        assert member is None


class TestGetWorkspaceForUser:
    """Test get_workspace_for_user function."""

    def test_returns_workspace(self, owner_user, owner_member, workspace) -> None:
        """Test returns workspace for user."""
        ws = get_workspace_for_user(owner_user)
        assert ws is not None
        assert ws.id == workspace.id


class TestCanRemoveMember:
    """Test can_remove_member permission function."""

    def test_owner_can_remove_owner(self, owner_member, second_owner_member) -> None:
        """Test owner can remove another owner."""
        assert can_remove_member(owner_member, second_owner_member) is True

    def test_owner_can_remove_admin(self, owner_member, admin_member) -> None:
        """Test owner can remove admin."""
        assert can_remove_member(owner_member, admin_member) is True

    def test_owner_can_remove_user(self, owner_member, user_member) -> None:
        """Test owner can remove user."""
        assert can_remove_member(owner_member, user_member) is True

    def test_admin_cannot_remove_owner(self, admin_member, owner_member) -> None:
        """Test admin cannot remove owner."""
        assert can_remove_member(admin_member, owner_member) is False

    def test_admin_can_remove_admin(self, admin_member, workspace, db) -> None:
        """Test admin can remove another admin."""
        from django.contrib.auth.models import User

        second_admin = WorkspaceMember.objects.create(
            user=User.objects.create_user("admin2", "admin2@test.com", "pass"),
            workspace=workspace,
            role="admin",
        )
        assert can_remove_member(admin_member, second_admin) is True

    def test_admin_can_remove_user(self, admin_member, user_member) -> None:
        """Test admin can remove user."""
        assert can_remove_member(admin_member, user_member) is True

    def test_user_cannot_remove_anyone(
        self, user_member, owner_member, admin_member
    ) -> None:
        """Test user cannot remove anyone."""
        assert can_remove_member(user_member, owner_member) is False
        assert can_remove_member(user_member, admin_member) is False


class TestCanChangeRole:
    """Test can_change_role permission function."""

    def test_owner_can_change_any_role(
        self, owner_member, admin_member, user_member
    ) -> None:
        """Test owner can change any member's role to any value."""
        assert can_change_role(owner_member, admin_member, "user") is True
        assert can_change_role(owner_member, admin_member, "owner") is True
        assert can_change_role(owner_member, user_member, "admin") is True
        assert can_change_role(owner_member, user_member, "owner") is True

    def test_admin_can_promote_user_to_admin(self, admin_member, user_member) -> None:
        """Test admin can promote user to admin."""
        assert can_change_role(admin_member, user_member, "admin") is True

    def test_admin_can_demote_admin_to_user(self, admin_member, workspace, db) -> None:
        """Test admin can demote another admin to user."""
        from django.contrib.auth.models import User

        second_admin = WorkspaceMember.objects.create(
            user=User.objects.create_user("admin2", "admin2@test.com", "pass"),
            workspace=workspace,
            role="admin",
        )
        assert can_change_role(admin_member, second_admin, "user") is True

    def test_admin_cannot_change_owner_role(self, admin_member, owner_member) -> None:
        """Test admin cannot change owner's role."""
        assert can_change_role(admin_member, owner_member, "admin") is False
        assert can_change_role(admin_member, owner_member, "user") is False

    def test_admin_cannot_promote_to_owner(self, admin_member, user_member) -> None:
        """Test admin cannot promote anyone to owner."""
        assert can_change_role(admin_member, user_member, "owner") is False

    def test_user_cannot_change_roles(
        self, user_member, admin_member, owner_member
    ) -> None:
        """Test user cannot change anyone's role."""
        assert can_change_role(user_member, admin_member, "user") is False
        assert can_change_role(user_member, owner_member, "user") is False


class TestCanInviteUser:
    """Test can_invite_user permission function."""

    def test_can_invite_when_under_limit(
        self, starter_workspace, starter_plan, db
    ) -> None:
        """Test can invite when under plan limit."""
        from django.contrib.auth.models import User

        WorkspaceMember.objects.create(
            user=User.objects.create_user("user1", "user1@test.com", "pass"),
            workspace=starter_workspace,
            role="owner",
        )
        can_invite, message = can_invite_user(starter_workspace)
        assert can_invite is True
        assert message == ""

    def test_cannot_invite_when_at_limit(
        self, starter_workspace, starter_plan, db
    ) -> None:
        """Test cannot invite when at plan limit."""
        from django.contrib.auth.models import User

        WorkspaceMember.objects.create(
            user=User.objects.create_user("user1", "user1@test.com", "pass"),
            workspace=starter_workspace,
            role="owner",
        )
        WorkspaceMember.objects.create(
            user=User.objects.create_user("user2", "user2@test.com", "pass"),
            workspace=starter_workspace,
            role="user",
        )
        can_invite, message = can_invite_user(starter_workspace)
        assert can_invite is False
        assert "basic plan allows up to 2 users" in message

    def test_inactive_members_not_counted(
        self, starter_workspace, starter_plan, db
    ) -> None:
        """Test inactive members are not counted toward limit."""
        from django.contrib.auth.models import User

        WorkspaceMember.objects.create(
            user=User.objects.create_user("user1", "user1@test.com", "pass"),
            workspace=starter_workspace,
            role="owner",
        )
        WorkspaceMember.objects.create(
            user=User.objects.create_user("user2", "user2@test.com", "pass"),
            workspace=starter_workspace,
            role="user",
            is_active=False,
        )
        can_invite, message = can_invite_user(starter_workspace)
        assert can_invite is True

    def test_get_remaining_seats(self, starter_workspace, starter_plan, db) -> None:
        """Test get_remaining_seats returns correct count."""
        from django.contrib.auth.models import User

        WorkspaceMember.objects.create(
            user=User.objects.create_user("user1", "user1@test.com", "pass"),
            workspace=starter_workspace,
            role="owner",
        )
        assert get_remaining_seats(starter_workspace) == 1


# =============================================================================
# Member Management View Tests
# =============================================================================


@pytest.mark.django_db
class TestMembersListView:
    """Test members list view."""

    def test_requires_login(self, client) -> None:
        """Test members list requires authentication."""
        response = client.get(reverse("core:members_list"))
        assert response.status_code == 302
        assert "login" in response.url

    def test_requires_admin_role(self, authenticated_user_client) -> None:
        """Test members list requires admin or owner role."""
        response = authenticated_user_client.get(reverse("core:members_list"))
        assert response.status_code == 302

    def test_accessible_by_owner(self, authenticated_owner_client) -> None:
        """Test members list is accessible by owner."""
        response = authenticated_owner_client.get(reverse("core:members_list"))
        assert response.status_code == 200
        assert b"Team Members" in response.content

    def test_accessible_by_admin(self, authenticated_admin_client) -> None:
        """Test members list is accessible by admin."""
        response = authenticated_admin_client.get(reverse("core:members_list"))
        assert response.status_code == 200


@pytest.mark.django_db
class TestInviteMemberView:
    """Test invite member view."""

    def test_creates_invitation(
        self, authenticated_owner_client, workspace, owner_user
    ) -> None:
        """Test inviting a new member creates invitation."""
        response = authenticated_owner_client.post(
            reverse("core:invite_member"),
            {"email": "newuser@test.com", "role": "user"},
        )
        assert response.status_code == 302

        invitation = WorkspaceInvitation.objects.filter(
            workspace=workspace, email="newuser@test.com"
        ).first()
        assert invitation is not None
        assert invitation.role == "user"
        assert invitation.invited_by == owner_user

    def test_admin_cannot_invite_owner(
        self, authenticated_admin_client, workspace
    ) -> None:
        """Test admin cannot invite someone as owner."""
        response = authenticated_admin_client.post(
            reverse("core:invite_member"),
            {"email": "newowner@test.com", "role": "owner"},
        )
        assert response.status_code == 302

        invitation = WorkspaceInvitation.objects.filter(
            workspace=workspace, email="newowner@test.com"
        ).first()
        assert invitation is None


@pytest.mark.django_db
class TestRemoveMemberView:
    """Test remove member view."""

    def test_owner_can_remove_admin(
        self, authenticated_owner_client, admin_member
    ) -> None:
        """Test owner can remove admin."""
        response = authenticated_owner_client.post(
            reverse("core:remove_member", args=[admin_member.id])
        )
        assert response.status_code == 302

        admin_member.refresh_from_db()
        assert admin_member.is_active is False

    def test_admin_cannot_remove_owner(
        self, authenticated_admin_client, owner_member
    ) -> None:
        """Test admin cannot remove owner."""
        response = authenticated_admin_client.post(
            reverse("core:remove_member", args=[owner_member.id])
        )
        assert response.status_code == 302

        owner_member.refresh_from_db()
        assert owner_member.is_active is True


@pytest.mark.django_db
class TestChangeRoleView:
    """Test change role view."""

    def test_owner_can_demote_admin(
        self, authenticated_owner_client, admin_member
    ) -> None:
        """Test owner can demote admin to user."""
        response = authenticated_owner_client.post(
            reverse("core:change_role", args=[admin_member.id]),
            {"role": "user"},
        )
        assert response.status_code == 302

        admin_member.refresh_from_db()
        assert admin_member.role == "user"

    def test_admin_cannot_promote_to_owner(
        self, authenticated_admin_client, user_member
    ) -> None:
        """Test admin cannot promote someone to owner."""
        response = authenticated_admin_client.post(
            reverse("core:change_role", args=[user_member.id]),
            {"role": "owner"},
        )
        assert response.status_code == 302

        user_member.refresh_from_db()
        assert user_member.role == "user"


# =============================================================================
# Invitation Acceptance Tests
# =============================================================================


@pytest.mark.django_db
class TestInvitationAcceptance:
    """Test invitation acceptance flow."""

    def test_accept_page_shows_workspace_info(
        self, client, pending_invitation, workspace
    ) -> None:
        """Test accept invitation page shows workspace info."""
        response = client.get(
            reverse("core:accept_invitation", args=[pending_invitation.token])
        )
        assert response.status_code == 200
        assert workspace.name.encode() in response.content

    def test_expired_invitation_redirects(self, client, expired_invitation) -> None:
        """Test accessing expired invitation shows error."""
        response = client.get(
            reverse("core:accept_invitation", args=[expired_invitation.token])
        )
        assert response.status_code == 302

    def test_acceptance_creates_membership(
        self, client, pending_invitation, invitee_user, workspace
    ) -> None:
        """Test accepting invitation creates workspace membership."""
        client.force_login(invitee_user)

        response = client.post(
            reverse("core:confirm_accept_invitation", args=[pending_invitation.token])
        )
        assert response.status_code == 302

        member = WorkspaceMember.objects.filter(
            user=invitee_user, workspace=workspace
        ).first()
        assert member is not None
        assert member.role == "admin"
        assert member.is_active is True

        pending_invitation.refresh_from_db()
        assert pending_invitation.accepted_at is not None

    def test_wrong_email_shows_error(
        self, client, pending_invitation, regular_user
    ) -> None:
        """Test accepting invitation with wrong email shows error."""
        client.force_login(regular_user)

        response = client.post(
            reverse("core:confirm_accept_invitation", args=[pending_invitation.token])
        )
        assert response.status_code == 302

        # Regular user already has membership from fixture, but not via invitation
        # The key test is that the invitation wasn't accepted
        pending_invitation.refresh_from_db()
        assert pending_invitation.accepted_at is None

    def test_cancel_invitation(
        self, authenticated_owner_client, pending_invitation
    ) -> None:
        """Test cancelling an invitation."""
        response = authenticated_owner_client.post(
            reverse("core:cancel_invitation", args=[pending_invitation.id])
        )
        assert response.status_code == 302

        exists = WorkspaceInvitation.objects.filter(id=pending_invitation.id).exists()
        assert exists is False

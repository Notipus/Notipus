"""Tests for build-before-plan onboarding: auto-provisioned workspaces.

New signups no longer pick a plan before entering the product. The
dashboard provisions a free workspace on first visit (named after the
Slack team captured at OAuth, else the username) and sends the user to
the integrations hub; plan decisions move to the upgrade page.
"""

import pytest
from core.constants import SLACK_TEAM_NAME_SESSION_KEY
from core.models import Workspace, WorkspaceMember
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse


@pytest.fixture
def user(db: None) -> User:
    """Create a user without any workspace."""
    return User.objects.create_user(
        username="newbie", email="newbie@example.com", password="x"
    )


@pytest.fixture
def logged_in_client(client: Client, user: User) -> Client:
    """Return a test client with the workspace-less user logged in."""
    client.force_login(user)
    return client


class TestDashboardAutoProvisioning:
    """First dashboard visit provisions a free workspace, no form."""

    def test_first_visit_provisions_free_workspace(
        self, logged_in_client: Client, user: User
    ) -> None:
        """A workspace-less user gets a free workspace and lands on
        the integrations hub instead of the plan-selection funnel."""
        response = logged_in_client.get(reverse("core:dashboard"))

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        workspace = Workspace.objects.get()
        assert workspace.subscription_plan == "free"
        assert workspace.subscription_status == "active"
        member = WorkspaceMember.objects.get(user=user)
        assert member.workspace == workspace
        assert member.role == "owner"

    def test_workspace_named_after_slack_team(self, logged_in_client: Client) -> None:
        """The Slack team name captured at OAuth names the workspace
        and is cleared from the session once used."""
        session = logged_in_client.session
        session[SLACK_TEAM_NAME_SESSION_KEY] = "Acme Inc"
        session.save()

        logged_in_client.get(reverse("core:dashboard"))

        assert Workspace.objects.get().name == "Acme Inc"
        assert SLACK_TEAM_NAME_SESSION_KEY not in logged_in_client.session

    def test_workspace_name_falls_back_to_username(
        self, logged_in_client: Client
    ) -> None:
        """Without a Slack team name, the username names the workspace."""
        logged_in_client.get(reverse("core:dashboard"))

        assert Workspace.objects.get().name == "newbie's Workspace"

    def test_second_visit_does_not_duplicate(self, logged_in_client: Client) -> None:
        """A user with a workspace renders the dashboard normally."""
        logged_in_client.get(reverse("core:dashboard"))
        response = logged_in_client.get(reverse("core:dashboard"))

        assert response.status_code == 200
        assert Workspace.objects.count() == 1

    def test_stale_selected_plan_session_key_is_cleared(
        self, logged_in_client: Client
    ) -> None:
        """A leftover selected_plan key cannot leak into provisioning:
        the workspace is free and the key is removed."""
        session = logged_in_client.session
        session["selected_plan"] = "pro"
        session.save()

        logged_in_client.get(reverse("core:dashboard"))

        assert Workspace.objects.get().subscription_plan == "free"
        assert "selected_plan" not in logged_in_client.session

    def test_racing_provision_adopts_existing_workspace(
        self, logged_in_client: Client, user: User
    ) -> None:
        """Provisioning behind an existing membership returns it.

        Simulates the loser of a concurrent first-visit race: the
        membership already exists by the time provisioning runs, so no
        duplicate workspace is created.
        """
        from core.views.dashboard import _provision_free_workspace
        from django.test import RequestFactory

        workspace = Workspace.objects.create(
            name="Winner", subscription_plan="free", subscription_status="active"
        )
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

        request = RequestFactory().get(reverse("core:dashboard"))
        request.user = user
        request.session = logged_in_client.session

        assert _provision_free_workspace(request) == workspace
        assert Workspace.objects.count() == 1


class TestSelectPlanRedirect:
    """Plan selection belongs to the upgrade page once a workspace exists."""

    def test_workspace_owner_is_redirected_to_upgrade(
        self, logged_in_client: Client, user: User
    ) -> None:
        """A user with a workspace never sees the pre-signup plan page."""
        workspace = Workspace.objects.create(
            name="Acme", subscription_plan="free", subscription_status="active"
        )
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

        response = logged_in_client.get(reverse("core:select_plan"))

        assert response.status_code == 302
        assert response.url == reverse("core:upgrade_plan")

    def test_plain_member_is_redirected_to_dashboard(
        self, logged_in_client: Client, user: User
    ) -> None:
        """Non-admin members go to the dashboard, not the upgrade page
        (which would bounce them for lacking billing permissions)."""
        workspace = Workspace.objects.create(
            name="Acme", subscription_plan="free", subscription_status="active"
        )
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="user")

        response = logged_in_client.get(reverse("core:select_plan"))

        assert response.status_code == 302
        assert response.url == reverse("core:dashboard")

    def test_workspace_less_user_still_sees_plans(
        self, logged_in_client: Client
    ) -> None:
        """Without a workspace the page renders (manual funnel intact)."""
        response = logged_in_client.get(reverse("core:select_plan"))

        assert response.status_code == 200

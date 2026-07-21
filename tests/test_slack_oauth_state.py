"""Tests for Slack OAuth CSRF (state) protection and admin gating.

These tests cover the security hardening of the Slack OAuth flows:

- ``slack_connect`` / ``slack_connect_callback`` (workspace notifications):
  the callback must reject a missing or mismatched ``state`` parameter
  *before* exchanging the authorization code for a token.
- ``slack_auth`` / ``slack_auth_callback`` (login): the login callback must
  validate ``state`` and reject unverified Slack emails.
- ``configure_slack``: only workspace admins/owners may mutate the
  destination channel.
"""

import json
from typing import Any
from unittest.mock import Mock, patch

import pytest
from core.models import Integration, UserProfile, Workspace, WorkspaceMember
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

SLACK_CONNECT_SETTINGS = {
    "SLACK_CLIENT_ID": "test_client_id",
    "SLACK_CLIENT_SECRET": "test_client_secret",
    "SLACK_CONNECT_REDIRECT_URI": "http://localhost/connect/callback/",
}


@pytest.mark.django_db
class TestSlackConnectState:
    """Test CSRF state handling for the workspace Slack connect flow."""

    @pytest.fixture
    def owner_user(self, client: Client) -> tuple[User, Workspace]:
        """Create an owner user (via UserProfile) and their workspace.

        Args:
            client: Django test client.

        Returns:
            Tuple of the created user and workspace.
        """
        user = User.objects.create_user(
            username="owner@example.com",
            password="pw",
            email="owner@example.com",
        )
        workspace = Workspace.objects.create(name="Test Workspace")
        UserProfile.objects.create(user=user, workspace=workspace)
        return user, workspace

    @override_settings(**SLACK_CONNECT_SETTINGS)
    def test_connect_stores_state_in_session(
        self, client: Client, owner_user: tuple[User, Workspace]
    ) -> None:
        """slack_connect stores a state token and includes it in the URL."""
        user, _ = owner_user
        client.force_login(user)

        response = client.get(reverse("core:slack_connect"))

        assert response.status_code == 302
        state = client.session["slack_connect_oauth_state"]
        assert state
        assert f"state={state}" in response.url

    @override_settings(**SLACK_CONNECT_SETTINGS)
    @patch("core.views.integrations.slack.requests.post")
    def test_callback_missing_state_rejected_without_exchange(
        self,
        mock_post: Mock,
        client: Client,
        owner_user: tuple[User, Workspace],
    ) -> None:
        """A callback with no state is rejected before any token exchange."""
        user, workspace = owner_user
        client.force_login(user)

        session = client.session
        session["slack_connect_oauth_state"] = "expected_state"
        session.save()

        response = client.get(reverse("core:slack_connect_callback"), {"code": "abc"})

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        mock_post.assert_not_called()
        assert not Integration.objects.filter(workspace=workspace).exists()

    @override_settings(**SLACK_CONNECT_SETTINGS)
    @patch("core.views.integrations.slack.requests.post")
    def test_callback_wrong_state_rejected_without_exchange(
        self,
        mock_post: Mock,
        client: Client,
        owner_user: tuple[User, Workspace],
    ) -> None:
        """A callback with a mismatched state is rejected before exchange."""
        user, workspace = owner_user
        client.force_login(user)

        session = client.session
        session["slack_connect_oauth_state"] = "expected_state"
        session.save()

        response = client.get(
            reverse("core:slack_connect_callback"),
            {"code": "abc", "state": "attacker_state"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        mock_post.assert_not_called()
        assert not Integration.objects.filter(workspace=workspace).exists()

    @override_settings(**SLACK_CONNECT_SETTINGS)
    @patch("core.views.integrations.slack.requests.post")
    def test_callback_valid_state_proceeds(
        self,
        mock_post: Mock,
        client: Client,
        owner_user: tuple[User, Workspace],
    ) -> None:
        """A callback with a matching state exchanges the code and connects."""
        user, workspace = owner_user
        client.force_login(user)

        session = client.session
        session["slack_connect_oauth_state"] = "shared_state"
        session.save()

        token_response = Mock()
        token_response.json.return_value = {
            "ok": True,
            "access_token": "xoxb-token",
            "team": {"id": "T123", "name": "Test Team"},
            "incoming_webhook": {
                "channel": "#general",
                "url": "https://hooks.slack.com/services/xxx",
            },
        }
        mock_post.return_value = token_response

        response = client.get(
            reverse("core:slack_connect_callback"),
            {"code": "abc", "state": "shared_state"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        mock_post.assert_called_once()
        integration = Integration.objects.get(
            workspace=workspace, integration_type="slack_notifications"
        )
        assert integration.oauth_credentials["access_token"] == "xoxb-token"


@pytest.mark.django_db
class TestSlackAuthState:
    """Test CSRF state and email verification for the Slack login flow."""

    @override_settings(
        SLACK_CLIENT_ID="cid",
        SLACK_REDIRECT_URI="http://localhost/auth/callback/",
    )
    def test_auth_stores_state_in_session(self, client: Client) -> None:
        """slack_auth stores a state token and includes it in the URL."""
        response = client.get(reverse("core:slack_auth"))

        assert response.status_code == 302
        state = client.session["slack_auth_oauth_state"]
        assert state
        assert f"state={state}" in response.url

    @patch("core.views.auth._get_slack_token")
    def test_auth_callback_missing_state_rejected(
        self, mock_get_token: Mock, client: Client
    ) -> None:
        """A login callback with no state is rejected before token exchange."""
        session = client.session
        session["slack_auth_oauth_state"] = "expected"
        session.save()

        response = client.get(reverse("core:slack_auth_callback"), {"code": "abc"})

        assert response.status_code == 400
        mock_get_token.assert_not_called()
        assert not User.objects.exists()

    @patch("core.views.auth._get_slack_token")
    def test_auth_callback_wrong_state_rejected(
        self, mock_get_token: Mock, client: Client
    ) -> None:
        """A login callback with a mismatched state is rejected."""
        session = client.session
        session["slack_auth_oauth_state"] = "expected"
        session.save()

        response = client.get(
            reverse("core:slack_auth_callback"),
            {"code": "abc", "state": "attacker"},
        )

        assert response.status_code == 400
        mock_get_token.assert_not_called()
        assert not User.objects.exists()

    @patch("core.views.auth.login")
    @patch("core.views.auth._get_slack_user_info")
    @patch("core.views.auth._get_slack_token")
    def test_auth_callback_valid_state_and_verified_email_logs_in(
        self,
        mock_get_token: Mock,
        mock_get_user_info: Mock,
        mock_login: Mock,
        client: Client,
    ) -> None:
        """A valid state with a verified email creates and logs in the user.

        ``login`` is patched because the project configures multiple auth
        backends; the assertion here is that the flow reaches login rather
        than exercising Django's session login internals.
        """
        session = client.session
        session["slack_auth_oauth_state"] = "shared"
        session.save()

        mock_get_token.return_value = {"ok": True, "access_token": "tok"}
        mock_get_user_info.return_value = {
            "ok": True,
            "sub": "U123",
            "email": "new@example.com",
            "name": "New User",
            "email_verified": True,
        }

        response = client.get(
            reverse("core:slack_auth_callback"),
            {"code": "abc", "state": "shared"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:dashboard")
        assert User.objects.filter(email="new@example.com").exists()
        mock_login.assert_called_once()

    @patch("core.views.auth._get_slack_user_info")
    @patch("core.views.auth._get_slack_token")
    def test_auth_callback_unverified_email_rejected(
        self,
        mock_get_token: Mock,
        mock_get_user_info: Mock,
        client: Client,
    ) -> None:
        """Login is rejected when Slack reports the email is unverified."""
        session = client.session
        session["slack_auth_oauth_state"] = "shared"
        session.save()

        mock_get_token.return_value = {"ok": True, "access_token": "tok"}
        mock_get_user_info.return_value = {
            "ok": True,
            "sub": "U123",
            "email": "unverified@example.com",
            "name": "Unverified User",
            "email_verified": False,
        }

        response = client.get(
            reverse("core:slack_auth_callback"),
            {"code": "abc", "state": "shared"},
        )

        assert response.status_code == 400
        assert not User.objects.filter(email="unverified@example.com").exists()


@pytest.mark.django_db
class TestConfigureSlackAdminGate:
    """Test that configure_slack requires an admin/owner role."""

    def _make_member(self, role: str) -> tuple[User, Workspace, Integration]:
        """Create a user with the given workspace role and a Slack integration.

        Args:
            role: WorkspaceMember role to assign.

        Returns:
            Tuple of the user, workspace, and Slack integration.
        """
        user = User.objects.create_user(
            username=f"{role}@example.com",
            password="pw",
            email=f"{role}@example.com",
        )
        workspace = Workspace.objects.create(name=f"WS {role}")
        WorkspaceMember.objects.create(
            user=user, workspace=workspace, role=role, is_active=True
        )
        integration = Integration.objects.create(
            workspace=workspace,
            integration_type="slack_notifications",
            oauth_credentials={"access_token": "tok"},
            integration_settings={"channel": "#general"},
            is_active=True,
        )
        return user, workspace, integration

    def _post_channel(self, client: Client, channel: str) -> Any:
        """POST a channel change to configure_slack.

        Args:
            client: Django test client.
            channel: Desired channel value.

        Returns:
            The HTTP response.
        """
        return client.post(
            reverse("core:configure_slack"),
            data=json.dumps({"channel": channel}),
            content_type="application/json",
        )

    def test_non_admin_member_forbidden(self, client: Client) -> None:
        """A plain member cannot change the Slack destination channel."""
        user, _, integration = self._make_member("user")
        client.force_login(user)

        response = self._post_channel(client, "#evil")

        # JSON (fetch) endpoint: non-admins get a JSON 403, not a redirect.
        assert response.status_code == 403
        assert response.json()["error"]
        integration.refresh_from_db()
        assert integration.integration_settings["channel"] == "#general"

    def test_non_admin_get_channels_forbidden_json(self, client: Client) -> None:
        """A plain member gets a JSON 403 from get_slack_channels."""
        user, _, _ = self._make_member("user")
        client.force_login(user)

        response = client.get(reverse("core:get_slack_channels"))

        assert response.status_code == 403
        assert response.json()["error"]

    def test_admin_member_allowed(self, client: Client) -> None:
        """An admin member can change the Slack destination channel."""
        user, _, integration = self._make_member("admin")
        client.force_login(user)

        response = self._post_channel(client, "#announcements")

        assert response.status_code == 200
        integration.refresh_from_db()
        assert integration.integration_settings["channel"] == "#announcements"

    @patch("core.views.integrations.slack.requests.post")
    def test_non_admin_test_slack_forbidden(
        self, mock_post: Mock, client: Client
    ) -> None:
        """A plain member cannot trigger a test message and none is sent."""
        user, _, _ = self._make_member("user")
        client.force_login(user)

        response = client.post(reverse("core:test_slack"))

        # Non-admins are redirected away and no Slack API call is made.
        assert response.status_code == 302
        mock_post.assert_not_called()

    @patch("core.views.integrations.slack.requests.post")
    def test_admin_test_slack_allowed(self, mock_post: Mock, client: Client) -> None:
        """An admin member can trigger a test message via the Slack API."""
        user, _, _ = self._make_member("admin")
        client.force_login(user)

        api_response = Mock()
        api_response.json.return_value = {"ok": True}
        mock_post.return_value = api_response

        response = client.post(reverse("core:test_slack"))

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        mock_post.assert_called_once()

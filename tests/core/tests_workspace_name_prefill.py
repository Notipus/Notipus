"""Tests for prefilling the workspace name from the Slack team name.

The Slack login callback captures the OIDC team-name claim into the
session, and the create-workspace page uses it to prefill the name field
so users are not asked to retype (or improvise) their organization name.
"""

from unittest.mock import Mock, patch

import pytest
from core.constants import SLACK_TEAM_NAME_CLAIM, SLACK_TEAM_NAME_SESSION_KEY
from core.views.auth import WORKSPACE_NAME_MAX_LENGTH
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse


def _mock_userinfo(**extra: str) -> dict[str, object]:
    """Build a minimal successful Slack userInfo response.

    Args:
        extra: Additional claims to merge into the response.

    Returns:
        A userInfo payload accepted by ``slack_auth_callback``.
    """
    return {
        "ok": True,
        "sub": "U123",
        "email": "new@example.com",
        "name": "New User",
        "email_verified": True,
        **extra,
    }


@pytest.mark.django_db
class TestSlackTeamNameCapture:
    """Test that the login callback stores the Slack team name."""

    def _callback(self, client: Client, user_info: dict[str, object]) -> None:
        """Run the login callback with a valid state and mocked Slack APIs.

        Args:
            client: The Django test client.
            user_info: The mocked userInfo response payload.
        """
        session = client.session
        session["slack_auth_oauth_state"] = "shared"
        session.save()

        with (
            patch("core.views.auth.login"),
            patch(
                "core.views.auth._get_slack_token",
                return_value={"ok": True, "access_token": "tok"},
            ),
            patch("core.views.auth._get_slack_user_info", return_value=user_info),
        ):
            response = client.get(
                reverse("core:slack_auth_callback"),
                {"code": "abc", "state": "shared"},
            )
        assert response.status_code == 302

    def test_team_name_stored_in_session(self, client: Client) -> None:
        """The team-name claim is captured into the session on login."""
        self._callback(client, _mock_userinfo(**{SLACK_TEAM_NAME_CLAIM: "Acme Inc"}))

        assert client.session[SLACK_TEAM_NAME_SESSION_KEY] == "Acme Inc"

    def test_missing_team_name_leaves_session_unset(self, client: Client) -> None:
        """No session entry is written when Slack omits the claim."""
        self._callback(client, _mock_userinfo())

        assert SLACK_TEAM_NAME_SESSION_KEY not in client.session

    def test_team_name_is_trimmed(self, client: Client) -> None:
        """Surrounding whitespace is stripped before storing."""
        self._callback(
            client, _mock_userinfo(**{SLACK_TEAM_NAME_CLAIM: "  Acme Inc  "})
        )

        assert client.session[SLACK_TEAM_NAME_SESSION_KEY] == "Acme Inc"

    def test_team_name_is_truncated_to_field_length(self, client: Client) -> None:
        """Overlong names are capped at the Workspace.name max length."""
        self._callback(
            client,
            _mock_userinfo(**{SLACK_TEAM_NAME_CLAIM: "x" * 500}),
        )

        stored = client.session[SLACK_TEAM_NAME_SESSION_KEY]
        assert stored == "x" * WORKSPACE_NAME_MAX_LENGTH

    def test_non_string_team_name_ignored(self, client: Client) -> None:
        """A malformed (non-string) claim is not stored."""
        self._callback(
            client,
            _mock_userinfo(**{SLACK_TEAM_NAME_CLAIM: ["not", "a", "string"]}),  # type: ignore[arg-type]
        )

        assert SLACK_TEAM_NAME_SESSION_KEY not in client.session

    def test_whitespace_only_team_name_ignored(self, client: Client) -> None:
        """A claim that trims to empty is not stored."""
        self._callback(client, _mock_userinfo(**{SLACK_TEAM_NAME_CLAIM: "   "}))

        assert SLACK_TEAM_NAME_SESSION_KEY not in client.session

    def test_team_name_survives_login_session_flush(self, client: Client) -> None:
        """The team name is stored after login() and survives its flush.

        Django's ``login()`` flushes the session when a different user
        was previously logged in; the capture must happen after that or
        the value would be lost. Simulate the flush explicitly.
        """
        session = client.session
        session["slack_auth_oauth_state"] = "shared"
        session.save()

        with (
            patch(
                "core.views.auth.login",
                side_effect=lambda request, user, **kwargs: request.session.flush(),
            ),
            patch(
                "core.views.auth._get_slack_token",
                return_value={"ok": True, "access_token": "tok"},
            ),
            patch(
                "core.views.auth._get_slack_user_info",
                return_value=_mock_userinfo(**{SLACK_TEAM_NAME_CLAIM: "Acme Inc"}),
            ),
        ):
            response = client.get(
                reverse("core:slack_auth_callback"),
                {"code": "abc", "state": "shared"},
            )

        assert response.status_code == 302
        assert client.session[SLACK_TEAM_NAME_SESSION_KEY] == "Acme Inc"


@pytest.mark.django_db
class TestCreateWorkspacePrefill:
    """Test that the create-workspace form uses the captured team name."""

    def _login(self, client: Client) -> User:
        """Create and log in a user with no workspace.

        Args:
            client: The Django test client.

        Returns:
            The logged-in user.
        """
        user = User.objects.create_user(
            username="new@example.com", email="new@example.com", password="pw"
        )
        client.force_login(user)
        return user

    def _set_team_name(self, client: Client, team_name: str) -> None:
        """Store a captured Slack team name in the session.

        Args:
            client: The Django test client.
            team_name: The team name to store.
        """
        session = client.session
        session[SLACK_TEAM_NAME_SESSION_KEY] = team_name
        session.save()

    def test_form_prefills_from_session(self, client: Client) -> None:
        """The name input is prefilled with the captured team name."""
        self._login(client)
        self._set_team_name(client, "Acme Inc")

        response = client.get(reverse("core:create_workspace"))

        assert response.status_code == 200
        assert 'value="Acme Inc"' in response.content.decode()

    def test_form_empty_without_captured_name(self, client: Client) -> None:
        """The name input stays empty when no team name was captured."""
        self._login(client)

        response = client.get(reverse("core:create_workspace"))

        assert response.status_code == 200
        assert 'value=""' in response.content.decode()

    def test_team_name_cleared_after_workspace_created(self, client: Client) -> None:
        """Creating a workspace consumes the captured team name."""
        self._login(client)
        self._set_team_name(client, "Acme Inc")

        response = client.post(reverse("core:create_workspace"), {"name": "Acme Inc"})

        assert response.status_code == 302
        assert SLACK_TEAM_NAME_SESSION_KEY not in client.session

    @patch("core.views.auth.login")
    @patch("core.views.auth._get_slack_user_info")
    @patch("core.views.auth._get_slack_token")
    def test_end_to_end_login_to_prefill(
        self,
        mock_get_token: Mock,
        mock_get_user_info: Mock,
        mock_login: Mock,
        client: Client,
    ) -> None:
        """The team name flows from the login callback to the form.

        ``login`` is patched (multiple auth backends are configured), so
        the created user is logged in explicitly before loading the form.
        """
        session = client.session
        session["slack_auth_oauth_state"] = "shared"
        session.save()
        mock_get_token.return_value = {"ok": True, "access_token": "tok"}
        mock_get_user_info.return_value = _mock_userinfo(
            **{SLACK_TEAM_NAME_CLAIM: "Acme Inc"}
        )

        response = client.get(
            reverse("core:slack_auth_callback"),
            {"code": "abc", "state": "shared"},
        )
        assert response.status_code == 302

        client.force_login(User.objects.get(email="new@example.com"))
        response = client.get(reverse("core:create_workspace"))

        assert 'value="Acme Inc"' in response.content.decode()

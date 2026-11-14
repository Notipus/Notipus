"""Tests that bearer tokens in URL paths never reach Google Analytics.

Invitation links carry their token in the path
(``/workspace/invitation/<token>/accept/``), as do feedback links
(``/feedback/industry/<token>/``). Query-string scrubbing alone left them
in every page_view sent to GA4, server-side and from gtag.js.
"""

from collections.abc import Generator
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from core import analytics
from core.models import Workspace, WorkspaceInvitation
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

GA4_TEST_SETTINGS = {
    "GA4_MEASUREMENT_ID": "G-TEST123",
    "GA4_API_SECRET": "test-secret",
}


@pytest.fixture
def ga4() -> Generator[MagicMock, None, None]:
    """Enable GA4 and capture submitted payloads.

    Yields:
        Mock standing in for ``analytics._submit``.
    """
    with override_settings(**GA4_TEST_SETTINGS):
        with patch.object(analytics, "_submit") as mock_submit:
            yield mock_submit


@pytest.fixture
def invitation(db: None) -> WorkspaceInvitation:
    """Create a pending invitation.

    Returns:
        The invitation.
    """
    inviter = User.objects.create_user(username="inviter", email="i@example.com")
    return WorkspaceInvitation.objects.create(
        workspace=Workspace.objects.create(name="Acme"),
        email="invitee@example.com",
        role="user",
        invited_by=inviter,
        expires_at=timezone.now() + timedelta(days=7),
    )


def test_sanitize_redacts_invitation_token() -> None:
    """The invitation token path segment is replaced."""
    url = "https://app.example.com/workspace/invitation/abc-123/accept/?utm_source=x"

    sanitized = analytics.sanitize_page_location(url)

    assert "abc-123" not in sanitized
    assert sanitized == (
        "https://app.example.com/workspace/invitation/redacted/accept/?utm_source=x"
    )


def test_sanitize_redacts_feedback_token() -> None:
    """The feedback token path segment is replaced."""
    sanitized = analytics.sanitize_page_location(
        "https://app.example.com/feedback/industry/tok456/"
    )

    assert sanitized == "https://app.example.com/feedback/industry/redacted/"


def test_sanitize_leaves_other_paths() -> None:
    """Ordinary paths pass through unchanged."""
    url = "https://app.example.com/workspace/members/"

    assert analytics.sanitize_page_location(url) == url


def test_server_page_view_omits_invitation_token(
    ga4: MagicMock, invitation: WorkspaceInvitation
) -> None:
    """The server-side page_view for an invitation page has no token."""
    viewer = User.objects.create_user(username="v", email="invitee@example.com")
    client = Client()
    client.force_login(viewer)

    with override_settings(GA4_CLIENT_SIDE=False):
        response = client.get(
            reverse("core:accept_invitation", args=[invitation.token]),
            HTTP_USER_AGENT="Mozilla/5.0",
        )

    assert response.status_code == 200
    locations = [
        event["params"]["page_location"]
        for call in ga4.call_args_list
        for event in call.args[0]["events"]
        if event["name"] == "page_view"
    ]
    assert locations
    assert all(str(invitation.token) not in loc for loc in locations)


def test_client_side_gtag_omits_invitation_token(
    invitation: WorkspaceInvitation,
) -> None:
    """gtag.js is configured with a sanitized page_location."""
    with override_settings(GA4_CLIENT_SIDE=True, **GA4_TEST_SETTINGS):
        response = Client().get(
            reverse("core:accept_invitation", args=[invitation.token])
        )

    html = response.content.decode()
    assert "page_location" in html
    gtag_config = html[html.index("gtag('config'") :].split("</script>")[0]
    assert str(invitation.token) not in gtag_config
    assert "/workspace/invitation/redacted/accept/" in gtag_config

"""Tests that logging out requires a CSRF-protected POST.

With ACCOUNT_LOGOUT_ON_GET a cross-site link or <img> pointing at
/accounts/logout/ signed users out. Logout is now POST-only and the nav
and invitation page submit forms instead of linking.
"""

import re
from datetime import timedelta

import pytest
from core.models import Workspace, WorkspaceInvitation, WorkspaceMember
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone


@pytest.fixture
def member_client(db: None) -> Client:
    """A client logged in as a workspace owner.

    Returns:
        The logged-in client.
    """
    user = User.objects.create_user(username="owner", email="owner@example.com")
    workspace = Workspace.objects.create(name="Acme")
    WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")
    client = Client()
    client.force_login(user)
    return client


def test_get_does_not_log_out(member_client: Client) -> None:
    """A GET to the logout URL leaves the session signed in."""
    response = member_client.get(reverse("account_logout"))

    assert response.status_code == 200
    assert "_auth_user_id" in member_client.session


def test_post_logs_out(member_client: Client) -> None:
    """A POST to the logout URL signs the user out."""
    response = member_client.post(reverse("account_logout"))

    assert response.status_code == 302
    assert "_auth_user_id" not in member_client.session


def test_nav_logout_is_a_post_form(member_client: Client) -> None:
    """Both nav logout controls are POST forms, not links."""
    html = member_client.get(reverse("core:workspace_settings")).content.decode()
    logout_url = reverse("account_logout")

    assert f'href="{logout_url}"' not in html
    forms = re.findall(
        rf'<form method="post" action="{re.escape(logout_url)}"[^>]*>(.*?)</form>',
        html,
        re.S,
    )
    assert len(forms) == 2
    assert all("csrfmiddlewaretoken" in form for form in forms)


@pytest.mark.django_db
def test_invitation_switch_account_is_a_post_form() -> None:
    """The wrong-account invitation page signs out through a POST form."""
    inviter = User.objects.create_user(username="inv", email="inv@example.com")
    invitation = WorkspaceInvitation.objects.create(
        workspace=Workspace.objects.create(name="Acme"),
        email="invitee@example.com",
        role="user",
        invited_by=inviter,
        expires_at=timezone.now() + timedelta(days=7),
    )
    other = User.objects.create_user(username="other", email="other@example.com")
    client = Client()
    client.force_login(other)

    html = client.get(
        reverse("core:accept_invitation", args=[invitation.token])
    ).content.decode()

    logout_url = reverse("account_logout")
    assert f'href="{logout_url}' not in html
    assert f'action="{logout_url}"' in html

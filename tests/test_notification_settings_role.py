"""Tests that only workspace admins/owners can change notification settings."""

import json

import pytest
from core.models import Workspace, WorkspaceMember
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse


def _client_for(role: str) -> tuple[Client, Workspace]:
    """Log in a member with ``role`` on a fresh workspace.

    Args:
        role: WorkspaceMember role.

    Returns:
        The logged-in client and the workspace.
    """
    workspace = Workspace.objects.create(name="Acme")
    user = User.objects.create_user(username=role, email=f"{role}@example.com")
    WorkspaceMember.objects.create(user=user, workspace=workspace, role=role)
    client = Client()
    client.force_login(user)
    return client, workspace


def _update(client: Client) -> int:
    """POST a settings change turning payment-success notifications off.

    Args:
        client: The logged-in client.

    Returns:
        Response status code.
    """
    response = client.post(
        reverse("core:update_notification_settings"),
        data=json.dumps({"notify_payment_success": False}),
        content_type="application/json",
    )
    return response.status_code


@pytest.mark.django_db
def test_view_only_member_cannot_update_settings() -> None:
    """A plain member gets 403 and the setting is unchanged."""
    client, workspace = _client_for("user")

    assert _update(client) == 403
    workspace.notification_settings.refresh_from_db()
    assert workspace.notification_settings.notify_payment_success is True


@pytest.mark.django_db
def test_view_only_member_can_still_read_settings() -> None:
    """Reading settings stays open to every member."""
    client, _ = _client_for("user")

    response = client.get(reverse("core:get_notification_settings"))

    assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["owner", "admin"])
def test_admin_and_owner_can_update_settings(role: str) -> None:
    """Owners and admins can change settings."""
    client, workspace = _client_for(role)

    assert _update(client) == 200
    workspace.notification_settings.refresh_from_db()
    assert workspace.notification_settings.notify_payment_success is False

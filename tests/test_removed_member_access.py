"""Tests that a removed workspace member loses access, legacy profile or not.

Removing a member only deactivates their WorkspaceMember row, and their
UserProfile keeps pointing at the workspace. Authorization used to fall
back to that profile whenever no active membership existed and treated
the user as owner, so a removed creator kept full control. Migration
0038 gives genuine profile-only users a membership row instead.
"""

import importlib

import pytest
from core.models import UserProfile, Workspace, WorkspaceMember
from core.services.dashboard import DashboardService
from core.services.recipients import admin_emails
from core.views.integrations.base import get_user_workspace, require_admin_role
from core.views.integrations.slack import _get_admin_workspace
from django.apps import apps
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpRequest
from django.test import Client, RequestFactory
from django.urls import reverse


def _request(user: User | AnonymousUser) -> HttpRequest:
    """Build a GET request for ``user`` with message storage.

    Args:
        user: The requesting user.

    Returns:
        A request usable by the authorization helpers.
    """
    request = RequestFactory().get("/")
    request.user = user
    request.session = {}  # type: ignore[assignment]
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    return request


@pytest.fixture
def removed_creator() -> tuple[User, Workspace]:
    """A workspace creator who was then removed by another owner.

    Returns:
        The removed user and the workspace they were removed from.
    """
    workspace = Workspace.objects.create(name="Acme")
    creator = User.objects.create_user(username="creator", email="c@example.com")
    UserProfile.objects.create(user=creator, workspace=workspace)
    WorkspaceMember.objects.create(
        user=creator, workspace=workspace, role="owner", is_active=False
    )
    other = User.objects.create_user(username="other", email="o@example.com")
    WorkspaceMember.objects.create(user=other, workspace=workspace, role="owner")
    return creator, workspace


@pytest.mark.django_db
def test_removed_creator_has_no_workspace(
    removed_creator: tuple[User, Workspace],
) -> None:
    """The profile link no longer resolves a workspace for a removed member."""
    creator, _ = removed_creator
    request = _request(creator)

    assert get_user_workspace(request) is None
    workspace, redirect = require_admin_role(request)
    assert workspace is None
    assert redirect is not None
    assert _get_admin_workspace(request) is None
    assert DashboardService().get_dashboard_data(creator) is None


@pytest.mark.django_db
def test_removed_creator_cannot_read_notification_settings(
    removed_creator: tuple[User, Workspace],
) -> None:
    """The settings API refuses a removed member."""
    creator, _ = removed_creator
    client = Client()
    client.force_login(creator)

    response = client.get(reverse("core:get_notification_settings"))

    assert response.status_code == 404


@pytest.mark.django_db
def test_removed_creator_gets_no_admin_emails(
    removed_creator: tuple[User, Workspace],
) -> None:
    """Operational email is not sent to a removed member."""
    _, workspace = removed_creator
    WorkspaceMember.objects.filter(workspace=workspace, is_active=True).delete()

    assert admin_emails(workspace) == []


@pytest.mark.django_db
def test_active_member_keeps_access() -> None:
    """An active owner still resolves their workspace."""
    workspace = Workspace.objects.create(name="Acme")
    user = User.objects.create_user(username="owner", email="owner@example.com")
    WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

    request = _request(user)
    assert get_user_workspace(request) == workspace
    assert require_admin_role(request) == (workspace, None)


def _run_backfill() -> None:
    """Run the 0038 data migration against the current schema."""
    migration = importlib.import_module(
        "core.migrations.0038_backfill_profile_only_memberships"
    )
    migration.backfill_memberships(apps, None)


@pytest.mark.django_db
def test_backfill_grants_profile_only_user_owner() -> None:
    """A legacy user linked only by profile becomes owner of their workspace."""
    workspace = Workspace.objects.create(name="Legacy")
    user = User.objects.create_user(username="legacy", email="l@example.com")
    UserProfile.objects.create(user=user, workspace=workspace)

    _run_backfill()

    member = WorkspaceMember.objects.get(user=user)
    assert member.workspace == workspace
    assert member.role == "owner"
    assert member.is_active
    assert get_user_workspace(_request(user)) == workspace


@pytest.mark.django_db
def test_backfill_uses_admin_when_workspace_has_owner() -> None:
    """A profile-only user beside a real owner is backfilled as admin."""
    workspace = Workspace.objects.create(name="Legacy")
    owner = User.objects.create_user(username="owner", email="ow@example.com")
    WorkspaceMember.objects.create(user=owner, workspace=workspace, role="owner")
    user = User.objects.create_user(username="legacy", email="l@example.com")
    UserProfile.objects.create(user=user, workspace=workspace)

    _run_backfill()

    assert WorkspaceMember.objects.get(user=user).role == "admin"


@pytest.mark.django_db
def test_backfill_does_not_restore_removed_member(
    removed_creator: tuple[User, Workspace],
) -> None:
    """A deactivated membership is left alone, so removal sticks."""
    creator, _ = removed_creator

    _run_backfill()

    assert WorkspaceMember.objects.filter(user=creator).count() == 1
    assert not WorkspaceMember.objects.get(user=creator).is_active

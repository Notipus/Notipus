"""Tests for the per-workspace daily cap on invitation and setup emails.

Both forms send mail to any address an admin types; without a cap one
workspace could send unlimited email through the app.
"""

from collections.abc import Generator

import pytest
from core.models import Plan, Workspace, WorkspaceInvitation, WorkspaceMember
from core.services import email_quota
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "email-quota-tests",
    }
}


@pytest.fixture(autouse=True)
def real_cache() -> Generator[None, None, None]:
    """Use a working cache (test settings use DummyCache) and a cap of 2.

    Yields:
        Nothing; settings are restored afterwards.
    """
    from django.core.cache import cache

    with override_settings(
        CACHES=LOCMEM_CACHE,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    ):
        cache.clear()
        original = email_quota.DAILY_EMAIL_CAP
        email_quota.DAILY_EMAIL_CAP = 2
        yield
        email_quota.DAILY_EMAIL_CAP = original
        cache.clear()


@pytest.fixture
def owner_client(
    workspace: Workspace, owner_user: User, owner_member: WorkspaceMember
) -> Client:
    """A client logged in as an owner on a plan with plenty of seats.

    Returns:
        The logged-in client.
    """
    Plan.objects.update_or_create(
        name=workspace.subscription_plan,
        defaults={"display_name": "P", "max_users": 50, "is_active": True},
    )
    client = Client()
    client.force_login(owner_user)
    return client


def test_quota_allows_up_to_cap_per_workspace() -> None:
    """The cap is counted per workspace and per kind."""
    assert email_quota.consume_daily_email_quota(1, email_quota.INVITATION)
    assert email_quota.consume_daily_email_quota(1, email_quota.INVITATION)
    assert not email_quota.consume_daily_email_quota(1, email_quota.INVITATION)
    assert email_quota.consume_daily_email_quota(2, email_quota.INVITATION)
    assert email_quota.consume_daily_email_quota(1, email_quota.SETUP_INSTRUCTIONS)


@pytest.mark.django_db
def test_invitations_capped_per_day(owner_client: Client) -> None:
    """The third invitation of the day is refused with a friendly error."""
    for i in range(3):
        response = owner_client.post(
            reverse("core:invite_member"),
            {"email": f"person{i}@example.com", "role": "user"},
            follow=True,
        )

    assert WorkspaceInvitation.objects.count() == 2
    texts = [str(m) for m in response.context["messages"]]
    assert any("today's limit" in t for t in texts)


@pytest.mark.django_db
def test_setup_instructions_capped_per_day(owner_client: Client) -> None:
    """The third setup-instructions email of the day is not sent."""
    for i in range(3):
        response = owner_client.post(
            reverse("core:email_setup_instructions", args=["stripe"]),
            {"recipient_email": f"dev{i}@example.com"},
            follow=True,
        )

    assert len(mail.outbox) == 2
    texts = [str(m) for m in response.context["messages"]]
    assert any("today's limit" in t for t in texts)

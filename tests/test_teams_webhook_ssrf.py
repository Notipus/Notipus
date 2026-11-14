"""Tests that the Teams webhook URL can only target Microsoft webhook hosts.

The server POSTs to the saved URL, and the "Test" button does so on
demand, so accepting any https:// host let an admin make the server probe
internal addresses (SSRF).
"""

from unittest.mock import MagicMock, patch

import pytest
from core.models import Integration, Workspace, WorkspaceMember
from core.views.integrations.teams import _is_valid_webhook_url
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse


@pytest.fixture
def owner() -> tuple[Client, Workspace]:
    """Log in a workspace owner.

    Returns:
        The client and the workspace.
    """
    user = User.objects.create_user(username="owner", email="owner@example.com")
    workspace = Workspace.objects.create(name="Acme")
    WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")
    client = Client()
    client.force_login(user)
    return client, workspace


@pytest.mark.parametrize(
    "url",
    [
        "https://logic.azure.com/workflows/x",
        "https://prod-12.westus.logic.azure.com:443/workflows/abc?sig=s",
        "https://default1234.ab.environment.api.powerplatform.com/powerautomate/x",
        "https://acme.webhook.office.com/webhookb2/abc",
        "https://outlook.office.com/webhook/abc",
        "https://PROD-1.WestUS.Logic.Azure.com/workflows/abc",
    ],
)
def test_official_hosts_accepted(url: str) -> None:
    """Power Automate, Logic Apps and Office webhook hosts are accepted."""
    assert _is_valid_webhook_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/hook",
        "https://[::1]/hook",
        "https://localhost/hook",
        "https://internal.example.com/hook",
        "https://logic.azure.com.evil.example/workflows/x",
        "https://evillogic.azure.com/workflows/x",
        "https://prod-1.westus.logic.azure.com:8443/workflows/x",
        "https://user:pw@prod-1.westus.logic.azure.com/workflows/x",
        "http://prod-1.westus.logic.azure.com/workflows/x",
    ],
)
def test_internal_and_foreign_hosts_rejected(url: str) -> None:
    """Internal addresses, look-alike hosts and odd ports are refused."""
    assert _is_valid_webhook_url(url) is False


@pytest.mark.django_db
def test_connect_rejects_internal_url(owner: tuple[Client, Workspace]) -> None:
    """Saving an internal URL stores nothing."""
    client, _ = owner

    client.post(
        reverse("core:teams_connect"),
        data={"webhook_url": "https://169.254.169.254/latest/"},
    )

    assert not Integration.objects.filter(
        integration_type="teams_notifications"
    ).exists()


@pytest.mark.django_db
def test_test_button_refuses_legacy_internal_url(
    owner: tuple[Client, Workspace],
) -> None:
    """A URL saved before validation existed is not posted to."""
    client, workspace = owner
    Integration.objects.create(
        workspace=workspace,
        integration_type="teams_notifications",
        oauth_credentials={"webhook_url": "https://10.0.0.5/hook"},
        is_active=True,
    )

    with patch("core.views.integrations.teams.requests.post") as post:
        client.post(reverse("core:test_teams"))

    post.assert_not_called()


@pytest.mark.django_db
def test_test_button_does_not_follow_redirects(
    owner: tuple[Client, Workspace],
) -> None:
    """The test send disables redirects and treats one as a failure."""
    client, workspace = owner
    Integration.objects.create(
        workspace=workspace,
        integration_type="teams_notifications",
        oauth_credentials={"webhook_url": "https://prod-1.westus.logic.azure.com/w"},
        is_active=True,
    )
    redirect_response = MagicMock(is_redirect=True)
    redirect_response.raise_for_status.return_value = None

    with patch(
        "core.views.integrations.teams.requests.post", return_value=redirect_response
    ) as post:
        response = client.post(reverse("core:test_teams"), follow=True)

    assert post.call_args.kwargs["allow_redirects"] is False
    texts = [str(m) for m in response.context["messages"]]
    assert any("Failed to send test message" in t for t in texts)

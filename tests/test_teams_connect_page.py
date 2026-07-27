"""Rendering tests for the Microsoft Teams connect page.

Guards regressions in ``core/templates/core/teams_connect.html.j2``: the
setup-guidance template comment must be stripped (a multi-line Django
``{# #}`` once leaked onto the sibling Telegram page), the secret webhook
URL must never be rendered back into the field, and the page must expose a
proper top-level heading.
"""

import re

import pytest
from core.models import Workspace, WorkspaceMember
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse


@pytest.fixture
def owner_client(db: None) -> Client:
    """A client logged in as the owner of a workspace (admin-gated view)."""
    user = User.objects.create_user(
        username="owner@example.com", email="owner@example.com", password="pw"
    )
    workspace = Workspace.objects.create(name="Acme")
    WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")
    client = Client()
    client.force_login(user)
    return client


class TestTeamsConnectPage:
    """The connect form renders cleanly and never leaks the webhook URL."""

    def _get(self, client: Client) -> str:
        response = client.get(
            reverse("core:teams_connect"), HTTP_USER_AGENT="Mozilla/5.0"
        )
        assert response.status_code == 200
        return response.content.decode()

    def test_setup_comment_is_stripped(self, owner_client: Client) -> None:
        """The guidance comment must be stripped, not printed on the page."""
        body = self._get(owner_client)
        assert "Never render the stored webhook URL" not in body
        assert "{% comment %}" not in body
        assert "{#" not in body

    def test_webhook_field_never_prefilled(self, owner_client: Client) -> None:
        """The secret webhook URL is never rendered back into its input."""
        body = self._get(owner_client)
        tag = re.search(r'<input[^>]*id="webhook_url"[^>]*>', body)
        assert tag is not None
        assert "value=" not in tag.group(0)
        assert 'autocomplete="off"' in tag.group(0)

    def test_page_has_top_level_heading(self, owner_client: Client) -> None:
        """The primary heading is an <h1>, consistent with sibling pages."""
        body = self._get(owner_client)
        assert "<h1" in body


class TestTeamsWebhookUrlValidation:
    """The connect form rejects structurally-invalid webhook URLs.

    A scheme-only value like ``https://`` must not be storable: it would be
    saved and then fail every send/test at runtime.
    """

    def test_is_valid_webhook_url_rejects_scheme_only(self) -> None:
        """``https://`` (no host) is rejected by the structural check."""
        from core.views.integrations.teams import _is_valid_webhook_url

        assert _is_valid_webhook_url("https://") is False
        assert _is_valid_webhook_url("http://example.com/wf") is False
        assert _is_valid_webhook_url("not a url") is False
        assert _is_valid_webhook_url("https://logic.azure.com/workflows/x") is True

    def test_post_scheme_only_url_is_not_stored(self, owner_client: Client) -> None:
        """POSTing ``https://`` re-renders with an error and stores nothing."""
        from core.models import Integration

        response = owner_client.post(
            reverse("core:teams_connect"),
            data={"webhook_url": "https://"},
            HTTP_USER_AGENT="Mozilla/5.0",
        )
        # Invalid input redirects back to the form (does not persist).
        assert response.status_code == 302
        assert not Integration.objects.filter(
            integration_type="teams_notifications"
        ).exists()

    def test_post_valid_url_creates_integration(self, owner_client: Client) -> None:
        """A well-formed https webhook URL is stored."""
        from core.models import Integration

        response = owner_client.post(
            reverse("core:teams_connect"),
            data={"webhook_url": "https://logic.azure.com/workflows/abc"},
            HTTP_USER_AGENT="Mozilla/5.0",
        )
        assert response.status_code == 302
        integration = Integration.objects.get(integration_type="teams_notifications")
        assert integration.oauth_credentials["webhook_url"] == (
            "https://logic.azure.com/workflows/abc"
        )

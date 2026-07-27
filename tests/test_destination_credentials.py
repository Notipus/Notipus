"""Tests for the shared destination-credential lookups.

``collect_destinations`` is the single source of truth for which
destinations a workspace delivers to; both the immediate router and the
delayed queue call it, so these tests lock in the order and filtering that
the two paths now share.
"""

import pytest
from core.models import Integration, Workspace
from webhooks.services.destination_credentials import (
    collect_destinations,
    get_slack_credentials,
)


@pytest.fixture
def workspace(db: None) -> Workspace:
    """A bare workspace with no destinations configured."""
    return Workspace.objects.create(name="Acme", shop_domain="acme.myshopify.com")


def _add(workspace: Workspace, integration_type: str, creds: dict) -> None:
    Integration.objects.create(
        workspace=workspace,
        integration_type=integration_type,
        is_active=True,
        oauth_credentials=creds,
    )


class TestGetSlackCredentials:
    """Slack webhook URL comes from oauth_credentials.incoming_webhook.url."""

    def test_none_workspace_returns_none(self) -> None:
        """A missing workspace yields no credentials."""
        assert get_slack_credentials(None) is None

    def test_no_integration_returns_none(self, workspace: Workspace) -> None:
        """A workspace without a Slack integration yields no credentials."""
        assert get_slack_credentials(workspace) is None

    def test_returns_webhook_url(self, workspace: Workspace) -> None:
        """An active Slack integration yields its incoming-webhook URL."""
        _add(
            workspace,
            "slack_notifications",
            {"incoming_webhook": {"url": "https://hooks.slack.com/services/x"}},
        )
        assert get_slack_credentials(workspace) == {
            "webhook_url": "https://hooks.slack.com/services/x"
        }

    def test_missing_url_returns_none(self, workspace: Workspace) -> None:
        """A Slack integration lacking a webhook URL yields None."""
        _add(workspace, "slack_notifications", {"incoming_webhook": {}})
        assert get_slack_credentials(workspace) is None


class TestCollectDestinations:
    """collect_destinations returns every configured destination, in order."""

    def test_none_when_nothing_configured(self, workspace: Workspace) -> None:
        """No integrations means an empty destination list."""
        assert collect_destinations(workspace) == []

    def test_none_workspace(self) -> None:
        """A missing workspace means an empty destination list."""
        assert collect_destinations(None) == []

    def test_only_configured_destinations_included(self, workspace: Workspace) -> None:
        """Only destinations with valid credentials are returned."""
        _add(
            workspace,
            "telegram_notifications",
            {"bot_token": "123:abc", "chat_id": "-100"},
        )
        assert collect_destinations(workspace) == [
            ("telegram", {"bot_token": "123:abc", "chat_id": "-100"})
        ]

    def test_stable_order_slack_telegram_teams(self, workspace: Workspace) -> None:
        """All three are returned in a stable slack→telegram→teams order."""
        _add(
            workspace,
            "teams_notifications",
            {"webhook_url": "https://logic.azure.com/workflows/x"},
        )
        _add(
            workspace,
            "telegram_notifications",
            {"bot_token": "123:abc", "chat_id": "-100"},
        )
        _add(
            workspace,
            "slack_notifications",
            {"incoming_webhook": {"url": "https://hooks.slack.com/services/x"}},
        )
        names = [name for name, _ in collect_destinations(workspace)]
        assert names == ["slack", "telegram", "teams"]

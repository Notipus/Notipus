"""Tests for ThreadMappingService and Slack plugin thread support.

Tests CRUD operations for SlackThreadMapping and thread support in Slack plugin.
"""

from unittest.mock import Mock, patch

import pytest
from core.models import Workspace
from django.test import TestCase
from webhooks.models import SlackThreadMapping
from webhooks.services.thread_mapping import ThreadInfo, ThreadMappingService


@pytest.mark.django_db
class TestThreadMappingService(TestCase):
    """Tests for ThreadMappingService."""

    def setUp(self):
        """Set up test fixtures."""
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            slug="test-workspace",
        )
        self.service = ThreadMappingService()

    def test_store_thread_ts_creates_mapping(self):
        """Test storing a new thread mapping."""
        mapping = self.service.store_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="C123456",
            thread_ts="1234567890.123456",
        )

        assert mapping.id is not None
        assert mapping.workspace == self.workspace
        assert mapping.entity_type == "zendesk_ticket"
        assert mapping.entity_id == "12345"
        assert mapping.slack_channel_id == "C123456"
        assert mapping.slack_thread_ts == "1234567890.123456"

    def test_get_thread_ts_returns_existing_mapping(self):
        """Test getting an existing thread mapping."""
        # Create a mapping first
        self.service.store_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="C123456",
            thread_ts="1234567890.123456",
        )

        # Retrieve it
        result = self.service.get_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
        )

        assert result is not None
        assert isinstance(result, ThreadInfo)
        assert result.channel_id == "C123456"
        assert result.thread_ts == "1234567890.123456"

    def test_get_thread_ts_returns_none_for_missing_mapping(self):
        """Test getting a non-existent thread mapping."""
        result = self.service.get_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="nonexistent",
        )

        assert result is None

    def test_update_thread_ts_updates_existing_mapping(self):
        """Test updating an existing thread mapping."""
        # Create a mapping
        self.service.store_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="C123456",
            thread_ts="1234567890.123456",
        )

        # Update it
        result = self.service.update_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            thread_ts="9999999999.999999",
        )

        assert result is True

        # Verify update
        mapping = SlackThreadMapping.objects.get(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
        )
        assert mapping.slack_thread_ts == "9999999999.999999"

    def test_update_thread_ts_returns_false_for_missing_mapping(self):
        """Test updating a non-existent thread mapping."""
        result = self.service.update_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="nonexistent",
            thread_ts="9999999999.999999",
        )

        assert result is False

    def test_get_or_create_thread_returns_existing(self):
        """Test get_or_create returns existing mapping."""
        # Create a mapping first
        self.service.store_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="C123456",
            thread_ts="1234567890.123456",
        )

        # Get or create
        result, created = self.service.get_or_create_thread(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="CDIFFERENT",
            thread_ts="9999999999.999999",  # Different values
        )

        assert result is not None
        assert created is False
        # Should return original values, not the new ones
        assert result.channel_id == "C123456"
        assert result.thread_ts == "1234567890.123456"

    def test_get_or_create_thread_creates_new(self):
        """Test get_or_create creates new mapping."""
        result, created = self.service.get_or_create_thread(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="new_ticket",
            channel_id="C123456",
            thread_ts="1234567890.123456",
        )

        assert result is not None
        assert created is True
        assert result.channel_id == "C123456"
        assert result.thread_ts == "1234567890.123456"

    def test_get_or_create_thread_without_thread_ts(self):
        """Test get_or_create returns None without thread_ts for new mapping."""
        result, created = self.service.get_or_create_thread(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="new_ticket",
            channel_id="C123456",
            thread_ts=None,  # No thread_ts
        )

        assert result is None
        assert created is False

    def test_delete_thread_mapping(self):
        """Test deleting a thread mapping."""
        # Create a mapping
        self.service.store_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="C123456",
            thread_ts="1234567890.123456",
        )

        # Delete it
        result = self.service.delete_thread_mapping(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
        )

        assert result is True

        # Verify deletion
        assert not SlackThreadMapping.objects.filter(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
        ).exists()

    def test_delete_thread_mapping_returns_false_for_missing(self):
        """Test deleting a non-existent thread mapping."""
        result = self.service.delete_thread_mapping(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="nonexistent",
        )

        assert result is False

    def test_mappings_are_workspace_scoped(self):
        """Test that mappings are scoped to workspace."""
        workspace2 = Workspace.objects.create(
            name="Other Workspace",
            slug="other-workspace",
        )

        # Create mapping in workspace 1
        self.service.store_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="C123456",
            thread_ts="1111111111.111111",
        )

        # Create mapping with same entity_id in workspace 2
        self.service.store_thread_ts(
            workspace=workspace2,
            entity_type="zendesk_ticket",
            entity_id="12345",
            channel_id="C654321",
            thread_ts="2222222222.222222",
        )

        # Verify they are separate
        result1 = self.service.get_thread_ts(
            workspace=self.workspace,
            entity_type="zendesk_ticket",
            entity_id="12345",
        )
        result2 = self.service.get_thread_ts(
            workspace=workspace2,
            entity_type="zendesk_ticket",
            entity_id="12345",
        )

        assert result1.thread_ts == "1111111111.111111"
        assert result2.thread_ts == "2222222222.222222"


class TestSlackPluginThreadSupport:
    """Tests for Slack plugin thread support."""

    @pytest.fixture
    def slack_plugin(self):
        """Create a Slack destination plugin instance."""
        from plugins.destinations.slack import SlackDestinationPlugin

        return SlackDestinationPlugin()

    def test_send_via_webhook_includes_thread_ts(self, slack_plugin):
        """Test that webhook send includes thread_ts in payload."""
        formatted = {"blocks": [], "color": "#28a745"}

        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_post.return_value = mock_response

            result = slack_plugin.send(
                formatted,
                credentials={"webhook_url": "https://hooks.slack.com/test"},
                options={"thread_ts": "1234567890.123456"},
            )

            # Verify thread_ts was included in the payload
            call_args = mock_post.call_args
            payload = call_args[1]["json"]
            assert payload.get("thread_ts") == "1234567890.123456"
            assert result["success"] is True

    def test_send_via_webhook_without_thread_ts(self, slack_plugin):
        """Test webhook send without thread_ts."""
        formatted = {"blocks": [], "color": "#28a745"}

        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_post.return_value = mock_response

            result = slack_plugin.send(
                formatted,
                credentials={"webhook_url": "https://hooks.slack.com/test"},
            )

            # Verify no thread_ts in payload
            call_args = mock_post.call_args
            payload = call_args[1]["json"]
            assert "thread_ts" not in payload
            assert result["success"] is True

    def test_send_via_api_returns_thread_info(self, slack_plugin):
        """Test API send returns thread information."""
        formatted = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Test"}}]
        }

        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = {
                "ok": True,
                "ts": "1234567890.123456",
                "channel": "C123456",
            }
            mock_post.return_value = mock_response

            result = slack_plugin.send(
                formatted,
                credentials={"bot_token": "xoxb-test-token"},
                options={"channel": "C123456"},
            )

            assert result["success"] is True
            assert result["ts"] == "1234567890.123456"
            assert result["channel"] == "C123456"
            assert (
                result["thread_ts"] == "1234567890.123456"
            )  # First message is thread parent

    def test_send_via_api_as_reply(self, slack_plugin):
        """Test API send as reply to existing thread."""
        formatted = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Reply"}}]
        }

        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = {
                "ok": True,
                "ts": "1234567890.999999",
                "channel": "C123456",
            }
            mock_post.return_value = mock_response

            result = slack_plugin.send(
                formatted,
                credentials={"bot_token": "xoxb-test-token"},
                options={"channel": "C123456", "thread_ts": "1234567890.123456"},
            )

            # Verify thread_ts was sent in request
            call_args = mock_post.call_args
            payload = call_args[1]["json"]
            assert payload["thread_ts"] == "1234567890.123456"

            # Verify result returns the parent thread_ts
            assert result["success"] is True
            assert result["thread_ts"] == "1234567890.123456"

    def test_send_missing_credentials_raises(self, slack_plugin):
        """Test send raises ValueError when credentials are missing."""
        formatted = {"blocks": []}

        with pytest.raises(ValueError, match="Missing"):
            slack_plugin.send(formatted, credentials={})

    def test_send_prefers_api_when_bot_token_present(self, slack_plugin):
        """Test that API is used when bot_token is present."""
        formatted = {"blocks": []}

        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = {
                "ok": True,
                "ts": "123",
                "channel": "C123",
            }
            mock_post.return_value = mock_response

            slack_plugin.send(
                formatted,
                credentials={
                    "webhook_url": "https://hooks.slack.com/test",  # Both present
                    "bot_token": "xoxb-test-token",
                },
                options={"channel": "C123456"},
            )

            # Should call API, not webhook
            call_args = mock_post.call_args
            url = call_args[0][0]
            assert "slack.com/api" in url

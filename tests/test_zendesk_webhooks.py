"""Tests for Zendesk webhook source plugin.

Tests HMAC signature validation, event type mapping, and customer data extraction.
"""

import base64
import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest
from plugins.sources.base import InvalidDataError
from plugins.sources.zendesk import ZendeskSourcePlugin


class TestZendeskSourcePlugin:
    """Tests for ZendeskSourcePlugin."""

    @pytest.fixture
    def plugin(self) -> ZendeskSourcePlugin:
        """Create a plugin instance with test secret."""
        return ZendeskSourcePlugin(
            webhook_secret="test_secret_key_123",
            zendesk_subdomain="testcompany",
        )

    @pytest.fixture
    def sample_ticket_data(self) -> dict:
        """Sample Zendesk ticket webhook payload."""
        return {
            "ticket": {
                "id": 12345,
                "subject": "Cannot login to my account",
                "description": "Trying to login but it says invalid password.",
                "status": "open",
                "priority": "high",
                "created_at": "2024-01-15T10:30:00Z",
                "requester": {
                    "id": 9876,
                    "name": "John Doe",
                    "email": "john@example.com",
                },
                "assignee": {
                    "id": 5555,
                    "name": "Support Agent",
                    "email": "agent@company.com",
                },
                "tags": ["login", "account", "urgent"],
                "organization": {
                    "id": 1111,
                    "name": "Acme Corp",
                },
                "via": {"channel": "email"},
            },
            "event": {"type": "ticket.created"},
        }

    def _create_signed_request(
        self, plugin: ZendeskSourcePlugin, body: dict, timestamp: str = "1234567890"
    ) -> Mock:
        """Create a mock request with valid HMAC signature."""
        body_str = json.dumps(body)
        message = timestamp + body_str
        digest = hmac.new(
            plugin.webhook_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("utf-8")

        request = Mock()
        request.headers = {
            "X-Zendesk-Webhook-Signature": signature,
            "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
        }
        request.body = body_str.encode("utf-8")
        request.content_type = "application/json"
        request.data = body_str.encode("utf-8")
        return request

    # === Initialization Tests ===

    def test_init_with_defaults(self):
        """Test plugin initialization with default values."""
        plugin = ZendeskSourcePlugin()
        assert plugin.webhook_secret == ""
        assert plugin.zendesk_subdomain == ""
        assert plugin._current_webhook_data is None

    def test_init_with_values(self, plugin: ZendeskSourcePlugin):
        """Test plugin initialization with provided values."""
        assert plugin.webhook_secret == "test_secret_key_123"
        assert plugin.zendesk_subdomain == "testcompany"

    # === Signature Validation Tests ===

    def test_validate_webhook_valid_signature(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test webhook validation with valid HMAC signature."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        assert plugin.validate_webhook(request) is True

    def test_validate_webhook_invalid_signature(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test webhook validation with invalid signature."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        request.headers["X-Zendesk-Webhook-Signature"] = "invalid_signature"
        assert plugin.validate_webhook(request) is False

    def test_validate_webhook_missing_signature(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test webhook validation with missing signature header."""
        request = Mock()
        request.headers = {"X-Zendesk-Webhook-Signature-Timestamp": "1234567890"}
        request.body = json.dumps(sample_ticket_data).encode("utf-8")
        assert plugin.validate_webhook(request) is False

    def test_validate_webhook_missing_timestamp(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test webhook validation with missing timestamp header."""
        request = Mock()
        request.headers = {"X-Zendesk-Webhook-Signature": "some_signature"}
        request.body = json.dumps(sample_ticket_data).encode("utf-8")
        assert plugin.validate_webhook(request) is False

    def test_validate_webhook_no_secret_configured(self, sample_ticket_data: dict):
        """Test webhook validation when no secret is configured."""
        plugin = ZendeskSourcePlugin(webhook_secret="")
        request = Mock()
        request.headers = {
            "X-Zendesk-Webhook-Signature": "some_signature",
            "X-Zendesk-Webhook-Signature-Timestamp": "1234567890",
        }
        request.body = json.dumps(sample_ticket_data).encode("utf-8")
        assert plugin.validate_webhook(request) is False

    # === Event Type Mapping Tests ===

    def test_event_type_mapping_ticket_created(self):
        """Test ticket.created event type mapping."""
        assert (
            ZendeskSourcePlugin.EVENT_TYPE_MAPPING["ticket.created"]
            == "support_ticket_created"
        )

    def test_event_type_mapping_ticket_solved(self):
        """Test ticket.solved event type mapping."""
        assert (
            ZendeskSourcePlugin.EVENT_TYPE_MAPPING["ticket.solved"]
            == "support_ticket_resolved"
        )

    def test_event_type_mapping_ticket_assigned(self):
        """Test ticket.assigned event type mapping."""
        assert (
            ZendeskSourcePlugin.EVENT_TYPE_MAPPING["ticket.assigned"]
            == "support_ticket_assigned"
        )

    def test_event_type_mapping_ticket_comment_added(self):
        """Test ticket.comment_added event type mapping."""
        assert (
            ZendeskSourcePlugin.EVENT_TYPE_MAPPING["ticket.comment_added"]
            == "support_ticket_comment"
        )

    # === Webhook Parsing Tests ===

    def test_parse_webhook_ticket_created(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test parsing a ticket.created webhook."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        result = plugin.parse_webhook(request)

        assert result is not None
        assert result["type"] == "support_ticket_created"
        assert result["provider"] == "zendesk"
        assert result["external_id"] == "12345"
        assert result["metadata"]["subject"] == "Cannot login to my account"
        assert result["metadata"]["ticket_status"] == "open"
        assert result["metadata"]["priority"] == "high"
        assert result["metadata"]["zendesk_subdomain"] == "testcompany"

    def test_parse_webhook_extracts_requester(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test that requester info is extracted correctly."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        result = plugin.parse_webhook(request)

        assert result["metadata"]["requester"]["id"] == "9876"
        assert result["metadata"]["requester"]["name"] == "John Doe"
        assert result["metadata"]["requester"]["email"] == "john@example.com"

    def test_parse_webhook_extracts_assignee(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test that assignee info is extracted correctly."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        result = plugin.parse_webhook(request)

        assert result["metadata"]["assignee"]["id"] == "5555"
        assert result["metadata"]["assignee"]["name"] == "Support Agent"

    def test_parse_webhook_extracts_tags(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test that tags are extracted correctly."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        result = plugin.parse_webhook(request)

        assert result["metadata"]["tags"] == ["login", "account", "urgent"]

    def test_parse_webhook_test_webhook_returns_none(self, plugin: ZendeskSourcePlugin):
        """Test that test webhooks return None."""
        test_data = {"event": {"type": "test"}, "test": True}
        request = self._create_signed_request(plugin, test_data)
        result = plugin.parse_webhook(request)
        assert result is None

    def test_parse_webhook_invalid_content_type(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test parsing fails with invalid content type."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        request.content_type = "text/plain"

        with pytest.raises(InvalidDataError, match="Invalid content type"):
            plugin.parse_webhook(request)

    def test_parse_webhook_invalid_json(self, plugin: ZendeskSourcePlugin):
        """Test parsing fails with invalid JSON."""
        request = Mock()
        request.content_type = "application/json"
        request.data = b"not valid json"
        request.body = b"not valid json"

        with pytest.raises(InvalidDataError, match="Invalid JSON data"):
            plugin.parse_webhook(request)

    def test_parse_webhook_empty_body(self, plugin: ZendeskSourcePlugin):
        """Test parsing fails with empty body."""
        request = Mock()
        request.content_type = "application/json"
        request.data = b"{}"
        request.body = b"{}"

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            plugin.parse_webhook(request)

    def test_parse_webhook_missing_ticket_id(self, plugin: ZendeskSourcePlugin):
        """Test parsing fails when ticket ID is missing."""
        data = {"event": {"type": "ticket.created"}, "ticket": {"subject": "Test"}}
        request = self._create_signed_request(plugin, data)

        with pytest.raises(InvalidDataError, match="Missing ticket ID"):
            plugin.parse_webhook(request)

    # === Status and Priority Mapping Tests ===

    def test_status_mapping(self, plugin: ZendeskSourcePlugin):
        """Test ticket status mapping."""
        assert ZendeskSourcePlugin.STATUS_MAPPING["new"] == "new"
        assert ZendeskSourcePlugin.STATUS_MAPPING["open"] == "open"
        assert ZendeskSourcePlugin.STATUS_MAPPING["pending"] == "pending"
        assert ZendeskSourcePlugin.STATUS_MAPPING["hold"] == "on_hold"
        assert ZendeskSourcePlugin.STATUS_MAPPING["solved"] == "solved"
        assert ZendeskSourcePlugin.STATUS_MAPPING["closed"] == "closed"

    def test_priority_mapping(self, plugin: ZendeskSourcePlugin):
        """Test ticket priority mapping."""
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["low"] == "low"
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["normal"] == "normal"
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["high"] == "high"
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["urgent"] == "urgent"

    # === Customer Data Tests ===

    def test_get_customer_data(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test customer data extraction."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        plugin.parse_webhook(request)  # This stores the webhook data

        customer_data = plugin.get_customer_data("9876")

        assert customer_data["email"] == "john@example.com"
        assert customer_data["first_name"] == "John"
        assert customer_data["last_name"] == "Doe"
        assert customer_data["company"] == "Acme Corp"

    def test_get_customer_data_no_organization(self, plugin: ZendeskSourcePlugin):
        """Test customer data when no organization is present."""
        data = {
            "ticket": {
                "id": 12345,
                "requester": {
                    "id": 9876,
                    "name": "Jane Smith",
                    "email": "jane@test.com",
                },
            },
            "event": {"type": "ticket.created"},
        }
        request = self._create_signed_request(plugin, data)
        plugin.parse_webhook(request)

        customer_data = plugin.get_customer_data("9876")
        assert customer_data["company"] == "Individual"

    def test_get_customer_data_no_webhook_data(self, plugin: ZendeskSourcePlugin):
        """Test that get_customer_data raises when no webhook data available."""
        from plugins.sources.base import CustomerNotFoundError

        with pytest.raises(CustomerNotFoundError):
            plugin.get_customer_data("unknown")

    # === Event Type Detection Tests ===

    def test_extract_event_type_from_event_field(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test event type extraction from event field."""
        event_type = plugin._extract_event_type(sample_ticket_data)
        assert event_type == "ticket.created"

    def test_extract_event_type_infers_solved(self, plugin: ZendeskSourcePlugin):
        """Test event type inference for solved tickets."""
        data = {"ticket": {"id": 123, "status": "solved"}}
        event_type = plugin._extract_event_type(data)
        assert event_type == "ticket.solved"

    def test_extract_event_type_infers_comment(self, plugin: ZendeskSourcePlugin):
        """Test event type inference for comment events."""
        data = {"ticket": {"id": 123, "latest_comment": {"body": "New comment"}}}
        event_type = plugin._extract_event_type(data)
        assert event_type == "ticket.comment_added"

    def test_extract_event_type_defaults_to_updated(self, plugin: ZendeskSourcePlugin):
        """Test event type defaults to updated for unknown events."""
        data = {"ticket": {"id": 123, "status": "open"}}
        event_type = plugin._extract_event_type(data)
        assert event_type == "ticket.updated"

    # === Sentiment Text Tests ===

    def test_sentiment_text_for_created_event(
        self, plugin: ZendeskSourcePlugin, sample_ticket_data: dict
    ):
        """Test sentiment_text is set correctly for created events."""
        request = self._create_signed_request(plugin, sample_ticket_data)
        result = plugin.parse_webhook(request)

        assert "sentiment_text" in result["metadata"]
        assert "Cannot login" in result["metadata"]["sentiment_text"]
        assert "invalid password" in result["metadata"]["sentiment_text"]

    def test_sentiment_text_for_comment_event(self, plugin: ZendeskSourcePlugin):
        """Test sentiment_text includes comment for comment events."""
        data = {
            "ticket": {
                "id": 12345,
                "subject": "Help needed",
                "description": "Original description",
                "latest_comment": {"body": "This is frustrating!"},
            },
            "event": {"type": "ticket.comment_added"},
        }
        request = self._create_signed_request(plugin, data)
        result = plugin.parse_webhook(request)

        assert "This is frustrating!" in result["metadata"]["sentiment_text"]


class TestZendeskEventBasedWebhooks:
    """Tests for Zendesk event-based webhook format.

    Event-based webhooks use 'zen:event-type:ticket.*' format and
    put ticket data in the 'detail' object.
    """

    @pytest.fixture
    def plugin(self) -> ZendeskSourcePlugin:
        """Create a plugin instance with test secret."""
        return ZendeskSourcePlugin(
            webhook_secret="test_secret_key_123",
            zendesk_subdomain="testcompany",
        )

    def _create_signed_request(self, plugin: ZendeskSourcePlugin, data: dict) -> Mock:
        """Create a mock request with valid signature."""
        timestamp = "1705320600"
        body = json.dumps(data)
        message = timestamp + body
        digest = hmac.new(
            plugin.webhook_secret.encode(), message.encode(), hashlib.sha256
        ).digest()
        signature = base64.b64encode(digest).decode()

        request = Mock()
        request.headers = {
            "X-Zendesk-Webhook-Signature": signature,
            "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
        }
        request.body = body.encode()
        request.content_type = "application/json"
        request.data = body.encode()
        return request

    def test_event_based_webhook_format(self, plugin: ZendeskSourcePlugin):
        """Test parsing event-based webhook with zen:event-type prefix."""
        data = {
            "type": "zen:event-type:ticket.created",
            "detail": {
                "id": 12345,
                "subject": "Help needed",
                "description": "I need assistance.",
                "status": "NEW",
                "priority": "HIGH",
                "requester": {
                    "id": 9876,
                    "name": "John Doe",
                    "email": "john@example.com",
                },
            },
            "time": "2024-01-15T10:30:00Z",
        }
        request = self._create_signed_request(plugin, data)
        result = plugin.parse_webhook(request)

        assert result["type"] == "support_ticket_created"
        assert result["external_id"] == "12345"
        assert result["metadata"]["subject"] == "Help needed"

    def test_event_based_webhook_status_changed(self, plugin: ZendeskSourcePlugin):
        """Test parsing event-based status change webhook."""
        data = {
            "type": "zen:event-type:ticket.status_changed",
            "detail": {
                "id": 12345,
                "subject": "Existing ticket",
                "status": "SOLVED",
                "priority": "NORMAL",
            },
            "event": {
                "previous": "OPEN",
                "current": "SOLVED",
            },
        }
        request = self._create_signed_request(plugin, data)
        result = plugin.parse_webhook(request)

        assert result["type"] == "support_ticket_status_changed"
        assert result["metadata"]["ticket_status"] == "solved"

    def test_event_based_webhook_agent_assignment(self, plugin: ZendeskSourcePlugin):
        """Test parsing agent assignment webhook."""
        data = {
            "type": "zen:event-type:ticket.agent_assignment_changed",
            "detail": {
                "id": 12345,
                "subject": "Ticket being reassigned",
                "status": "OPEN",
                "assignee": {
                    "id": 5555,
                    "name": "New Agent",
                },
            },
        }
        request = self._create_signed_request(plugin, data)
        result = plugin.parse_webhook(request)

        assert result["type"] == "support_ticket_assigned"
        assert result["metadata"]["assignee"]["name"] == "New Agent"

    def test_uppercase_status_mapping(self, plugin: ZendeskSourcePlugin):
        """Test that uppercase status values are mapped correctly."""
        assert ZendeskSourcePlugin.STATUS_MAPPING["NEW"] == "new"
        assert ZendeskSourcePlugin.STATUS_MAPPING["OPEN"] == "open"
        assert ZendeskSourcePlugin.STATUS_MAPPING["PENDING"] == "pending"
        assert ZendeskSourcePlugin.STATUS_MAPPING["HOLD"] == "on_hold"
        assert ZendeskSourcePlugin.STATUS_MAPPING["SOLVED"] == "solved"
        assert ZendeskSourcePlugin.STATUS_MAPPING["CLOSED"] == "closed"

    def test_uppercase_priority_mapping(self, plugin: ZendeskSourcePlugin):
        """Test that uppercase priority values are mapped correctly."""
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["LOW"] == "low"
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["NORMAL"] == "normal"
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["HIGH"] == "high"
        assert ZendeskSourcePlugin.PRIORITY_MAPPING["URGENT"] == "urgent"

    def test_event_based_comment_added(self, plugin: ZendeskSourcePlugin):
        """Test parsing comment added event."""
        data = {
            "type": "zen:event-type:ticket.comment_added",
            "detail": {
                "id": 12345,
                "subject": "Ongoing conversation",
                "status": "OPEN",
                "latest_comment": {
                    "body": "Thanks for the update!",
                    "is_public": True,
                },
            },
        }
        request = self._create_signed_request(plugin, data)
        result = plugin.parse_webhook(request)

        assert result["type"] == "support_ticket_comment"
        assert result["metadata"]["latest_comment"] == "Thanks for the update!"

    def test_event_type_mapping_includes_zen_prefix(self):
        """Test that event type mapping includes zen:event-type prefixed events."""
        mapping = ZendeskSourcePlugin.EVENT_TYPE_MAPPING

        # Verify both formats are mapped
        assert "ticket.created" in mapping
        assert "zen:event-type:ticket.created" in mapping

        # Verify they map to the same internal type
        assert mapping["ticket.created"] == mapping["zen:event-type:ticket.created"]


class TestZendeskPluginMetadata:
    """Tests for plugin metadata."""

    def test_get_metadata(self):
        """Test plugin metadata."""
        metadata = ZendeskSourcePlugin.get_metadata()

        assert metadata.name == "zendesk"
        assert metadata.display_name == "Zendesk"
        assert metadata.version == "1.0.0"
        assert "webhook" in metadata.description.lower()

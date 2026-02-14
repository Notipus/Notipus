"""Zendesk source plugin implementation.

This module implements the BaseSourcePlugin interface for Zendesk,
handling webhook validation, parsing, and customer data retrieval
using HMAC-SHA256 signature verification.
"""

import base64
import hashlib
import hmac
import json
import logging
from typing import Any, ClassVar

from django.http import HttpRequest
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.sources.base import (
    BaseSourcePlugin,
    CustomerNotFoundError,
    InvalidDataError,
)

logger = logging.getLogger(__name__)


class ZendeskSourcePlugin(BaseSourcePlugin):
    """Handle Zendesk webhooks and customer data.

    This plugin validates webhook signatures using HMAC-SHA256
    verification as documented in Zendesk's webhook documentation.

    The signature is computed as: base64(HMACSHA256(timestamp + body))
    using the X-Zendesk-Webhook-Signature and X-Zendesk-Webhook-Signature-Timestamp
    headers for verification.

    Attributes:
        EVENT_TYPE_MAPPING: Maps Zendesk event types to internal event types.
    """

    # Event type mapping supports both trigger-based (ticket.*) and
    # event-based (zen:event-type:ticket.*) webhook formats
    EVENT_TYPE_MAPPING: ClassVar[dict[str, str]] = {
        # Trigger/automation format (ticket.*)
        "ticket.created": "support_ticket_created",
        "ticket.updated": "support_ticket_updated",
        "ticket.status_changed": "support_ticket_status_changed",
        "ticket.comment_added": "support_ticket_comment",
        "ticket.solved": "support_ticket_resolved",
        "ticket.assigned": "support_ticket_assigned",
        "ticket.reopened": "support_ticket_reopened",
        "ticket.priority_changed": "support_ticket_priority_changed",
        "ticket.merged": "support_ticket_updated",
        "ticket.tags_changed": "support_ticket_updated",
        # Event-based webhook format (zen:event-type:ticket.*)
        "zen:event-type:ticket.created": "support_ticket_created",
        "zen:event-type:ticket.status_changed": "support_ticket_status_changed",
        "zen:event-type:ticket.comment_added": "support_ticket_comment",
        "zen:event-type:ticket.agent_assignment_changed": "support_ticket_assigned",
        "zen:event-type:ticket.priority_changed": "support_ticket_priority_changed",
        "zen:event-type:ticket.tags_changed": "support_ticket_updated",
        "zen:event-type:ticket.custom_field_changed": "support_ticket_updated",
        "zen:event-type:ticket.merged": "support_ticket_updated",
        "zen:event-type:ticket.csat_received": "support_ticket_updated",
        # Test events
        "test": "test",
    }

    # Status mapping for Zendesk ticket statuses (handles both lowercase
    # from triggers and uppercase from event-based webhooks)
    STATUS_MAPPING: ClassVar[dict[str, str]] = {
        "new": "new",
        "NEW": "new",
        "open": "open",
        "OPEN": "open",
        "pending": "pending",
        "PENDING": "pending",
        "hold": "on_hold",
        "HOLD": "on_hold",
        "on-hold": "on_hold",
        "ON-HOLD": "on_hold",
        "solved": "solved",
        "SOLVED": "solved",
        "closed": "closed",
        "CLOSED": "closed",
    }

    # Priority mapping (handles both cases)
    PRIORITY_MAPPING: ClassVar[dict[str, str]] = {
        "low": "low",
        "LOW": "low",
        "normal": "normal",
        "NORMAL": "normal",
        "high": "high",
        "HIGH": "high",
        "urgent": "urgent",
        "URGENT": "urgent",
    }

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Zendesk source plugin.
        """
        return PluginMetadata(
            name="zendesk",
            display_name="Zendesk",
            version="1.0.0",
            description="Zendesk webhook handler for support tickets",
            plugin_type=PluginType.SOURCE,
            capabilities={
                PluginCapability.WEBHOOK_VALIDATION,
                PluginCapability.CUSTOMER_DATA,
            },
            priority=100,
        )

    def __init__(
        self,
        webhook_secret: str = "",
        zendesk_subdomain: str = "",
    ) -> None:
        """Initialize plugin with webhook secret and subdomain.

        Args:
            webhook_secret: Secret key for webhook signature validation.
            zendesk_subdomain: Zendesk subdomain for building ticket URLs.
        """
        super().__init__(webhook_secret)
        self.zendesk_subdomain = zendesk_subdomain
        self._current_webhook_data: dict[str, Any] | None = None

    def validate_webhook(self, request: HttpRequest) -> bool:
        """Validate the webhook signature using HMAC verification.

        Zendesk webhooks use HMAC-SHA256 for signature verification.
        The signature is computed as: base64(HMACSHA256(timestamp + body))

        Args:
            request: The incoming HTTP request.

        Returns:
            True if signature is valid, False otherwise.
        """
        signature = request.headers.get("X-Zendesk-Webhook-Signature")
        timestamp = request.headers.get("X-Zendesk-Webhook-Signature-Timestamp")

        if not signature or not timestamp:
            logger.warning("Missing Zendesk webhook signature or timestamp headers")
            return False

        if not self.webhook_secret:
            logger.error("Zendesk webhook secret is not configured")
            return False

        # Zendesk signature formula: base64(HMACSHA256(timestamp + body))
        try:
            body = request.body
            if isinstance(body, bytes):
                body_str = body.decode("utf-8")
            else:
                body_str = str(body)

            message = timestamp + body_str
            secret = (
                self.webhook_secret.encode("utf-8")
                if isinstance(self.webhook_secret, str)
                else self.webhook_secret
            )

            digest = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
            calculated_signature = base64.b64encode(digest).decode("utf-8")

            return hmac.compare_digest(signature, calculated_signature)
        except Exception as e:
            logger.error(f"Error validating Zendesk webhook signature: {e}")
            return False

    def _validate_zendesk_request(self, request: HttpRequest) -> str:
        """Validate Zendesk webhook request and extract event type.

        Args:
            request: The incoming HTTP request.

        Returns:
            The event type string.

        Raises:
            InvalidDataError: If content type or event type is invalid.
        """
        if request.content_type != "application/json":
            raise InvalidDataError("Invalid content type")

        return "ticket.created"  # Default, will be overridden by actual event data

    def _parse_zendesk_json(self, request: HttpRequest) -> dict[str, Any]:
        """Parse and validate Zendesk JSON data.

        Args:
            request: The incoming HTTP request.

        Returns:
            Parsed JSON data dictionary.

        Raises:
            InvalidDataError: If JSON is invalid or empty.
        """
        try:
            body = getattr(request, "data", None) or request.body
            data = json.loads(body) if isinstance(body, (str, bytes)) else body
        except (json.JSONDecodeError, AttributeError) as e:
            raise InvalidDataError("Invalid JSON data") from e

        if not isinstance(data, dict):
            raise InvalidDataError("Invalid JSON data")
        if not data:
            raise InvalidDataError("Missing required fields")

        return data

    def _extract_event_type(self, data: dict[str, Any]) -> str:
        """Extract event type from Zendesk webhook data.

        Zendesk webhooks can come in two formats:
        1. Event-based: Has top-level 'type' field like 'zen:event-type:ticket.created'
        2. Trigger-based: Custom JSON with ticket data, may have 'event' field

        Args:
            data: Parsed webhook data.

        Returns:
            Event type string.
        """
        # Event-based webhook format (has top-level 'type' field)
        # Example: {"type": "zen:event-type:ticket.created", "detail": {...}}
        if "type" in data and isinstance(data["type"], str):
            event_type = data["type"]
            if event_type.startswith("zen:event-type:"):
                return event_type

        # Zendesk event subscription format with 'event' object
        if "event" in data:
            event_info = data.get("event", {})
            if isinstance(event_info, dict):
                return str(event_info.get("type", "ticket.updated"))
            return str(event_info)

        # Trigger/automation format - infer from ticket status changes
        ticket = self._get_ticket_data(data)
        status = ticket.get("status", "").lower()
        if status == "solved":
            return "ticket.solved"
        elif "comment" in data or "latest_comment" in ticket:
            return "ticket.comment_added"

        return "ticket.updated"

    def _is_test_webhook(self, data: dict[str, Any]) -> bool:
        """Check if this is a test webhook.

        Args:
            data: Parsed webhook data.

        Returns:
            True if this is a test webhook, False otherwise.
        """
        event_type = self._extract_event_type(data)
        return event_type == "test" or data.get("test", False)

    def _extract_ticket_id(self, data: dict[str, Any]) -> str:
        """Extract ticket ID from Zendesk webhook data.

        Handles both trigger-based (ticket.id) and event-based (detail.id) formats.

        Args:
            data: Parsed webhook data.

        Returns:
            Ticket ID string.

        Raises:
            InvalidDataError: If ticket ID cannot be extracted.
        """
        ticket = self._get_ticket_data(data)
        ticket_id = ticket.get("id")

        if not ticket_id:
            raise InvalidDataError("Missing ticket ID")

        return str(ticket_id)

    def _get_ticket_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Get ticket data from webhook payload.

        Handles both trigger-based (data.ticket) and event-based (data.detail) formats.

        Args:
            data: Parsed webhook data.

        Returns:
            Ticket data dictionary.
        """
        # Event-based webhook format uses 'detail'
        if "detail" in data and isinstance(data["detail"], dict):
            return data["detail"]
        # Trigger-based webhook format uses 'ticket'
        if "ticket" in data and isinstance(data["ticket"], dict):
            return data["ticket"]
        # Fallback to data itself
        return data

    def _extract_requester_info(self, data: dict[str, Any]) -> dict[str, Any]:
        """Extract requester (customer) information from webhook data.

        Args:
            data: Parsed webhook data.

        Returns:
            Dictionary with requester information.
        """
        ticket = self._get_ticket_data(data)
        requester = ticket.get("requester", {})

        # Handle both object and ID-only formats
        if isinstance(requester, dict):
            return {
                "id": str(requester.get("id", "")),
                "name": requester.get("name", ""),
                "email": requester.get("email", ""),
            }

        # Requester might just be an ID
        return {
            "id": str(requester) if requester else "",
            "name": "",
            "email": ticket.get("requester_email", ""),
        }

    def _extract_assignee_info(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Extract assignee information from webhook data.

        Args:
            data: Parsed webhook data.

        Returns:
            Dictionary with assignee information, or None if not assigned.
        """
        ticket = self._get_ticket_data(data)
        assignee = ticket.get("assignee", {})

        if not assignee:
            return None

        if isinstance(assignee, dict):
            return {
                "id": str(assignee.get("id", "")),
                "name": assignee.get("name", ""),
                "email": assignee.get("email", ""),
            }

        return {"id": str(assignee), "name": "", "email": ""}

    def _build_ticket_event_data(
        self,
        event_type: str,
        ticket_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Build ticket event data structure.

        Args:
            event_type: The internal event type.
            ticket_id: Ticket identifier.
            data: Raw webhook data.

        Returns:
            Standardized event data dictionary.
        """
        ticket = self._get_ticket_data(data)
        requester = self._extract_requester_info(data)
        assignee = self._extract_assignee_info(data)

        # Extract ticket content
        subject = ticket.get("subject", "")
        description = ticket.get("description", "")

        # For comment events, get the latest comment
        latest_comment = ticket.get("latest_comment", {})
        if isinstance(latest_comment, dict):
            comment_body = latest_comment.get("body", "")
        else:
            comment_body = ""

        # Extract status and priority
        status = ticket.get("status", "open")
        priority = ticket.get("priority", "normal")

        # Map to standardized values
        mapped_status = self.STATUS_MAPPING.get(status, status)
        mapped_priority = self.PRIORITY_MAPPING.get(priority, priority)

        # Extract tags
        tags = ticket.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        event_data: dict[str, Any] = {
            "type": event_type,
            "customer_id": requester.get("id") or requester.get("email", "unknown"),
            "provider": "zendesk",
            "external_id": ticket_id,
            "created_at": ticket.get("created_at") or data.get("time"),
            "status": "success",
            "metadata": {
                "ticket_id": ticket_id,
                "subject": subject,
                "description": description,
                "ticket_status": mapped_status,
                "priority": mapped_priority,
                "tags": tags,
                "requester": requester,
                "assignee": assignee,
                "channel": ticket.get("channel", ticket.get("via", {}).get("channel")),
                "group_id": ticket.get("group_id"),
                "organization_id": ticket.get("organization_id"),
                "brand_id": ticket.get("brand_id"),
                "custom_fields": ticket.get("custom_fields", []),
                # For comment events
                "latest_comment": comment_body
                if event_type == "support_ticket_comment"
                else None,
                "comment_is_public": latest_comment.get("is_public", True)
                if latest_comment
                else None,
                # Zendesk subdomain for building ticket URLs
                "zendesk_subdomain": self.zendesk_subdomain,
            },
        }

        # Add sentiment analysis text (subject + description for analysis)
        event_data["metadata"]["sentiment_text"] = f"{subject}\n\n{description}"
        if comment_body and event_type == "support_ticket_comment":
            event_data["metadata"]["sentiment_text"] = f"{subject}\n\n{comment_body}"

        return event_data

    def parse_webhook(
        self, request: HttpRequest, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Parse Zendesk webhook data.

        Args:
            request: The incoming HTTP request.
            **kwargs: Additional arguments (unused).

        Returns:
            Parsed event data dictionary, or None for test webhooks.

        Raises:
            InvalidDataError: If webhook data is invalid.
        """
        # Validate request
        self._validate_zendesk_request(request)

        # Parse JSON data
        data = self._parse_zendesk_json(request)

        # Store webhook data for later use
        self._current_webhook_data = data

        # Check for test webhook
        if self._is_test_webhook(data):
            return None

        # Extract event type from data
        zendesk_event_type = self._extract_event_type(data)

        # Map to internal event type
        event_type = self.EVENT_TYPE_MAPPING.get(zendesk_event_type)
        if not event_type:
            # Default to updated for unknown events
            event_type = "support_ticket_updated"
            logger.info(
                f"Unknown Zendesk event type: {zendesk_event_type}, "
                f"defaulting to {event_type}"
            )

        # Extract ticket ID
        ticket_id = self._extract_ticket_id(data)

        # Build and return event data
        return self._build_ticket_event_data(event_type, ticket_id, data)

    def get_customer_data(self, customer_id: str) -> dict[str, Any]:
        """Get customer data from stored webhook data.

        Args:
            customer_id: The customer identifier.

        Returns:
            Dictionary of customer information.

        Raises:
            CustomerNotFoundError: If no webhook data is available.
        """
        if not self._current_webhook_data:
            raise CustomerNotFoundError("No webhook data available")

        data = self._current_webhook_data
        requester = self._extract_requester_info(data)
        ticket = self._get_ticket_data(data)

        # Try to get organization info
        organization = ticket.get("organization", {})
        company_name = ""
        if isinstance(organization, dict):
            company_name = organization.get("name", "")

        return {
            "company": company_name or "Individual",
            "email": requester.get("email", ""),
            "first_name": requester.get("name", "").split()[0]
            if requester.get("name")
            else "",
            "last_name": " ".join(requester.get("name", "").split()[1:])
            if requester.get("name")
            else "",
            "customer_id": customer_id,
            "metadata": {
                "zendesk_user_id": requester.get("id"),
                "organization_id": ticket.get("organization_id"),
                "tags": ticket.get("tags", []),
            },
        }

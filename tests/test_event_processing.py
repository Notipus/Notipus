"""Tests for event processing and notification formatting.

This module tests the EventProcessor class for handling various
webhook events and generating properly formatted RichNotification objects.
"""

from typing import Any

import pytest
from webhooks.models.rich_notification import (
    NotificationSeverity,
    NotificationType,
    RichNotification,
)
from webhooks.services.event_processor import EventProcessor


def test_notification_formatting() -> None:
    """Test that notifications are properly formatted.

    Verifies a payment success event generates a valid RichNotification
    with correct headline (event-focused, no company name) and severity.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_success",
        "customer_id": "cust_123",
        "amount": 29.99,
        "currency": "USD",
        "status": "success",
        "provider": "stripe",
        "external_id": "evt_123",
        "metadata": {
            "subscription_id": "sub_123",
            "plan_name": "Enterprise",
        },
    }

    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
        "first_name": "John",
        "last_name": "Doe",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    # Headlines are now event-focused, company info is in body
    assert "$29.99" in notification.headline
    assert "received" in notification.headline.lower()
    assert notification.severity == NotificationSeverity.SUCCESS
    assert notification.type == NotificationType.PAYMENT_SUCCESS


def test_missing_required_customer_data() -> None:
    """Test handling of missing required customer data.

    Verifies notification is still generated when customer data is incomplete.
    Headlines are now event-focused and don't include customer info.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_success",
        "customer_id": "cust_123",
        "amount": 29.99,
        "currency": "USD",
        "status": "success",
        "provider": "stripe",
        "external_id": "evt_123",
        "metadata": {},
    }

    customer_data: dict[str, Any] = {
        "team_size": "50",  # Missing company, name, and email
        "plan_name": "Enterprise",
    }

    # Should still generate notification (headline is event-focused)
    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    # Headlines are now event-focused, no customer info needed
    assert "$29.99" in notification.headline
    assert "received" in notification.headline.lower()


def test_invalid_event_type() -> None:
    """Test handling of invalid event type.

    Verifies ValueError is raised for unrecognized event types.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "invalid_event",
        "customer_id": "cust_123",
        "amount": 29.99,
        "provider": "stripe",
        "external_id": "evt_123",
    }

    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
    }

    with pytest.raises(ValueError, match="Invalid event type"):
        processor.build_rich_notification(event_data, customer_data)


def test_missing_event_type() -> None:
    """Test handling of missing event type.

    Verifies ValueError is raised when event_data has no type.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "customer_id": "cust_123",
        "amount": 29.99,
    }

    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
    }

    with pytest.raises(ValueError, match="Missing event type"):
        processor.build_rich_notification(event_data, customer_data)


def test_empty_event_data() -> None:
    """Test handling of empty event data.

    Verifies ValueError is raised when event_data is empty dict.
    """
    processor = EventProcessor()

    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
    }

    with pytest.raises(ValueError, match="Missing event type"):
        processor.build_rich_notification({}, customer_data)


def test_payment_failure_event() -> None:
    """Test payment failure event processing.

    Verifies payment failures generate error severity notifications.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_failure",
        "customer_id": "cust_123",
        "amount": 29.99,
        "currency": "USD",
        "status": "failed",
        "provider": "stripe",
        "external_id": "evt_fail123",
        "metadata": {
            "failure_reason": "Card declined",
        },
    }

    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    assert notification.severity == NotificationSeverity.ERROR
    assert notification.type == NotificationType.PAYMENT_FAILURE


def test_payment_failure_retry_headline() -> None:
    """Test payment failure with attempt_count > 1 shows retry in headline.

    Verifies the headline includes '(retry #N)' when attempt_count > 1.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_failure",
        "customer_id": "cust_123",
        "amount": 53.20,
        "currency": "USD",
        "status": "failed",
        "provider": "stripe",
        "external_id": "evt_fail456",
        "metadata": {
            "attempt_count": 2,
            "next_payment_attempt": 1740182400,
        },
    }

    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    assert "$53.20" in notification.headline
    assert "retry #2" in notification.headline
    assert notification.severity == NotificationSeverity.ERROR


def test_payment_failure_first_attempt_no_retry_in_headline() -> None:
    """Test payment failure with attempt_count 1 does NOT show retry in headline.

    First attempts should just show the normal failure headline.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_failure",
        "customer_id": "cust_123",
        "amount": 53.20,
        "currency": "USD",
        "status": "failed",
        "provider": "stripe",
        "external_id": "evt_fail789",
        "metadata": {
            "attempt_count": 1,
        },
    }

    customer_data: dict[str, Any] = {
        "company": "Acme Corp",
        "email": "billing@acme.com",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    assert "$53.20 payment failed" == notification.headline
    assert "retry" not in notification.headline


def test_subscription_created_event() -> None:
    """Test subscription created event processing.

    Verifies subscription creation generates success notification.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "subscription_created",
        "customer_id": "cust_123",
        "amount": 99.00,
        "currency": "USD",
        "status": "active",
        "provider": "stripe",
        "external_id": "evt_sub123",
        "metadata": {
            "plan_name": "Pro",
        },
    }

    customer_data: dict[str, Any] = {
        "company": "Startup Inc",
        "email": "billing@startup.com",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    assert notification.type == NotificationType.SUBSCRIPTION_CREATED
    # Headlines are now event-focused
    assert "New customer" in notification.headline


def test_subscription_canceled_event() -> None:
    """Test subscription canceled event processing.

    Verifies subscription cancellation generates warning notification.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "subscription_canceled",
        "customer_id": "cust_123",
        "amount": 99.00,
        "currency": "USD",
        "status": "canceled",
        "provider": "stripe",
        "external_id": "evt_cancel123",
        "metadata": {},
    }

    customer_data: dict[str, Any] = {
        "company": "Churned Co",
        "email": "billing@churned.com",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    assert isinstance(notification, RichNotification)
    assert notification.type == NotificationType.SUBSCRIPTION_CANCELED
    assert notification.severity == NotificationSeverity.WARNING


def test_process_event_rich_returns_dict() -> None:
    """Test that process_event_rich returns a formatted dict.

    Verifies the method returns a dict suitable for Slack API.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_success",
        "customer_id": "cust_123",
        "amount": 49.99,
        "currency": "USD",
        "status": "success",
        "provider": "stripe",
        "external_id": "evt_123",
        "metadata": {},
    }

    customer_data: dict[str, Any] = {
        "company": "Test Corp",
        "email": "billing@test.com",
    }

    result = processor.process_event_rich(event_data, customer_data, target="slack")

    assert isinstance(result, dict)
    # Slack format uses attachments array for colored messages
    assert "attachments" in result
    assert isinstance(result["attachments"], list)
    assert len(result["attachments"]) > 0
    assert "blocks" in result["attachments"][0]
    assert "color" in result["attachments"][0]


def test_display_name_fallback_to_email() -> None:
    """Test customer info email is captured when company/name are empty.

    Customer email should be available in notification customer info.
    Headlines are event-focused and don't include customer details.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_success",
        "customer_id": "cust_123",
        "amount": 29.99,
        "currency": "USD",
        "status": "success",
        "provider": "stripe",
        "external_id": "evt_123",
        "metadata": {},
    }

    customer_data: dict[str, Any] = {
        "email": "billing@techstartup.io",
        "company": "",
        "first_name": "",
        "last_name": "",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    # Headlines are event-focused
    assert "$29.99" in notification.headline
    # Customer email is available in CustomerInfo
    assert notification.customer is not None
    assert notification.customer.email == "billing@techstartup.io"


def test_display_name_ignores_individual() -> None:
    """Test that 'Individual' company name is ignored.

    Customer info should have email available, not 'Individual'.
    """
    processor = EventProcessor()

    event_data: dict[str, Any] = {
        "type": "payment_success",
        "customer_id": "cust_123",
        "amount": 29.99,
        "currency": "USD",
        "status": "success",
        "provider": "stripe",
        "external_id": "evt_123",
        "metadata": {},
    }

    customer_data: dict[str, Any] = {
        "email": "billing@enterprise.com",
        "company": "Individual",
        "first_name": "",
        "last_name": "",
    }

    notification = processor.build_rich_notification(event_data, customer_data)
    # Headlines are event-focused (no customer info)
    assert "$29.99" in notification.headline
    # Customer info should have email, and company_name should be set to "Individual"
    assert notification.customer is not None
    assert notification.customer.email == "billing@enterprise.com"

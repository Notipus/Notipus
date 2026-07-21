"""Tests for pending event queue service.

This module tests the PendingEventQueue which implements delayed processing
of webhook events to allow related events to be aggregated before sending
a single notification.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest
from webhooks.services.pending_event_queue import PendingEventQueue


class TestPendingEventQueueStorage:
    """Test event storage functionality."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance for each test."""
        queue = PendingEventQueue()
        queue.DELAY_SECONDS = 0.1  # Short delay for tests
        return queue

    @pytest.fixture
    def mock_cache(self):
        """Mock Django cache."""
        cache_data: dict = {}

        def mock_get(key, default=None):
            return cache_data.get(key, default)

        def mock_set(key, value, timeout=None):
            cache_data[key] = value

        def mock_delete(key):
            cache_data.pop(key, None)

        with patch("webhooks.services.pending_event_queue.cache") as mock:
            mock.get = mock_get
            mock.set = mock_set
            mock.delete = mock_delete
            yield mock

    def test_store_event_creates_new_list(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that storing first event creates a new list."""
        event_data = {"type": "subscription_created", "customer_id": "cus_123"}
        customer_data = {"email": "test@example.com"}

        queue._store_event("idem_key", "ws_123", event_data, customer_data)

        key = "pending_webhook:ws_123:idem_key"
        stored = mock_cache.get(key)
        assert stored is not None
        assert len(stored) == 1
        # Check original fields are present (plus _queued_at timestamp)
        assert stored[0]["event_data"]["type"] == event_data["type"]
        assert stored[0]["event_data"]["customer_id"] == event_data["customer_id"]
        # Timestamp added for orphan recovery
        assert "_queued_at" in stored[0]["event_data"]
        assert stored[0]["customer_data"] == customer_data

    def test_store_event_appends_to_existing(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that storing second event appends to existing list."""
        event1 = {"type": "subscription_created", "customer_id": "cus_123"}
        event2 = {"type": "invoice_paid", "customer_id": "cus_123"}
        customer1 = {"email": ""}
        customer2 = {"email": "found@example.com"}

        queue._store_event("idem_key", "ws_123", event1, customer1)
        queue._store_event("idem_key", "ws_123", event2, customer2)

        key = "pending_webhook:ws_123:idem_key"
        stored = mock_cache.get(key)
        assert len(stored) == 2


class TestPendingEventQueueAggregation:
    """Test event aggregation logic."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance."""
        return PendingEventQueue()

    def test_aggregate_takes_email_from_invoice_event(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that email is extracted from invoice events."""
        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                },
                "customer_data": {"email": ""},
            },
            {
                "event_data": {
                    "type": "invoice_paid",
                    "customer_id": "cus_123",
                    "customer_email": "from_invoice@example.com",
                },
                "customer_data": {"email": "from_invoice@example.com"},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert customer["email"] == "from_invoice@example.com"

    def test_aggregate_prefers_subscription_type(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that subscription_created type is preferred over invoice."""
        stored_items = [
            {
                "event_data": {"type": "invoice_paid", "customer_id": "cus_123"},
                "customer_data": {"email": "test@example.com"},
            },
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                },
                "customer_data": {"email": ""},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "subscription_created"

    def test_aggregate_prefers_trial_started_type(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that trial_started type is preferred over subscription_created."""
        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                },
                "customer_data": {"email": ""},
            },
            {
                "event_data": {"type": "trial_started", "customer_id": "cus_123"},
                "customer_data": {"email": ""},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "trial_started"

    def test_aggregate_empty_list_returns_empty_dicts(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that empty list returns empty dicts."""
        event, customer = queue._aggregate_events([])
        assert event == {}
        assert customer == {}

    def test_aggregate_single_event_returns_as_is(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that single event is returned as-is."""
        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                    "amount": 29.99,
                },
                "customer_data": {"email": "test@example.com", "first_name": "John"},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "subscription_created"
        assert event["amount"] == 29.99
        assert customer["email"] == "test@example.com"
        assert customer["first_name"] == "John"

    def test_aggregate_merges_customer_fields(self, queue: PendingEventQueue) -> None:
        """Test that customer fields are merged from multiple events."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {"email": "", "first_name": "John", "last_name": ""},
            },
            {
                "event_data": {"type": "invoice_paid"},
                "customer_data": {
                    "email": "john@example.com",
                    "first_name": "",
                    "last_name": "Doe",
                },
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert customer["email"] == "john@example.com"
        assert customer["first_name"] == "John"  # From first event
        assert customer["last_name"] == "Doe"  # From second event

    def test_aggregate_copies_full_winning_event(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that the winner's amount/external_id/currency are used.

        When a later event wins the priority contest, the ENTIRE event must
        be copied - not just type and metadata. Otherwise the notification
        reports the first event's amount ($29.99) while the customer
        actually paid the winner's amount ($500).
        """
        stored_items = [
            {
                "event_data": {
                    "type": "invoice_paid",
                    "customer_id": "cus_123",
                    "amount": 29.99,
                    "currency": "EUR",
                    "external_id": "in_first",
                },
                "customer_data": {"email": "test@example.com"},
            },
            {
                "event_data": {
                    "type": "payment_success",
                    "customer_id": "cus_123",
                    "amount": 500.00,
                    "currency": "USD",
                    "external_id": "in_second",
                },
                "customer_data": {"email": "test@example.com"},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "payment_success"
        assert event["amount"] == 500.00
        assert event["currency"] == "USD"
        assert event["external_id"] == "in_second"

    def test_aggregate_preserves_billing_reason_from_losing_invoice(
        self, queue: PendingEventQueue
    ) -> None:
        """Test the losing invoice's billing_reason survives aggregation.

        subscription.created wins the type contest over invoice.paid, but
        the invoice's billing_reason ("subscription_create") is Stripe's
        only truthful first-payment signal - it must be folded into the
        winner's metadata for InsightDetector.
        """
        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                    "amount": 99.00,
                    "metadata": {"plan_name": "Pro"},
                },
                "customer_data": {"email": ""},
            },
            {
                "event_data": {
                    "type": "payment_success",
                    "customer_id": "cus_123",
                    "amount": 99.00,
                    "metadata": {"billing_reason": "subscription_create"},
                },
                "customer_data": {"email": "test@example.com"},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "subscription_created"
        assert event["metadata"]["billing_reason"] == "subscription_create"
        assert event["metadata"]["plan_name"] == "Pro"

    def test_aggregate_keeps_winning_invoice_billing_reason(
        self, queue: PendingEventQueue
    ) -> None:
        """Test a winning invoice's own billing_reason is not overwritten."""
        stored_items = [
            {
                "event_data": {
                    "type": "payment_success",
                    "customer_id": "cus_123",
                    "amount": 49.00,
                    "metadata": {"billing_reason": "subscription_cycle"},
                },
                "customer_data": {"email": "test@example.com"},
            },
            {
                "event_data": {
                    "type": "invoice_paid",
                    "customer_id": "cus_123",
                    "amount": 49.00,
                    "metadata": {"billing_reason": "subscription_create"},
                },
                "customer_data": {"email": "test@example.com"},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "payment_success"
        assert event["metadata"]["billing_reason"] == "subscription_cycle"

    def test_aggregate_surfaces_payment_failure_with_subscription_created(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that a payment_failure in the bucket is never silently dropped.

        A subscription created with an immediately-declining card queues
        subscription.created + invoice.payment_failed under the same
        idempotency key. The aggregated notification keeps the subscription
        as the primary event but must surface the failure details.
        """
        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                    "amount": 49.00,
                    "external_id": "sub_abc",
                    "metadata": {"subscription_id": "sub_abc"},
                },
                "customer_data": {"email": ""},
            },
            {
                "event_data": {
                    "type": "payment_failure",
                    "customer_id": "cus_123",
                    "amount": 49.00,
                    "external_id": "in_fail",
                    "metadata": {
                        "failure_reason": "Your card was declined",
                        "decline_code": "insufficient_funds",
                        "attempt_count": 1,
                        "next_payment_attempt": 1740182400,
                    },
                },
                "customer_data": {"email": "test@example.com"},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        # Subscription is still the primary notification...
        assert event["type"] == "subscription_created"
        # ...but the failure is folded into its metadata, not dropped
        metadata = event["metadata"]
        assert metadata["has_payment_failure"] is True
        assert metadata["failure_reason"] == "Your card was declined"
        assert metadata["decline_code"] == "insufficient_funds"
        assert metadata["attempt_count"] == 1
        assert metadata["next_payment_attempt"] == 1740182400
        assert metadata["failed_amount"] == 49.00
        # Winner's own metadata is preserved alongside
        assert metadata["subscription_id"] == "sub_abc"
        # Email still merged from the failure event
        assert customer["email"] == "test@example.com"

    def test_aggregate_failure_merge_does_not_mutate_stored_metadata(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that the aggregated metadata is deep-copied, not shared.

        Metadata can contain nested structures (e.g. Shopify line_items),
        so a shallow copy would still share the inner lists/dicts - any
        later mutation of the result would corrupt the stored item.
        """
        winner_metadata = {
            "subscription_id": "sub_abc",
            "line_items": [{"title": "Widget", "quantity": 1}],
        }
        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "metadata": winner_metadata,
                },
                "customer_data": {},
            },
            {
                "event_data": {
                    "type": "payment_failure",
                    "metadata": {"failure_reason": "Card declined"},
                },
                "customer_data": {},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        # Top-level merge must not leak into the stored item
        assert "has_payment_failure" not in winner_metadata

        # Nested structures must be independent copies too
        event["metadata"]["line_items"][0]["title"] = "MUTATED"
        event["metadata"]["line_items"].append({"title": "extra"})

        assert winner_metadata["line_items"] == [{"title": "Widget", "quantity": 1}]

    def test_aggregate_failure_merge_with_absent_winner_metadata(
        self, queue: PendingEventQueue
    ) -> None:
        """Test failure merge when the winner has no metadata key at all."""
        winner_event = {"type": "subscription_created", "customer_id": "cus_123"}
        stored_items = [
            {"event_data": winner_event, "customer_data": {}},
            {
                "event_data": {
                    "type": "payment_failure",
                    "metadata": {"failure_reason": "Card declined"},
                },
                "customer_data": {},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["metadata"]["has_payment_failure"] is True
        assert event["metadata"]["failure_reason"] == "Card declined"
        # The stored item must not have been mutated
        assert "metadata" not in winner_event

    def test_aggregate_failure_merge_with_none_winner_metadata(
        self, queue: PendingEventQueue
    ) -> None:
        """Test failure merge when the winner's metadata is explicitly None."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created", "metadata": None},
                "customer_data": {},
            },
            {
                "event_data": {
                    "type": "payment_failure",
                    "metadata": {"failure_reason": "Card declined"},
                },
                "customer_data": {},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["metadata"]["has_payment_failure"] is True
        assert event["metadata"]["failure_reason"] == "Card declined"

    def test_aggregate_failure_merge_with_none_failure_metadata(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that a failure event with None metadata still gets surfaced."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created", "metadata": {}},
                "customer_data": {},
            },
            {
                "event_data": {
                    "type": "payment_failure",
                    "amount": 49.00,
                    "metadata": None,
                },
                "customer_data": {},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["metadata"]["has_payment_failure"] is True
        assert event["metadata"]["failed_amount"] == 49.00

    def test_aggregate_preserves_zero_failed_amount(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that a legitimate 0.0 failed amount is kept (is not None check)."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created", "metadata": {}},
                "customer_data": {},
            },
            {
                "event_data": {
                    "type": "payment_failure",
                    "amount": 0.0,
                    "metadata": {"failure_reason": "Card declined"},
                },
                "customer_data": {},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["metadata"]["failed_amount"] == 0.0

    def test_aggregate_lone_payment_failure_stays_failure(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that a bucket of only failure events notifies as a failure."""
        stored_items = [
            {
                "event_data": {
                    "type": "payment_failure",
                    "customer_id": "cus_123",
                    "amount": 49.00,
                    "metadata": {"failure_reason": "Card declined"},
                },
                "customer_data": {"email": "test@example.com"},
            },
            {
                "event_data": {
                    "type": "invoice_paid",
                    "customer_id": "cus_123",
                    "amount": 0,
                },
                "customer_data": {"email": "test@example.com"},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "payment_failure"
        assert event["metadata"]["failure_reason"] == "Card declined"
        assert "has_payment_failure" not in event["metadata"]

    def test_aggregate_copies_metadata_from_preferred_type(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that metadata is copied from the preferred event type."""
        stored_items = [
            {
                "event_data": {
                    "type": "invoice_paid",
                    "metadata": {"plan": "basic"},
                },
                "customer_data": {},
            },
            {
                "event_data": {
                    "type": "subscription_created",
                    "metadata": {"plan": "pro", "billing_period": "monthly"},
                },
                "customer_data": {},
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        assert event["type"] == "subscription_created"
        assert event["metadata"]["plan"] == "pro"
        assert event["metadata"]["billing_period"] == "monthly"


class TestPendingEventQueueScheduling:
    """Test timer scheduling functionality."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance with short delay."""
        queue = PendingEventQueue()
        queue.DELAY_SECONDS = 0.1  # 100ms for fast tests
        # Clear any existing timers
        with queue._lock:
            queue._active_timers.clear()
        return queue

    def test_schedule_creates_timer(self, queue: PendingEventQueue) -> None:
        """Test that scheduling creates a timer."""
        with patch.object(queue, "_process_events"):
            queue._schedule_processing("idem_key", "ws_123", "stripe", None)

            timer_key = "ws_123:idem_key"
            assert timer_key in queue._active_timers
            assert isinstance(queue._active_timers[timer_key], threading.Timer)

            # Clean up
            queue._active_timers[timer_key].cancel()

    def test_schedule_does_not_duplicate_timer(self, queue: PendingEventQueue) -> None:
        """Test that scheduling twice doesn't create duplicate timers."""
        with patch.object(queue, "_process_events"):
            queue._schedule_processing("idem_key", "ws_123", "stripe", None)
            first_timer = queue._active_timers["ws_123:idem_key"]

            queue._schedule_processing("idem_key", "ws_123", "stripe", None)
            second_timer = queue._active_timers["ws_123:idem_key"]

            assert first_timer is second_timer

            # Clean up
            first_timer.cancel()


class TestPendingEventQueueIntegration:
    """Integration tests for the full queue flow."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance with short delay."""
        queue = PendingEventQueue()
        queue.DELAY_SECONDS = 0.2  # 200ms for tests
        with queue._lock:
            queue._active_timers.clear()
        return queue

    @pytest.fixture
    def mock_cache(self):
        """Mock Django cache."""
        cache_data: dict = {}

        def mock_get(key, default=None):
            return cache_data.get(key, default)

        def mock_set(key, value, timeout=None):
            cache_data[key] = value

        def mock_delete(key):
            cache_data.pop(key, None)

        with patch("webhooks.services.pending_event_queue.cache") as mock:
            mock.get = mock_get
            mock.set = mock_set
            mock.delete = mock_delete
            yield mock

    def test_queue_event_stores_and_schedules(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that queue_event both stores and schedules."""
        event_data = {"type": "subscription_created", "customer_id": "cus_123"}
        customer_data = {"email": "test@example.com"}

        with patch.object(queue, "_process_events"):
            queue.queue_event(
                idempotency_key="idem_123",
                workspace_id="ws_456",
                event_data=event_data,
                customer_data=customer_data,
                provider_name="stripe",
                workspace=None,
            )

        # Check event was stored
        key = "pending_webhook:ws_456:idem_123"
        stored = mock_cache.get(key)
        assert stored is not None
        assert len(stored) == 1

        # Check timer was scheduled
        assert "ws_456:idem_123" in queue._active_timers

        # Clean up
        queue._active_timers["ws_456:idem_123"].cancel()

    def test_multiple_events_same_key_aggregated(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that multiple events with same key are aggregated."""
        with patch.object(queue, "_process_events"):
            # First event (subscription.created - no email)
            queue.queue_event(
                idempotency_key="idem_123",
                workspace_id="ws_456",
                event_data={
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                },
                customer_data={"email": ""},
                provider_name="stripe",
                workspace=None,
            )

            # Second event (invoice.paid - has email)
            queue.queue_event(
                idempotency_key="idem_123",
                workspace_id="ws_456",
                event_data={
                    "type": "invoice_paid",
                    "customer_id": "cus_123",
                },
                customer_data={"email": "found@example.com"},
                provider_name="stripe",
                workspace=None,
            )

        # Check both events were stored
        key = "pending_webhook:ws_456:idem_123"
        stored = mock_cache.get(key)
        assert len(stored) == 2

        # Clean up
        queue._active_timers["ws_456:idem_123"].cancel()


class TestPendingEventQueueProcessing:
    """Test event processing and notification sending."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance."""
        queue = PendingEventQueue()
        with queue._lock:
            queue._active_timers.clear()
        return queue

    def test_process_events_sends_notification(self, queue: PendingEventQueue) -> None:
        """Test that processing sends a notification."""
        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                    "amount": 29.99,
                },
                "customer_data": {"email": "test@example.com"},
            },
        ]

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = stored_items
            mock_cache.add.return_value = True  # Acquire lock successfully

            with patch.object(
                queue, "_send_notification", return_value=True
            ) as mock_send:
                queue._process_events("idem_123", "ws_456", "stripe", None)

                mock_send.assert_called_once()
                call_args = mock_send.call_args
                assert call_args[0][0]["type"] == "subscription_created"
                assert call_args[0][1]["email"] == "test@example.com"

    def test_process_events_cleans_up_cache(self, queue: PendingEventQueue) -> None:
        """Test that processing deletes events from cache after success."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {"email": "test@example.com"},
            },
        ]

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = stored_items
            mock_cache.add.return_value = True  # Acquire lock

            with patch.object(queue, "_send_notification", return_value=True):
                queue._process_events("idem_123", "ws_456", "stripe", None)

                mock_cache.delete.assert_any_call("pending_webhook:ws_456:idem_123")

    def test_process_events_does_not_delete_on_failure(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that events are NOT deleted if notification fails."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {"email": "test@example.com"},
            },
        ]

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = stored_items
            mock_cache.add.return_value = True  # Acquire lock

            with patch.object(queue, "_send_notification", return_value=False):
                queue._process_events("idem_123", "ws_456", "stripe", None)

                # Should NOT delete the events - they should remain for retry
                delete_calls = [
                    call
                    for call in mock_cache.delete.call_args_list
                    if "pending_webhook" in str(call)
                ]
                assert len(delete_calls) == 0

    def test_process_events_removes_timer_reference(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that processing removes the timer reference."""
        # Add a fake timer reference
        timer_key = "ws_456:idem_123"
        queue._active_timers[timer_key] = MagicMock()

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = []
            mock_cache.add.return_value = True  # Acquire lock

            queue._process_events("idem_123", "ws_456", "stripe", None)

            assert timer_key not in queue._active_timers

    def test_process_events_handles_empty_cache(self, queue: PendingEventQueue) -> None:
        """Test that processing handles missing/expired events gracefully."""
        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = []
            mock_cache.add.return_value = True  # Acquire lock

            with patch.object(queue, "_send_notification") as mock_send:
                # Should not raise
                queue._process_events("idem_123", "ws_456", "stripe", None)

                # Should not send notification
                mock_send.assert_not_called()

    def test_process_events_skips_if_lock_not_acquired(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that processing is skipped if another process has the lock."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {"email": "test@example.com"},
            },
        ]

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = stored_items
            mock_cache.add.return_value = False  # Lock NOT acquired

            with patch.object(queue, "_send_notification") as mock_send:
                queue._process_events("idem_123", "ws_456", "stripe", None)

                # Should not send - another process has the lock
                mock_send.assert_not_called()


class TestRealisticScenarios:
    """Test realistic webhook scenarios based on production data."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance."""
        return PendingEventQueue()

    def test_stripe_subscription_flow_aggregation(
        self, queue: PendingEventQueue
    ) -> None:
        """Test aggregation of realistic Stripe subscription events.

        Simulates the actual production scenario:
        - subscription.created at T+0ms (no email)
        - invoice.paid at T+751ms (has email)
        - invoice.payment_succeeded at T+967ms (has email)

        All share the same idempotency_key.
        """
        stored_items = [
            {
                "event_data": {
                    "type": "trial_started",  # Transformed from subscription_created
                    "customer_id": "cus_TsAte3VpFw5ucr",
                    "amount": 0,
                    "metadata": {"is_trial": True, "trial_days": 14},
                },
                "customer_data": {
                    "email": "",
                    "first_name": "",
                    "last_name": "",
                },
            },
            {
                "event_data": {
                    "type": "invoice_paid",
                    "customer_id": "cus_TsAte3VpFw5ucr",
                    "customer_email": "senitew931@gamening.com",
                    "amount": 0,
                },
                "customer_data": {
                    "email": "senitew931@gamening.com",
                    "first_name": "",
                    "last_name": "",
                },
            },
            {
                "event_data": {
                    "type": "payment_success",
                    "customer_id": "cus_TsAte3VpFw5ucr",
                    "customer_email": "senitew931@gamening.com",
                    "amount": 0,
                },
                "customer_data": {
                    "email": "senitew931@gamening.com",
                    "first_name": "",
                    "last_name": "",
                },
            },
        ]

        event, customer = queue._aggregate_events(stored_items)

        # Should have email from invoice events
        assert customer["email"] == "senitew931@gamening.com"

        # Should have trial_started type (highest priority)
        assert event["type"] == "trial_started"

        # Should have trial metadata
        assert event["metadata"]["is_trial"] is True
        assert event["metadata"]["trial_days"] == 14


class TestOrphanRecovery:
    """Test orphaned event recovery on server startup."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance."""
        return PendingEventQueue()

    def test_is_orphaned_returns_true_for_old_events(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that old events are identified as orphaned."""
        import time

        from webhooks.services.pending_event_queue import ORPHAN_MIN_AGE_SECONDS

        # 10s older than threshold
        old_timestamp = time.time() - ORPHAN_MIN_AGE_SECONDS - 10

        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "_queued_at": old_timestamp,
                },
                "customer_data": {},
            }
        ]

        assert queue._is_orphaned(stored_items) is True

    def test_is_orphaned_returns_false_for_recent_events(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that recent events are not identified as orphaned."""
        import time

        recent_timestamp = time.time() - 5  # Only 5 seconds old

        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "_queued_at": recent_timestamp,
                },
                "customer_data": {},
            }
        ]

        assert queue._is_orphaned(stored_items) is False

    def test_is_orphaned_returns_true_for_missing_timestamp(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that events without timestamp are assumed orphaned."""
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {},
            }
        ]

        assert queue._is_orphaned(stored_items) is True

    def test_is_orphaned_returns_false_for_empty_list(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that empty list returns False."""
        assert queue._is_orphaned([]) is False

    def test_get_redis_client_returns_none_on_error(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that _get_redis_client handles errors gracefully."""
        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.client.get_client.side_effect = AttributeError("No client")
            mock_cache._cache.get_client.side_effect = AttributeError("No client")

            result = queue._get_redis_client()

            assert result is None

    def test_get_redis_client_uses_django_builtin_backend(
        self, queue: PendingEventQueue
    ) -> None:
        """Test the fallback to Django's built-in RedisCache backend.

        Production uses django.core.cache.backends.redis.RedisCache, which
        exposes the raw client via cache._cache.get_client - NOT the
        django-redis cache.client API. Without this fallback, orphan
        recovery silently returns 0 in production and queued notifications
        whose delivery failed are never retried.
        """
        raw_client = MagicMock(name="raw_redis_client")
        # Built-in backend has no `.client` attribute at all
        mock_cache = MagicMock(spec=["_cache"])
        mock_cache._cache.get_client.return_value = raw_client

        with patch("webhooks.services.pending_event_queue.cache", mock_cache):
            result = queue._get_redis_client()

        assert result is raw_client
        mock_cache._cache.get_client.assert_called_once_with(None, write=True)

    def test_scan_pending_keys_uses_and_strips_cache_prefix(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that scanning matches prefixed keys and yields logical keys.

        The backend stores entries under KEY_PREFIX:version:key (e.g.
        "notipus:1:pending_webhook:..."). An unprefixed SCAN matches
        nothing, and unstripped keys would miss on cache.get - both make
        recovery a silent no-op.
        """
        redis_client = MagicMock()
        redis_client.scan.return_value = (
            0,
            [
                b"notipus:1:pending_webhook:ws_123:idem_abc",
                b"notipus:1:pending_webhook:global:idem_def",
            ],
        )

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.make_key.return_value = "notipus:1:"

            keys = list(queue._scan_pending_keys(redis_client))

        redis_client.scan.assert_called_once_with(
            0, match="notipus:1:pending_webhook:*", count=100
        )
        assert keys == [
            "pending_webhook:ws_123:idem_abc",
            "pending_webhook:global:idem_def",
        ]

    def test_recover_orphaned_events_returns_zero_when_no_client(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that recovery returns 0 when Redis client unavailable."""
        with patch.object(queue, "_get_redis_client", return_value=None):
            result = queue.recover_orphaned_events()
            assert result == 0

    def test_recover_single_event_processes_orphaned_event(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that a single orphaned event is processed."""
        import time

        old_timestamp = time.time() - 60  # 1 minute old

        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "customer_id": "cus_123",
                    "_queued_at": old_timestamp,
                },
                "customer_data": {"email": "test@example.com"},
            }
        ]

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = stored_items

            with patch.object(queue, "_process_events") as mock_process:
                # Use "global" workspace to avoid DB lookup
                result = queue._recover_single_event(
                    "pending_webhook:global:idem_key_abc"
                )

                assert result is True
                mock_process.assert_called_once_with(
                    idempotency_key="idem_key_abc",
                    workspace_id="global",
                    provider_name="stripe",
                    workspace=None,
                )

    def test_recover_single_event_skips_recent_events(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that recent events are skipped during recovery."""
        import time

        recent_timestamp = time.time() - 5  # Only 5 seconds old

        stored_items = [
            {
                "event_data": {
                    "type": "subscription_created",
                    "_queued_at": recent_timestamp,
                },
                "customer_data": {},
            }
        ]

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = stored_items

            with patch.object(queue, "_process_events") as mock_process:
                # Use "global" workspace to avoid DB lookup
                result = queue._recover_single_event(
                    "pending_webhook:global:idem_key_abc"
                )

                assert result is False
                mock_process.assert_not_called()

    def test_recover_single_event_handles_invalid_key_format(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that invalid key formats are handled gracefully."""
        result = queue._recover_single_event("invalid_key")
        assert result is False

        result = queue._recover_single_event("only:two")
        assert result is False

        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.get.return_value = None
            # Valid format but no data in cache
            result = queue._recover_single_event("pending_webhook:global:key")
            assert result is False


class TestCustomerBasedAggregation:
    """Test customer-based aggregation for events without idempotency_key.

    When Stripe events arrive without an idempotency_key (common for
    Stripe-initiated flows), we use customer_id as a fallback aggregation key
    with a 60-second time bucket.
    """

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance for each test with proper cleanup."""
        queue = PendingEventQueue()
        queue.DELAY_SECONDS = 0.1  # Short delay for tests
        yield queue
        # Cleanup all timers after test
        for timer in list(queue._active_timers.values()):
            timer.cancel()
        queue._active_timers.clear()

    @pytest.fixture
    def mock_cache(self):
        """Mock Django cache."""
        cache_data: dict = {}

        def mock_get(key, default=None):
            return cache_data.get(key, default)

        def mock_set(key, value, timeout=None):
            cache_data[key] = value

        def mock_delete(key):
            cache_data.pop(key, None)

        with patch("webhooks.services.pending_event_queue.cache") as mock:
            mock.get = mock_get
            mock.set = mock_set
            mock.delete = mock_delete
            yield mock

    def test_customer_key_adds_time_bucket(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that customer-based keys get a time bucket appended."""
        import time

        event_data = {"type": "trial_started", "customer_id": "cus_123"}
        customer_data = {"email": ""}

        # Calculate expected time bucket
        expected_bucket = int(time.time() // 60)

        with patch.object(queue, "_process_events"):
            queue.queue_event(
                idempotency_key="customer:cus_123",
                workspace_id="ws_456",
                event_data=event_data,
                customer_data=customer_data,
                provider_name="stripe",
                workspace=None,
            )

        # Check that key includes time bucket
        expected_key = f"pending_webhook:ws_456:customer:cus_123:t{expected_bucket}"
        stored = mock_cache.get(expected_key)
        assert stored is not None
        assert len(stored) == 1

    def test_regular_idempotency_key_unchanged(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that regular idempotency keys are not modified."""
        event_data = {"type": "subscription_created", "customer_id": "cus_123"}
        customer_data = {"email": "test@example.com"}

        with patch.object(queue, "_process_events"):
            queue.queue_event(
                idempotency_key="req_abc123",  # Regular Stripe key
                workspace_id="ws_456",
                event_data=event_data,
                customer_data=customer_data,
                provider_name="stripe",
                workspace=None,
            )

        # Check that key is unchanged (no time bucket)
        expected_key = "pending_webhook:ws_456:req_abc123"
        stored = mock_cache.get(expected_key)
        assert stored is not None
        assert len(stored) == 1

    def test_customer_based_events_aggregate_within_time_bucket(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that customer-based events aggregate within the same time bucket."""
        import time

        # Get current time bucket
        time_bucket = int(time.time() // 60)

        with patch.object(queue, "_process_events"):
            # First event (subscription - no email)
            queue.queue_event(
                idempotency_key="customer:cus_123",
                workspace_id="ws_456",
                event_data={
                    "type": "trial_started",
                    "customer_id": "cus_123",
                },
                customer_data={"email": ""},
                provider_name="stripe",
                workspace=None,
            )

            # Second event (invoice - has email)
            queue.queue_event(
                idempotency_key="customer:cus_123",
                workspace_id="ws_456",
                event_data={
                    "type": "invoice_paid",
                    "customer_id": "cus_123",
                    "customer_email": "test@example.com",
                },
                customer_data={"email": "test@example.com"},
                provider_name="stripe",
                workspace=None,
            )

        # Both events should be stored under the same key
        expected_key = f"pending_webhook:ws_456:customer:cus_123:t{time_bucket}"
        stored = mock_cache.get(expected_key)
        assert stored is not None
        assert len(stored) == 2

        # Aggregate and verify email is found
        event, customer = queue._aggregate_events(stored)
        assert customer["email"] == "test@example.com"
        assert event["type"] == "trial_started"  # Higher priority

    def test_customer_based_events_aggregate_across_bucket_boundary(
        self, queue: PendingEventQueue, mock_cache
    ) -> None:
        """Test that events aggregate even when second arrives after bucket boundary.

        Simulates: subscription arrives at T=59s (bucket N), invoice at T=61s
        (bucket N+1). The invoice should join the subscription's bucket.
        """
        import time

        # Get current time bucket
        current_bucket = int(time.time() // 60)
        previous_bucket = current_bucket - 1

        # Pre-populate cache with an event in the previous bucket
        # (simulating subscription that arrived just before bucket boundary)
        prev_key = f"pending_webhook:ws_456:customer:cus_123:t{previous_bucket}"
        mock_cache.set(
            prev_key,
            [
                {
                    "event_data": {"type": "trial_started", "customer_id": "cus_123"},
                    "customer_data": {"email": ""},
                }
            ],
        )

        with patch.object(queue, "_process_events"):
            # Second event arrives in "current" bucket but should join previous
            queue.queue_event(
                idempotency_key="customer:cus_123",
                workspace_id="ws_456",
                event_data={
                    "type": "invoice_paid",
                    "customer_id": "cus_123",
                    "customer_email": "test@example.com",
                },
                customer_data={"email": "test@example.com"},
                provider_name="stripe",
                workspace=None,
            )

        # Event should have been added to the PREVIOUS bucket (not current)
        stored = mock_cache.get(prev_key)
        assert stored is not None
        assert len(stored) == 2  # Original + new event

        # Current bucket should NOT have been created
        curr_key = f"pending_webhook:ws_456:customer:cus_123:t{current_bucket}"
        assert mock_cache.get(curr_key) is None


class TestDeliveryRetrySemantics:
    """Test the delivery-retry window and bounded retry behavior.

    Once queued, the router has acknowledged the webhook to the provider
    and recorded a dedup marker that suppresses provider redelivery, so
    delivery is entirely this queue's responsibility.
    """

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance."""
        queue = PendingEventQueue()
        with queue._lock:
            queue._active_timers.clear()
        return queue

    @pytest.fixture
    def stateful_cache(self):
        """Stateful cache mock backing get/set/add/delete with a dict."""
        cache_data: dict = {}

        def mock_get(key, default=None):
            return cache_data.get(key, default)

        def mock_set(key, value, timeout=None):
            cache_data[key] = value

        def mock_add(key, value, timeout=None):
            if key in cache_data:
                return False
            cache_data[key] = value
            return True

        def mock_delete(key):
            cache_data.pop(key, None)

        with patch("webhooks.services.pending_event_queue.cache") as mock:
            mock.get = mock_get
            mock.set = mock_set
            mock.add = mock_add
            mock.delete = mock_delete
            mock.data = cache_data
            yield mock

    def test_retry_window_exceeds_dedup_window(self) -> None:
        """Test that events outlive the provider-retry suppression marker.

        The router records a dedup marker (30 min) when an event is
        queued, so the provider will not redeliver. If pending events
        expired before that (the old 300s TTL), a delivery failure lost
        the notification with no possible retry from either side.
        """
        from webhooks.services.event_consolidation import EventConsolidationService

        dedup_seconds = (
            EventConsolidationService.CONSOLIDATION_WINDOW_SECONDS
            * EventConsolidationService.DEDUP_WINDOW_MULTIPLIER
        )

        assert PendingEventQueue.TTL_SECONDS > dedup_seconds

    def test_failed_send_increments_attempt_counter(
        self, queue: PendingEventQueue, stateful_cache
    ) -> None:
        """Test that each failed delivery increments the bounded counter."""
        key = "pending_webhook:ws_456:idem_123"
        attempts_key = "pending_webhook_attempts:ws_456:idem_123"
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {"email": "test@example.com"},
            },
        ]
        stateful_cache.set(key, stored_items)

        with patch.object(queue, "_send_notification", return_value=False):
            queue._process_events("idem_123", "ws_456", "stripe", None)
            queue._process_events("idem_123", "ws_456", "stripe", None)

        # Events remain for the next recovery sweep; counter tracks attempts
        assert stateful_cache.get(key) == stored_items
        assert stateful_cache.get(attempts_key) == 2

    def test_gives_up_loudly_after_max_attempts(
        self, queue: PendingEventQueue, stateful_cache
    ) -> None:
        """Test that a permanently failing group is dropped after the cap.

        A revoked Slack webhook URL never succeeds; without a cap the
        sweep would retry it every interval for the whole TTL window.
        """
        from webhooks.services.pending_event_queue import MAX_SEND_ATTEMPTS

        key = "pending_webhook:ws_456:idem_123"
        attempts_key = "pending_webhook_attempts:ws_456:idem_123"
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {"email": "test@example.com"},
            },
        ]
        stateful_cache.set(key, stored_items)
        stateful_cache.set(attempts_key, MAX_SEND_ATTEMPTS - 1)

        with patch.object(queue, "_send_notification", return_value=False):
            queue._process_events("idem_123", "ws_456", "stripe", None)

        # Group and counter are dropped - explicitly, not by silent expiry
        assert stateful_cache.get(key) is None
        assert stateful_cache.get(attempts_key) is None

    def test_successful_send_clears_attempt_counter(
        self, queue: PendingEventQueue, stateful_cache
    ) -> None:
        """Test that a successful delivery resets the failure bookkeeping."""
        key = "pending_webhook:ws_456:idem_123"
        attempts_key = "pending_webhook_attempts:ws_456:idem_123"
        stored_items = [
            {
                "event_data": {"type": "subscription_created"},
                "customer_data": {"email": "test@example.com"},
            },
        ]
        stateful_cache.set(key, stored_items)
        stateful_cache.set(attempts_key, 3)

        with patch.object(queue, "_send_notification", return_value=True):
            queue._process_events("idem_123", "ws_456", "stripe", None)

        assert stateful_cache.get(key) is None
        assert stateful_cache.get(attempts_key) is None


class TestLockedAppend:
    """Test the distributed-lock append used for pending event storage."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance."""
        return PendingEventQueue()

    def test_append_raises_when_lock_never_acquired(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that lock starvation raises instead of silently dropping.

        A raise propagates to the router, which returns 5xx BEFORE the
        dedup marker is recorded - so the provider redelivers the webhook
        instead of the event being lost.
        """
        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.add.return_value = False  # Lock always held elsewhere

            with patch(
                "webhooks.services.pending_event_queue.time.sleep"
            ):  # Don't actually wait
                with pytest.raises(RuntimeError, match="append lock"):
                    queue._locked_append("pending_webhook:ws:idem", {"x": 1})

    def test_append_releases_lock_even_on_error(self, queue: PendingEventQueue) -> None:
        """Test that the append lock is released when cache.set fails."""
        with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
            mock_cache.add.return_value = True
            mock_cache.get.return_value = []
            mock_cache.set.side_effect = RuntimeError("redis down")

            with pytest.raises(RuntimeError, match="redis down"):
                queue._locked_append("pending_webhook:ws:idem", {"x": 1})

            mock_cache.delete.assert_called_once_with(
                "append_lock:pending_webhook:ws:idem"
            )


class TestPeriodicRecovery:
    """Test the background recovery sweep."""

    @pytest.fixture
    def queue(self) -> PendingEventQueue:
        """Create a fresh queue instance with the sweeper flag reset."""
        PendingEventQueue._recovery_thread_started = False
        yield PendingEventQueue()
        PendingEventQueue._recovery_thread_started = False

    def test_start_periodic_recovery_starts_thread_once(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that repeated calls start exactly one daemon thread."""
        with patch(
            "webhooks.services.pending_event_queue.threading.Thread"
        ) as mock_thread:
            queue.start_periodic_recovery()
            queue.start_periodic_recovery()

        assert mock_thread.call_count == 1
        assert mock_thread.call_args.kwargs["daemon"] is True
        mock_thread.return_value.start.assert_called_once()

    def test_failed_thread_start_allows_retry(self, queue: PendingEventQueue) -> None:
        """Test that a failed thread start does not disable recovery forever.

        If the started-flag stayed True after Thread creation/start raised
        (e.g. resource exhaustion), every later call would no-op and
        periodic recovery would be silently disabled for the process.
        """
        with patch(
            "webhooks.services.pending_event_queue.threading.Thread"
        ) as mock_thread:
            mock_thread.return_value.start.side_effect = RuntimeError(
                "can't start new thread"
            )
            with pytest.raises(RuntimeError, match="can't start new thread"):
                queue.start_periodic_recovery()

            assert PendingEventQueue._recovery_thread_started is False

            # A later call must be able to try again and succeed
            mock_thread.return_value.start.side_effect = None
            queue.start_periodic_recovery()

        assert mock_thread.call_count == 2
        assert PendingEventQueue._recovery_thread_started is True

    def test_sweep_runs_recovery_under_fleet_lock(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that a sweep recovers events only when it wins the lock."""
        sleep_calls = {"count": 0}

        def fake_sleep(seconds):
            sleep_calls["count"] += 1
            if sleep_calls["count"] > 1:
                raise KeyboardInterrupt  # Break out of the infinite loop

        with patch(
            "webhooks.services.pending_event_queue.time.sleep",
            side_effect=fake_sleep,
        ):
            with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
                mock_cache.add.return_value = True  # Win the fleet-wide lock
                with patch.object(
                    queue, "recover_orphaned_events", return_value=0
                ) as mock_recover:
                    with pytest.raises(KeyboardInterrupt):
                        queue._periodic_recovery_loop(300)

        mock_recover.assert_called_once()

    def test_sweep_skips_when_another_worker_holds_lock(
        self, queue: PendingEventQueue
    ) -> None:
        """Test that losing the fleet-wide lock skips the sweep."""
        sleep_calls = {"count": 0}

        def fake_sleep(seconds):
            sleep_calls["count"] += 1
            if sleep_calls["count"] > 1:
                raise KeyboardInterrupt

        with patch(
            "webhooks.services.pending_event_queue.time.sleep",
            side_effect=fake_sleep,
        ):
            with patch("webhooks.services.pending_event_queue.cache") as mock_cache:
                mock_cache.add.return_value = False  # Another worker sweeps
                with patch.object(queue, "recover_orphaned_events") as mock_recover:
                    with pytest.raises(KeyboardInterrupt):
                        queue._periodic_recovery_loop(300)

        mock_recover.assert_not_called()

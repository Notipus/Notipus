"""Tests for event consolidation service.

This module tests the EventConsolidationService which prevents notification
spam by consolidating related webhook events that fire in quick succession.
"""

from unittest.mock import patch

import pytest
from webhooks.services.event_consolidation import EventConsolidationService


class TestEventConsolidationService:
    """Test EventConsolidationService functionality."""

    @pytest.fixture
    def service(self) -> EventConsolidationService:
        """Create a fresh consolidation service for each test.

        Returns:
            EventConsolidationService instance.
        """
        return EventConsolidationService()

    @pytest.fixture
    def mock_cache(self):
        """Mock Django cache for testing.

        Yields:
            Mock cache with get/set methods.
        """
        cache_data: dict = {}

        def mock_get(key: str, default=None):
            return cache_data.get(key, default)

        def mock_set(key: str, value, timeout=None):
            cache_data[key] = value

        with patch("webhooks.services.event_consolidation.cache") as mock:
            mock.get = mock_get
            mock.set = mock_set
            yield mock

    def test_primary_event_allows_notification(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that primary events always allow notifications."""
        result = service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
        )

        assert result is True

    def test_secondary_event_suppressed_after_primary(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that secondary events of the same transaction are suppressed."""
        # First, process the primary event
        service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        # Now the secondary event should be suppressed
        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        assert result is False

    def test_invoice_paid_suppressed_after_subscription_created(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that invoice_paid is suppressed after subscription_created."""
        # Process subscription_created
        service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        # invoice_paid should be suppressed
        result = service.should_send_notification(
            event_type="invoice_paid",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        assert result is False

    def test_events_without_correlator_never_suppressed(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that events lacking a transaction correlator are delivered.

        Without a correlator we cannot tell which transaction a suppression
        marker belongs to, so a coarse customer-level fallback could swallow
        an unrelated transaction's notification. Deliver instead.
        """
        service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
        )

        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=100.00,
        )

        assert result is True

    def test_correlated_primary_does_not_suppress_uncorrelated_secondary(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that a correlated primary never suppresses a correlator-less event."""
        service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=100.00,
        )

        assert result is True

    def test_payment_failure_never_suppressed(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that payment_failure is never suppressed."""
        # Even after a primary event for the SAME transaction,
        # payment_failure should go through
        service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        result = service.should_send_notification(
            event_type="payment_failure",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        assert result is True

    def test_trial_ending_never_suppressed(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that trial_ending is delivered as its own notification.

        Stripe fires trial_will_end ~3 days before the trial converts, so a
        cache-based merge with the later payment can never work. The product
        decision is to deliver BOTH: "trial ending in 3 days" now and
        "trial converted" later (detected statelessly from the invoice).
        """
        result = service.should_send_notification(
            event_type="trial_ending",
            customer_id="cus_123",
            workspace_id="ws_456",
        )

        assert result is True

    def test_trial_ending_not_suppressed_even_after_payment(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that a recent payment does not suppress a trial_ending warning."""
        service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=100.00,
        )

        result = service.should_send_notification(
            event_type="trial_ending",
            customer_id="cus_123",
            workspace_id="ws_456",
        )

        assert result is True

    def test_trial_lifecycle_produces_both_notifications(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test the full trial lifecycle: warning AND conversion notifications.

        Sequence: trial_will_end fires ~3 days before the trial ends, then
        the first paid invoice arrives when it actually converts. The user
        must receive both notifications - the conversion is detected
        statelessly from the invoice payload (is_trial_conversion metadata
        set by the Stripe parser), not from a cache marker that would have
        expired days earlier.
        """
        from webhooks.services.insight_detector import InsightDetector

        # T=0: trial_will_end arrives -> "trial ending in 3 days" delivered
        trial_ending_allowed = service.should_send_notification(
            event_type="trial_ending",
            customer_id="cus_123",
            workspace_id="ws_456",
        )
        assert trial_ending_allowed is True

        # T=+3 days: first paid invoice after the trial arrives. The parser
        # flagged it as a trial conversion from the payload alone.
        payment_event = {
            "type": "payment_success",
            "customer_id": "cus_123",
            "workspace_id": "ws_456",
            "amount": 29.00,
            "metadata": {"is_trial_conversion": True},
        }
        payment_allowed = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=29.00,
        )
        assert payment_allowed is True

        # And the payment notification carries the "Trial converted" insight
        insight = InsightDetector().detect(payment_event, {"payment_history": []})
        assert insight is not None
        assert "Trial converted" in insight.text

    def test_unrelated_transaction_not_suppressed(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that suppression is scoped to the transaction correlator.

        A checkout for product A must not suppress the payment_success of an
        unrelated second purchase (different charge) by the same customer
        within the 5-minute window.
        """
        # T=0: checkout for product A suppresses ITS payment notifications
        service.should_send_notification(
            event_type="checkout_completed",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="ch_A",
        )

        # The same transaction's payment IS suppressed
        same_txn = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=50.00,
            correlation_id="ch_A",
        )
        assert same_txn is False

        # T=+90s: unrelated purchase (different charge) is NOT suppressed
        other_txn = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=500.00,
            correlation_id="ch_B",
        )
        assert other_txn is True

    def test_cancelling_one_sub_does_not_suppress_other_subs(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that cancelling one subscription only suppresses ITS invoice.

        A customer cancels their add-on subscription; the Basic
        subscription's monthly renewal invoice arriving within 5 minutes
        must still notify.
        """
        # Cancel the add-on subscription
        service.should_send_notification(
            event_type="subscription_deleted",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_addon",
        )

        # The add-on's final invoice IS suppressed (consolidated)
        addon_invoice = service.should_send_notification(
            event_type="invoice_paid",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=10.00,
            correlation_id="sub_addon",
        )
        assert addon_invoice is False

        # The Basic subscription's renewal invoice is NOT suppressed
        basic_invoice = service.should_send_notification(
            event_type="invoice_paid",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=29.00,
            correlation_id="sub_basic",
        )
        assert basic_invoice is True

    def test_extract_correlation_id_prefers_metadata(
        self, service: EventConsolidationService
    ) -> None:
        """Test correlator extraction prefers transaction metadata ids."""
        event = {
            "external_id": "in_123",
            "metadata": {"subscription_id": "sub_abc"},
        }
        assert service.extract_correlation_id(event) == "sub_abc"

    def test_extract_correlation_id_falls_back_to_external_id(
        self, service: EventConsolidationService
    ) -> None:
        """Test correlator extraction falls back to the external object id."""
        event = {"external_id": "in_123", "metadata": {}}
        assert service.extract_correlation_id(event) == "in_123"

    def test_extract_correlation_id_returns_none_without_identifiers(
        self, service: EventConsolidationService
    ) -> None:
        """Test correlator extraction returns None when nothing identifies it."""
        assert service.extract_correlation_id({"metadata": {}}) is None

    def test_different_customer_not_affected(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that consolidation is per-customer."""
        # Process subscription_created for customer 1
        service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        # Different customer should not be suppressed
        # Note: amount > 0 required to pass $0 payment filter
        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_789",
            workspace_id="ws_456",
            amount=100.00,
            correlation_id="sub_1",
        )

        assert result is True

    def test_different_workspace_not_affected(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that consolidation is per-workspace."""
        # Process subscription_created for workspace 1
        service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        # Different workspace should not be suppressed
        # Note: amount > 0 required to pass $0 payment filter
        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_other",
            amount=100.00,
            correlation_id="sub_1",
        )

        assert result is True

    def test_checkout_completed_suppresses_payment_events(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that checkout_completed suppresses its own payment events."""
        service.should_send_notification(
            event_type="checkout_completed",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="cs_1",
        )

        # Both payment_success and invoice_paid should be suppressed
        result1 = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=50.00,
            correlation_id="cs_1",
        )
        result2 = service.should_send_notification(
            event_type="invoice_paid",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=50.00,
            correlation_id="cs_1",
        )

        assert result1 is False
        assert result2 is False

    def test_empty_customer_id_allows_notification(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that empty customer_id always allows notification.

        Note: Uses subscription_created since payment_success without amount
        is filtered out by the $0 payment filter.
        """
        result = service.should_send_notification(
            event_type="subscription_created",
            customer_id="",
            workspace_id="ws_456",
        )

        assert result is True

    def test_empty_workspace_id_allows_notification(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that empty workspace_id always allows notification.

        Note: Uses subscription_created since payment_success without amount
        is filtered out by the $0 payment filter.
        """
        result = service.should_send_notification(
            event_type="subscription_created",
            customer_id="cus_123",
            workspace_id="",
        )

        assert result is True

    def test_is_duplicate_returns_false_for_new_event(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that is_duplicate returns False for new events."""
        result = service.is_duplicate(
            workspace_id="ws_456",
            external_id="evt_123",
        )

        assert result is False

    def test_is_duplicate_returns_true_for_recorded_event(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that is_duplicate returns True for recorded events."""
        # Record the event
        service.record_event(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            external_id="evt_123",
        )

        # Now it should be detected as duplicate
        result = service.is_duplicate(
            workspace_id="ws_456",
            external_id="evt_123",
        )

        assert result is True

    def test_is_duplicate_returns_false_for_empty_external_id(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that is_duplicate returns False for empty external_id."""
        result = service.is_duplicate(
            workspace_id="ws_456",
            external_id="",
        )

        assert result is False

    def test_is_duplicate_returns_false_for_none_external_id(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that is_duplicate returns False for None external_id."""
        result = service.is_duplicate(
            workspace_id="ws_456",
            external_id=None,
        )

        assert result is False

    def test_non_primary_event_does_not_suppress_others(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that non-primary events don't suppress other events."""
        # Process a non-primary event with amount > 0
        service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=100.00,
            correlation_id="in_1",
        )

        # Another payment_success should still go through
        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=100.00,
            correlation_id="in_1",
        )

        assert result is True

    def test_subscription_deleted_suppresses_invoice_paid(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that subscription_deleted suppresses its own invoice_paid."""
        service.should_send_notification(
            event_type="subscription_deleted",
            customer_id="cus_123",
            workspace_id="ws_456",
            correlation_id="sub_1",
        )

        result = service.should_send_notification(
            event_type="invoice_paid",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=10.00,
            correlation_id="sub_1",
        )

        assert result is False

    def test_shopify_order_created_suppresses_payment_success(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that Shopify order_created suppresses payment_success.

        When a Shopify order is placed, both orders/create and orders/paid webhooks
        fire. The order_created event should suppress the subsequent payment_success
        to prevent duplicate notifications.
        """
        # First, process order_created (from orders/create webhook).
        # Both webhooks carry the same order id as external_id, which
        # extract_correlation_id uses as the transaction correlator.
        service.should_send_notification(
            event_type="order_created",
            customer_id="shopify_cus_123",
            workspace_id="ws_456",
            correlation_id="450789469",
        )

        # Now payment_success (from orders/paid webhook) should be suppressed
        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="shopify_cus_123",
            workspace_id="ws_456",
            amount=100.00,
            correlation_id="450789469",
        )

        assert result is False

    def test_shopify_order_created_allows_notification(
        self, service: EventConsolidationService, mock_cache
    ) -> None:
        """Test that Shopify order_created allows its own notification."""
        result = service.should_send_notification(
            event_type="order_created",
            customer_id="shopify_cus_123",
            workspace_id="ws_456",
        )

        assert result is True


class TestZeroAmountFiltering:
    """Test zero-amount payment filtering functionality."""

    @pytest.fixture
    def service(self) -> EventConsolidationService:
        """Create a fresh consolidation service for each test.

        Returns:
            EventConsolidationService instance.
        """
        return EventConsolidationService()

    def test_zero_amount_payment_success_suppressed(
        self, service: EventConsolidationService
    ) -> None:
        """Test that $0 payment_success events are suppressed."""
        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=0.0,
        )
        assert result is False

    def test_zero_amount_invoice_paid_suppressed(
        self, service: EventConsolidationService
    ) -> None:
        """Test that $0 invoice_paid events are suppressed."""
        result = service.should_send_notification(
            event_type="invoice_paid",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=0.0,
        )
        assert result is False

    def test_none_amount_payment_success_suppressed(
        self, service: EventConsolidationService
    ) -> None:
        """Test that payment_success with no amount is suppressed."""
        result = service.should_send_notification(
            event_type="payment_success",
            customer_id="cus_123",
            workspace_id="ws_456",
            amount=None,
        )
        assert result is False

    def test_positive_amount_payment_success_allowed(
        self, service: EventConsolidationService
    ) -> None:
        """Test that payment_success with positive amount is allowed."""
        with patch("webhooks.services.event_consolidation.cache") as mock_cache:
            mock_cache.get.return_value = None
            result = service.should_send_notification(
                event_type="payment_success",
                customer_id="cus_123",
                workspace_id="ws_456",
                amount=100.00,
            )
        assert result is True

    def test_subscription_created_not_affected_by_zero_filter(
        self, service: EventConsolidationService
    ) -> None:
        """Test that subscription_created is not affected by zero amount filter."""
        with patch("webhooks.services.event_consolidation.cache") as mock_cache:
            mock_cache.get.return_value = None
            result = service.should_send_notification(
                event_type="subscription_created",
                customer_id="cus_123",
                workspace_id="ws_456",
                amount=0.0,
            )
        assert result is True

    def test_payment_failure_not_affected_by_zero_filter(
        self, service: EventConsolidationService
    ) -> None:
        """Test that payment_failure is not affected by zero amount filter."""
        with patch("webhooks.services.event_consolidation.cache") as mock_cache:
            mock_cache.get.return_value = None
            result = service.should_send_notification(
                event_type="payment_failure",
                customer_id="cus_123",
                workspace_id="ws_456",
                amount=0.0,
            )
        assert result is True


class TestEventConsolidationConstants:
    """Test EventConsolidationService constants."""

    def test_consolidation_window_is_reasonable(self) -> None:
        """Test that consolidation window is a reasonable value.

        Window is 5 minutes (300s) to handle Stripe's delayed event delivery
        where related events can arrive 3-4+ minutes apart.
        """
        assert EventConsolidationService.CONSOLIDATION_WINDOW_SECONDS >= 60
        assert EventConsolidationService.CONSOLIDATION_WINDOW_SECONDS <= 600

    def test_never_suppress_includes_critical_events(self) -> None:
        """Test that critical events are in NEVER_SUPPRESS."""
        never_suppress = EventConsolidationService.NEVER_SUPPRESS

        assert "payment_failure" in never_suppress
        assert "payment_action_required" in never_suppress
        # trial_ending is delivered as its own warning; the later "Trial
        # converted" insight is derived statelessly from the invoice payload
        assert "trial_ending" in never_suppress

    def test_trial_ending_not_a_suppression_target(self) -> None:
        """Test that no primary event suppresses trial_ending anymore."""
        for suppressed in EventConsolidationService.PRIMARY_EVENTS.values():
            assert "trial_ending" not in suppressed

    def test_primary_events_defined(self) -> None:
        """Test that primary events are properly defined."""
        primary = EventConsolidationService.PRIMARY_EVENTS

        assert "subscription_created" in primary
        assert "payment_success" in primary["subscription_created"]
        assert "invoice_paid" in primary["subscription_created"]

    def test_shopify_order_created_in_primary_events(self) -> None:
        """Test that Shopify order_created is defined as a primary event."""
        primary = EventConsolidationService.PRIMARY_EVENTS

        assert "order_created" in primary
        assert "payment_success" in primary["order_created"]

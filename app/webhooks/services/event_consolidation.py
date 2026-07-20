"""Event consolidation service for preventing notification spam.

This module provides a service to consolidate related webhook events
that arrive in quick succession for the same customer. For example,
when a Stripe subscription is created, multiple events fire:
- subscription_created
- invoice.payment_succeeded
- invoice.paid

Without consolidation, this results in 3 separate Slack notifications.
This service tracks recent events and suppresses redundant ones.
"""

import logging
from typing import Any, ClassVar

from django.core.cache import cache

logger = logging.getLogger(__name__)


class EventConsolidationService:
    """Consolidate related webhook events to prevent notification spam.

    Uses Django's cache to track recent events per customer/workspace.
    When a "primary" event is processed, subsequent "secondary" events
    within the consolidation window are suppressed.

    Note on race conditions: The suppression list update (get + set) is not
    atomic. If two primary events for the same customer arrive simultaneously,
    one's suppression list could be partially lost. This is acceptable because:
    1. It's a rare edge case (sub-second timing for same customer)
    2. The worst outcome is an extra notification, not a missed one
    3. Using atomic operations would add complexity for minimal benefit

    Attributes:
        CONSOLIDATION_WINDOW_SECONDS: Time window for event consolidation.
        DEDUP_WINDOW_MULTIPLIER: Multiplier for deduplication window vs consolidation.
        PRIMARY_EVENTS: Mapping of primary events to events they suppress.
        NEVER_SUPPRESS: Events that should always send notifications.
        ZERO_AMOUNT_FILTER_EVENTS: Payment events filtered when amount is $0.
    """

    # Time window (in seconds) during which related events are consolidated.
    # 5 minutes to handle Stripe's delayed event delivery (events can
    # arrive 3-4+ minutes apart for the same user action).
    CONSOLIDATION_WINDOW_SECONDS: ClassVar[int] = 300

    # Deduplication window is longer than consolidation to catch delayed retries
    # Multiplier of 6 means 30 minutes for dedup vs 5 minutes for consolidation
    DEDUP_WINDOW_MULTIPLIER: ClassVar[int] = 6

    # Events that suppress other events when processed first
    # Format: {primary_event: {events_to_suppress}}
    PRIMARY_EVENTS: ClassVar[dict[str, set[str]]] = {
        # Stripe: New subscription suppresses the payment notifications that follow
        "subscription_created": {"payment_success", "invoice_paid"},
        # Stripe: Subscription deletion suppresses final invoice notification
        "subscription_deleted": {"invoice_paid"},
        # Stripe: Checkout completion suppresses payment notifications
        "checkout_completed": {"payment_success", "invoice_paid"},
        # Shopify: Order creation suppresses the payment notification that follows
        "order_created": {"payment_success"},
    }

    # Events that should never be suppressed (always important)
    # trial_ending is delivered as its own "trial ending in N days" warning;
    # the later "Trial converted" insight is derived statelessly from the
    # first post-trial invoice payload (see StripePlugin), so both
    # notifications fire for a full trial lifecycle.
    NEVER_SUPPRESS: ClassVar[set[str]] = {
        "payment_failure",
        "payment_action_required",
        "trial_ending",
    }

    # Metadata fields that identify the transaction an event belongs to,
    # in preference order. Used to scope suppression so a primary event
    # only suppresses secondaries for the SAME transaction.
    CORRELATION_METADATA_FIELDS: ClassVar[tuple[str, ...]] = (
        "subscription_id",
        "invoice_id",
        "charge_id",
        "order_id",
    )

    # Payment events that should be suppressed when amount is $0 (trial invoices)
    ZERO_AMOUNT_FILTER_EVENTS: ClassVar[set[str]] = {
        "payment_success",
        "invoice_paid",
    }

    def __init__(self) -> None:
        """Initialize the consolidation service."""
        pass

    def _get_cache_key(
        self, workspace_id: str, customer_id: str, event_type: str
    ) -> str:
        """Generate cache key for event tracking.

        Args:
            workspace_id: The workspace UUID.
            customer_id: The customer identifier.
            event_type: The event type.

        Returns:
            Cache key string.
        """
        return f"event_consolidation:{workspace_id}:{customer_id}:{event_type}"

    def _get_suppression_key(
        self,
        workspace_id: str,
        customer_id: str,
        correlation_id: str,
    ) -> str:
        """Generate cache key for tracking which events to suppress.

        Suppression is always scoped to the transaction-level correlator
        (e.g. subscription_id or invoice_id), so a primary event for one
        transaction never suppresses events belonging to an unrelated
        transaction for the same customer. Events without a correlator
        never participate in suppression (see should_send_notification).

        Args:
            workspace_id: The workspace UUID.
            customer_id: The customer identifier.
            correlation_id: Transaction-level correlator.

        Returns:
            Cache key string for suppression tracking.
        """
        return f"event_suppress:{workspace_id}:{customer_id}:{correlation_id}"

    def extract_correlation_id(self, event_data: dict[str, Any]) -> str | None:
        """Extract the transaction-level correlator from a parsed event.

        Prefers explicit metadata identifiers (subscription_id, invoice_id,
        charge_id, order_id) and falls back to the event's external object
        id. Related events for the same transaction (e.g. a subscription and
        its invoices) share the metadata identifier, while unrelated
        transactions for the same customer do not.

        Args:
            event_data: Parsed event data dictionary.

        Returns:
            Correlator string, or None if the event carries none.
        """
        metadata = event_data.get("metadata") or {}
        for field in self.CORRELATION_METADATA_FIELDS:
            value = metadata.get(field)
            if value:
                return str(value)

        external_id = event_data.get("external_id")
        if external_id:
            return str(external_id)

        return None

    def should_send_notification(
        self,
        event_type: str,
        customer_id: str,
        workspace_id: str,
        amount: float | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """Check if notification should be sent or suppressed.

        This method:
        1. Filters out $0 payment events (trial invoices, etc.)
        2. Never suppresses critical events (payment failures, etc.)
        3. Checks if this event type should be suppressed due to a recent primary event
        4. If this is a primary event, marks secondary events for suppression

        Args:
            event_type: The normalized event type (e.g., "subscription_created").
            customer_id: The customer identifier.
            workspace_id: The workspace UUID.
            amount: Optional payment amount for filtering zero-amount events.
            correlation_id: Transaction-level correlator (subscription_id,
                invoice_id, charge_id, ...). Suppression only applies
                between events sharing the same correlator; without one the
                event is always delivered (never suppressed and never
                suppressing), since a coarse customer-level fallback could
                swallow an unrelated transaction's notification.

        Returns:
            True if notification should be sent, False if it should be suppressed.
        """
        # Filter $0 payment events (trial invoices create noise)
        if event_type in self.ZERO_AMOUNT_FILTER_EVENTS:
            if amount is None or amount <= 0:
                logger.info(
                    f"Suppressing {event_type} with zero/no amount "
                    f"for customer {customer_id} in workspace {workspace_id}"
                )
                return False

        if not customer_id or not workspace_id:
            # Can't consolidate without identifiers, so allow the notification
            return True

        # Never suppress critical events
        if event_type in self.NEVER_SUPPRESS:
            logger.debug(
                f"Event {event_type} is in NEVER_SUPPRESS list, allowing notification"
            )
            return True

        # Without a transaction-level correlator we cannot tell which
        # transaction a suppression marker belongs to. Deliver rather than
        # risk suppressing (or being suppressed by) an unrelated transaction.
        if not correlation_id:
            logger.debug(
                f"Event {event_type} has no transaction correlator, "
                f"skipping consolidation suppression"
            )
            return True

        # Check if this event should be suppressed. Only a primary event
        # for the SAME transaction (same correlator) can suppress this one.
        suppression_key = self._get_suppression_key(
            workspace_id, customer_id, correlation_id
        )
        suppressed_events = cache.get(suppression_key) or set()

        if event_type in suppressed_events:
            logger.info(
                f"Suppressing {event_type} notification for customer {customer_id} "
                f"in workspace {workspace_id} (consolidation with primary event)"
            )
            return False

        # If this is a primary event, mark secondary events for suppression
        if event_type in self.PRIMARY_EVENTS:
            events_to_suppress = self.PRIMARY_EVENTS[event_type]
            self._mark_events_for_suppression(
                workspace_id, customer_id, events_to_suppress, correlation_id
            )
            logger.debug(
                f"Primary event {event_type} processed, marking {events_to_suppress} "
                f"for suppression"
            )

        return True

    def _mark_events_for_suppression(
        self,
        workspace_id: str,
        customer_id: str,
        events_to_suppress: set[str],
        correlation_id: str,
    ) -> None:
        """Mark events for suppression within the consolidation window.

        Args:
            workspace_id: The workspace UUID.
            customer_id: The customer identifier.
            events_to_suppress: Set of event types to suppress.
            correlation_id: Transaction-level correlator scoping the
                suppression to a single transaction.
        """
        suppression_key = self._get_suppression_key(
            workspace_id, customer_id, correlation_id
        )

        # Get existing suppressed events and merge
        existing = cache.get(suppression_key) or set()
        updated = existing | events_to_suppress

        # Store with TTL
        cache.set(
            suppression_key,
            updated,
            timeout=self.CONSOLIDATION_WINDOW_SECONDS,
        )

    def record_event(
        self,
        event_type: str,
        customer_id: str,
        workspace_id: str,
        external_id: str | None = None,
    ) -> None:
        """Record that an event was processed (for deduplication).

        This can be used for exact deduplication based on external_id,
        in addition to the type-based consolidation.

        Args:
            event_type: The event type.
            customer_id: The customer identifier.
            workspace_id: The workspace UUID.
            external_id: Optional external event ID for exact deduplication.
        """
        if external_id:
            dedup_key = f"event_dedup:{workspace_id}:{external_id}"
            dedup_timeout = (
                self.CONSOLIDATION_WINDOW_SECONDS * self.DEDUP_WINDOW_MULTIPLIER
            )
            cache.set(dedup_key, True, timeout=dedup_timeout)

    def is_duplicate(
        self,
        workspace_id: str,
        external_id: str | None,
    ) -> bool:
        """Check if an event with this external_id was already processed.

        Args:
            workspace_id: The workspace UUID.
            external_id: The external event ID.

        Returns:
            True if this is a duplicate event.
        """
        if not external_id:
            return False

        dedup_key = f"event_dedup:{workspace_id}:{external_id}"
        return cache.get(dedup_key) is not None


# Global instance for convenience
event_consolidation_service = EventConsolidationService()

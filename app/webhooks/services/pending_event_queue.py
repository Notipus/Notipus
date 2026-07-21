"""Pending event queue for delayed webhook processing.

This module implements a delayed processing system for Stripe webhooks.
Events are queued and processed after a delay to allow related events
(e.g., subscription.created and invoice.paid) to arrive before sending
a single consolidated notification.

The delay ensures we have complete data (like customer_email from invoice
events) even when processing subscription events that arrive first.

Delivery reliability: once an event is queued the router has already
acknowledged it to the provider (and recorded a dedup marker that
suppresses provider retries), so delivery is entirely our responsibility.
Failed sends leave the events in Redis; a periodic recovery sweep (plus a
sweep at server startup for ephemeral infrastructure) retries them until
either delivery succeeds, MAX_SEND_ATTEMPTS is exhausted, or the retry
window (TTL_SECONDS) expires.

Thread Safety:
- Uses cache.add (atomic SET NX on Redis) for distributed locking, both
  for event processing and for appending to the pending-event list
- Timer scheduling uses threading.Lock for in-process safety
"""

import copy
import logging
import threading
import time
import uuid
from typing import Any, ClassVar, cast

from core.encrypted_cache import decrypt_cache_value, encrypt_cache_value
from core.models import Integration, Workspace
from django.conf import settings
from django.core.cache import cache
from django.db import connections

from .redis_client import get_raw_redis_client

logger = logging.getLogger(__name__)

# Minimum age (in seconds) before an orphaned event is processed by a
# recovery sweep. This prevents processing events that were just queued
# and have active timers.
ORPHAN_MIN_AGE_SECONDS = 35  # Slightly longer than DELAY_SECONDS

# Lock TTL for distributed processing lock (seconds)
# Should be longer than max expected processing time
PROCESSING_LOCK_TTL = 60

# Maximum retries for storing events (lock contention)
MAX_STORE_RETRIES = 3

# Distributed append lock: TTL bounds how long a crashed lock holder can
# block appends; attempts * delay bounds how long a request waits.
APPEND_LOCK_TTL_SECONDS = 5
APPEND_LOCK_MAX_ATTEMPTS = 20
APPEND_LOCK_RETRY_DELAY_SECONDS = 0.05

# Finalization (removing sent items) runs in worker threads with no
# request waiting, so it can afford a budget longer than the lock TTL:
# even a crashed lock holder expires within the wait, keeping the
# delete-outright fallback for truly pathological cases only.
APPEND_LOCK_FINALIZE_MAX_ATTEMPTS = 120  # 120 * 0.05s = 6s > 5s TTL

# How often the background sweep retries undelivered events. Combined
# with TTL_SECONDS this defines the delivery-retry schedule.
RECOVERY_SWEEP_INTERVAL_SECONDS = 300

# Give up (loudly) after this many failed delivery attempts for one
# event group - e.g. a revoked Slack webhook URL that will never succeed.
MAX_SEND_ATTEMPTS = 20

# Fleet-wide marker so only one worker runs each periodic sweep.
SWEEP_LOCK_KEY = "pending_webhook_sweep_lock"


class PendingEventQueue:
    """Queue for delayed processing of webhook events.

    When Stripe events arrive, they share an idempotency_key for related
    events (subscription.created, invoice.paid, invoice.payment_succeeded).
    This queue collects all events with the same key and processes them
    together after a delay, ensuring we have complete data for notifications.

    Attributes:
        DELAY_SECONDS: Time to wait before processing (default 30s).
        TTL_SECONDS: Redis TTL for pending events. This is the delivery
            retry window: once queued, the provider's own retries are
            suppressed (dedup marker), so it must comfortably exceed the
            aggregation delay AND give the periodic recovery sweep time
            to retry failed sends through e.g. a Slack outage.
    """

    DELAY_SECONDS = 30
    TTL_SECONDS = 6 * 60 * 60  # 6h delivery-retry window

    # Track active timers to avoid duplicate scheduling
    # Key: "{workspace_id}:{idempotency_key}" -> Timer
    _active_timers: dict[str, threading.Timer] = {}
    _lock = threading.Lock()

    # Per-process guard so the periodic recovery thread starts only once
    _recovery_thread_started = False

    def queue_event(
        self,
        idempotency_key: str,
        workspace_id: str,
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
        provider_name: str,
        workspace: Workspace | None,
    ) -> None:
        """Store event and schedule processing after delay.

        Args:
            idempotency_key: Stripe idempotency key shared by related events,
                or customer-based key (format: "customer:{customer_id}").
            workspace_id: Workspace UUID string.
            event_data: Parsed event data from the webhook.
            customer_data: Customer data extracted from webhook.
            provider_name: Name of the provider (e.g., "stripe").
            workspace: Workspace model instance (can be None for global).
        """
        # For customer-based keys (without Stripe idempotency key), add time bucket
        # to group related events within a 60-second window
        is_customer_key = idempotency_key.startswith("customer:")
        if is_customer_key:
            storage_key = self._get_customer_storage_key(idempotency_key, workspace_id)
        else:
            storage_key = idempotency_key

        # Store event in Redis
        self._store_event(storage_key, workspace_id, event_data, customer_data)

        # Schedule processing (only if not already scheduled)
        self._schedule_processing(storage_key, workspace_id, provider_name, workspace)

        logger.debug(
            f"Queued event {event_data.get('type')} for key "
            f"{storage_key} in workspace {workspace_id}"
        )

    def _get_customer_storage_key(self, idempotency_key: str, workspace_id: str) -> str:
        """Get storage key for customer-based aggregation.

        Uses 60-second time buckets to group related events. To handle events
        arriving at bucket boundaries (e.g., subscription at T=59s, invoice at
        T=61s), we check if there's an existing key in the previous bucket and
        use that if found.

        Args:
            idempotency_key: Customer-based key (format: "customer:{customer_id}").
            workspace_id: Workspace UUID string.

        Returns:
            Storage key with time bucket suffix.
        """
        current_bucket = int(time.time() // 60)
        previous_bucket = current_bucket - 1

        # Check if there's an existing aggregation in the previous bucket
        # (handles events arriving at bucket boundaries)
        prev_key = f"{idempotency_key}:t{previous_bucket}"
        prev_redis_key = f"pending_webhook:{workspace_id}:{prev_key}"

        if cache.get(prev_redis_key):
            logger.debug(f"Found existing events in previous bucket, using {prev_key}")
            return prev_key

        # No existing events in previous bucket, use current bucket
        return f"{idempotency_key}:t{current_bucket}"

    def _store_event(
        self,
        idempotency_key: str,
        workspace_id: str,
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
    ) -> None:
        """Store event to Redis keyed by idempotency_key.

        Uses a distributed lock to prevent concurrent appends from
        overwriting each other. Raises on persistent failure so the
        router returns 5xx and the provider redelivers the webhook
        (the dedup marker is only recorded after successful queueing).

        Args:
            idempotency_key: Stripe idempotency key.
            workspace_id: Workspace UUID string.
            event_data: Parsed event data.
            customer_data: Customer data extracted from webhook.
        """
        key = f"pending_webhook:{workspace_id}:{idempotency_key}"

        # Add timestamp for orphan recovery age checking
        event_data_with_ts = event_data.copy()
        event_data_with_ts["_queued_at"] = time.time()

        new_item = {
            "event_data": event_data_with_ts,
            "customer_data": customer_data,
        }

        for attempt in range(MAX_STORE_RETRIES):
            try:
                self._locked_append(key, new_item)
                return
            except Exception as e:
                if attempt == MAX_STORE_RETRIES - 1:
                    logger.error(
                        f"Failed to store event after {MAX_STORE_RETRIES} attempts: {e}"
                    )
                    raise
                # Small backoff before retry
                time.sleep(0.01 * (attempt + 1))

    def _locked_append(self, key: str, item: dict[str, Any]) -> None:
        """Append an item to the cached list under a distributed lock.

        cache.add is an atomic SET NX on Redis, giving cross-process
        mutual exclusion through the public cache API. This works with
        Django's built-in Redis backend - whose keys are prefixed and
        pickled, so a raw-client WATCH/MULTI transaction cannot see
        them - as well as any other cache backend.

        Args:
            key: Cache key for the list.
            item: Item to append.

        Raises:
            RuntimeError: If the append lock could not be acquired.
        """
        token = self._acquire_append_lock(key)
        if token is None:
            raise RuntimeError(f"Could not acquire append lock for {key}")
        try:
            existing = decrypt_cache_value(cache.get(key)) or []
            existing.append(item)
            # Pending payloads carry customer PII (emails, names, order
            # data) and must be encrypted at rest in Redis.
            cache.set(key, encrypt_cache_value(existing), timeout=self.TTL_SECONDS)
        finally:
            self._release_append_lock(key, token)

    def _acquire_append_lock(
        self, key: str, max_attempts: int = APPEND_LOCK_MAX_ATTEMPTS
    ) -> str | None:
        """Try to acquire the distributed append lock for a pending list.

        The lock value is a per-acquisition token so release can verify
        ownership: without it, a holder delayed past the lock TTL (GC
        pause, slow cache) would unconditionally delete the lock a
        successor has since acquired, reintroducing concurrent writers.

        Args:
            key: Cache key of the pending list the lock protects.
            max_attempts: Acquisition attempts before giving up. Callers
                on the request path keep the short default; callers that
                can wait (finalization) pass a budget exceeding the lock
                TTL so a crashed holder's lock expires within it.

        Returns:
            The ownership token when acquired, None when all attempts
            timed out.
        """
        lock_key = f"append_lock:{key}"
        token = uuid.uuid4().hex
        for _ in range(max_attempts):
            if cache.add(lock_key, token, timeout=APPEND_LOCK_TTL_SECONDS):
                return token
            time.sleep(APPEND_LOCK_RETRY_DELAY_SECONDS)
        return None

    def _release_append_lock(self, key: str, token: str) -> None:
        """Release the distributed append lock if still owned.

        Compare-then-delete through the cache API is not atomic (a truly
        atomic Lua compare-and-delete would mean reimplementing the
        backend's key prefixing and value serialization against raw
        Redis), so a successor acquiring between the get and the delete
        can still lose its lock - but only within that microsecond
        window, versus the unconditional delete which clobbered the
        successor whenever THIS holder ran past the lock TTL. The append
        lock guards a ~millisecond read-modify-write, so shrinking the
        exposure to the compare window is the practical fix.

        Args:
            key: Cache key of the pending list the lock protects.
            token: Ownership token returned by ``_acquire_append_lock``.
        """
        lock_key = f"append_lock:{key}"
        if cache.get(lock_key) == token:
            cache.delete(lock_key)

    def _schedule_processing(
        self,
        idempotency_key: str,
        workspace_id: str,
        provider_name: str,
        workspace: Workspace | None,
    ) -> None:
        """Schedule a timer to process events after DELAY_SECONDS.

        Only schedules if no timer is already active for this key.

        Args:
            idempotency_key: Stripe idempotency key.
            workspace_id: Workspace UUID string.
            provider_name: Name of the provider.
            workspace: Workspace model instance.
        """
        timer_key = f"{workspace_id}:{idempotency_key}"

        with self._lock:
            if timer_key in self._active_timers:
                # Timer already scheduled for this idempotency_key
                return

            timer = threading.Timer(
                self.DELAY_SECONDS,
                self._process_events_in_thread,
                args=[idempotency_key, workspace_id, provider_name, workspace],
            )
            timer.daemon = True  # Don't block shutdown
            timer.start()

            self._active_timers[timer_key] = timer

            logger.info(
                f"Scheduled processing in {self.DELAY_SECONDS}s for "
                f"idempotency_key {idempotency_key}"
            )

    def _process_events_in_thread(
        self,
        idempotency_key: str,
        workspace_id: str,
        provider_name: str,
        workspace: Workspace | None,
    ) -> None:
        """Run ``_process_events`` in a timer thread, then close DB connections.

        Timer threads die right after this call; without an explicit
        close, the ORM connection each one opened is never returned to
        PostgreSQL, leaking one server connection per processed group.

        Args:
            idempotency_key: Stripe idempotency key.
            workspace_id: Workspace UUID string.
            provider_name: Name of the provider.
            workspace: Workspace model instance.
        """
        try:
            self._process_events(
                idempotency_key, workspace_id, provider_name, workspace
            )
        finally:
            connections.close_all()

    def _process_events(
        self,
        idempotency_key: str,
        workspace_id: str,
        provider_name: str,
        workspace: Workspace | None,
    ) -> None:
        """Process all queued events for an idempotency_key.

        Called by timer after delay. Aggregates events and sends one notification.

        Uses distributed locking to prevent multiple servers from processing
        the same events simultaneously.

        Args:
            idempotency_key: Stripe idempotency key.
            workspace_id: Workspace UUID string.
            provider_name: Name of the provider.
            workspace: Workspace model instance.
        """
        timer_key = f"{workspace_id}:{idempotency_key}"

        # Clean up timer reference
        with self._lock:
            self._active_timers.pop(timer_key, None)

        # Try to acquire distributed lock
        lock_key = f"processing_lock:{workspace_id}:{idempotency_key}"
        if not self._acquire_lock(lock_key):
            logger.info(
                f"Another process is handling idempotency_key {idempotency_key}, "
                f"skipping"
            )
            return

        try:
            # Get all stored events
            key = f"pending_webhook:{workspace_id}:{idempotency_key}"
            stored_items = self._read_pending_items(key)
            if stored_items is None:
                return  # Poisoned entry purged; already logged loudly

            if not stored_items:
                logger.warning(
                    f"No events found for idempotency_key {idempotency_key} "
                    f"(may have expired or already processed)"
                )
                return

            logger.info(
                f"Processing {len(stored_items)} events for idempotency_key "
                f"{idempotency_key}"
            )

            # Aggregate events into ONE notification
            aggregated_event, aggregated_customer = self._aggregate_events(stored_items)

            # Send notification - only delete events if successful
            success = self._send_notification(
                aggregated_event, aggregated_customer, provider_name, workspace
            )

            attempts_key = f"pending_webhook_attempts:{workspace_id}:{idempotency_key}"
            if success:
                # Remove exactly the items that were sent; events appended
                # by another worker mid-send stay queued for their own cycle.
                remainder = self._remove_processed_items(key, stored_items)
                cache.delete(attempts_key)
                if remainder:
                    self._schedule_processing(
                        idempotency_key, workspace_id, provider_name, workspace
                    )
            else:
                # Leave events for the periodic recovery sweep, but give
                # up loudly on groups that will never deliver (e.g. a
                # revoked Slack webhook URL) instead of retrying forever.
                attempts = self._record_failed_attempt(attempts_key)
                if attempts >= MAX_SEND_ATTEMPTS:
                    logger.error(
                        f"Giving up on notification for {idempotency_key} after "
                        f"{attempts} failed delivery attempts; dropping events"
                    )
                    # Drop only what we tried to send; a mid-send append is
                    # a new group and gets a fresh attempt budget.
                    remainder = self._remove_processed_items(key, stored_items)
                    cache.delete(attempts_key)
                    if remainder:
                        self._schedule_processing(
                            idempotency_key, workspace_id, provider_name, workspace
                        )
                else:
                    logger.warning(
                        f"Notification failed for {idempotency_key}, events left "
                        f"for retry (attempt {attempts}/{MAX_SEND_ATTEMPTS})"
                    )
        finally:
            # Always release the lock
            self._release_lock(lock_key)

    def _remove_processed_items(
        self, key: str, processed_items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Atomically remove the processed items from the pending list.

        The read-aggregate-send sequence runs without the append lock (a
        Slack HTTP call is far too slow to hold it), so another worker
        can append an event to the list mid-send. Deleting the key
        outright would drop that event unprocessed. Under the append
        lock, remove exactly the items that were aggregated and keep
        anything that arrived after the snapshot. Items are compared by
        value, removing ONE occurrence per processed item so duplicates
        with identical content keep their multiplicity (``_queued_at``
        timestamps make equal-looking events from distinct webhooks
        distinguishable in practice, but this does not rely on it).

        Args:
            key: Cache key of the pending list.
            processed_items: Snapshot of the items that were aggregated
                and sent (or given up on).

        Returns:
            Items still queued (appended during the send), empty when the
            list is fully drained.
        """
        token = self._acquire_append_lock(
            key, max_attempts=APPEND_LOCK_FINALIZE_MAX_ATTEMPTS
        )
        if token is None:
            # The finalize budget outlasts the lock TTL, so this means
            # sustained contention, not one crashed holder. Fall back to
            # the pre-lock behavior of deleting the whole key rather than
            # leaving the group to be resent forever by the recovery sweep.
            logger.warning(
                f"Could not acquire append lock to finalize {key}; "
                f"deleting the pending list outright"
            )
            cache.delete(key)
            return []
        try:
            current = decrypt_cache_value(cache.get(key)) or []
            remainder = list(current)
            for item in processed_items:
                try:
                    remainder.remove(item)
                except ValueError:
                    # Already gone (e.g. the list was rewritten after a
                    # TTL expiry); nothing to remove for this item.
                    pass
            if remainder:
                cache.set(key, encrypt_cache_value(remainder), timeout=self.TTL_SECONDS)
            else:
                cache.delete(key)
            return remainder
        finally:
            self._release_append_lock(key, token)

    def _read_pending_items(self, key: str) -> list[dict[str, Any]] | None:
        """Read and decrypt the pending list, purging poisoned entries.

        ``decrypt_cache_value`` returns None both for a true miss and for
        a ciphertext token no configured key can decrypt (e.g. the key
        was rotated out of ``FIELD_ENCRYPTION_KEYS`` before the entry
        drained). The two must not be conflated: a poisoned entry would
        otherwise sit in Redis until TTL expiry, logging "no events
        found" on every timer/sweep pass while its notifications are
        silently lost. Purge it (and its attempt counter) loudly instead
        - it can never be processed.

        Args:
            key: Cache key of the pending list
                ("pending_webhook:{workspace}:{idempotency_key}").

        Returns:
            The decrypted item list ([] when the key is absent), or None
            when a poisoned entry was found and purged.
        """
        raw = cache.get(key)
        # log_failures=False: the key-specific error below covers the
        # poisoned case; the generic warning would just duplicate it.
        items = decrypt_cache_value(raw, log_failures=False)
        if raw is not None and items is None:
            logger.error(
                f"Pending events under {key} could not be decrypted with any "
                f"configured key (FIELD_ENCRYPTION_KEYS rotated too early?). "
                f"Dropping the undecryptable entry; its notifications are lost."
            )
            cache.delete(key)
            cache.delete(
                key.replace("pending_webhook:", "pending_webhook_attempts:", 1)
            )
            return None
        return items or []

    def _record_failed_attempt(self, attempts_key: str) -> int:
        """Increment and return the failed-delivery counter for a group.

        The counter lives as long as the pending events themselves so a
        group's attempts are bounded across process restarts.

        Args:
            attempts_key: Cache key of the attempt counter.

        Returns:
            The updated number of failed attempts.
        """
        try:
            attempts = int(cache.get(attempts_key) or 0) + 1
        except (TypeError, ValueError):
            attempts = 1
        cache.set(attempts_key, attempts, timeout=self.TTL_SECONDS)
        return attempts

    def _acquire_lock(self, lock_key: str) -> bool:
        """Acquire a distributed lock using Redis SETNX.

        Args:
            lock_key: Key for the lock.

        Returns:
            True if lock was acquired, False if already held by another process.
        """
        # Use cache.add() which is atomic (SETNX equivalent)
        return cache.add(lock_key, "locked", timeout=PROCESSING_LOCK_TTL)

    def _release_lock(self, lock_key: str) -> None:
        """Release a distributed lock.

        Args:
            lock_key: Key for the lock.
        """
        try:
            cache.delete(lock_key)
        except Exception as e:
            logger.warning(f"Failed to release lock {lock_key}: {e}")

    # Event type priority for aggregation (higher = preferred)
    TYPE_PRIORITY: ClassVar[dict[str, int]] = {
        "trial_started": 100,
        "subscription_created": 90,
        "subscription_updated": 80,
        "subscription_deleted": 80,
        "checkout_completed": 70,
        "payment_failure": 60,  # Never dropped: folded into winner's metadata
        "payment_success": 50,
        "invoice_paid": 40,
    }

    # Failure details preserved when a payment_failure loses the priority
    # contest (e.g. to subscription_created in the same idempotency bucket)
    FAILURE_METADATA_FIELDS: ClassVar[tuple[str, ...]] = (
        "failure_reason",
        "decline_code",
        "attempt_count",
        "next_payment_attempt",
    )

    def _aggregate_events(
        self, stored_items: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Combine multiple events, prioritizing best data.

        Priority rules:
        - event: The FULL event (type, amount, external_id, currency,
          metadata, ...) is taken from the highest-priority event so the
          notification never mixes fields from unrelated events.
        - customer_email: Take from ANY event that has it (invoice events
          have it, subscription events don't).
        - payment_failure: Never silently dropped. If a failure shares the
          bucket with a higher-priority event (e.g. subscription created
          with an immediately-declining card), its details are merged into
          the winner's metadata so one richer notification surfaces both.

        Args:
            stored_items: List of stored items with event_data and customer_data.

        Returns:
            Tuple of (aggregated_event_data, aggregated_customer_data).
        """
        if not stored_items:
            return {}, {}

        # Pick the highest-priority event as the winner (first wins ties)
        # and copy it in FULL - amount, external_id, currency and metadata
        # must all come from the same event, not from stored_items[0].
        winner = max(
            stored_items,
            key=lambda item: self.TYPE_PRIORITY.get(
                item["event_data"].get("type", ""), 0
            ),
        )
        result_event = winner["event_data"].copy()
        # Always give the result its own metadata dict: a shared reference
        # (or a None/missing value) must never leak into later mutation by
        # _merge_payment_failure or downstream consumers. Deep copy because
        # metadata can hold nested structures (e.g. Shopify line_items).
        winner_metadata = winner["event_data"].get("metadata")
        result_event["metadata"] = (
            copy.deepcopy(winner_metadata) if isinstance(winner_metadata, dict) else {}
        )
        result_customer = winner["customer_data"].copy()

        for item in stored_items:
            if item is winner:
                continue
            event_data = item["event_data"]
            customer_data = item["customer_data"]

            # Priority: get email from ANY event that has it
            # (invoice events have customer_email, subscription events don't)
            if customer_data.get("email") and not result_customer.get("email"):
                result_customer["email"] = customer_data["email"]
                logger.debug(
                    f"Found customer email from {event_data.get('type')}: "
                    f"{customer_data['email']}"
                )

            # Also check event_data for customer_email (from raw webhook)
            if event_data.get("customer_email") and not result_customer.get("email"):
                result_customer["email"] = event_data["customer_email"]

            # Merge other customer data if missing
            for field in ["first_name", "last_name", "company_name"]:
                if customer_data.get(field) and not result_customer.get(field):
                    result_customer[field] = customer_data[field]

        self._merge_payment_failure(result_event, stored_items)

        logger.info(
            f"Aggregated {len(stored_items)} events: type={result_event.get('type')}, "
            f"email={result_customer.get('email') or 'MISSING'}"
        )

        self._warn_if_missing_email(result_event, result_customer, stored_items)

        return result_event, result_customer

    def _merge_payment_failure(
        self,
        result_event: dict[str, Any],
        stored_items: list[dict[str, Any]],
    ) -> None:
        """Fold a losing payment_failure's details into the winning event.

        When subscription.created and invoice.payment_failed share an
        idempotency bucket, the subscription event wins the type contest but
        the failure must still be surfaced. Marks the winner's metadata with
        ``has_payment_failure`` plus the failure details so the notification
        (via InsightDetector) reports "new subscription - but the initial
        payment failed" instead of a bare "New subscription!".

        Args:
            result_event: Aggregated event data (mutated).
            stored_items: Original list of stored items.
        """
        if result_event.get("type") == "payment_failure":
            return  # The failure IS the notification

        failure_items = [
            item
            for item in stored_items
            if item["event_data"].get("type") == "payment_failure"
        ]
        if not failure_items:
            return

        failure_event = failure_items[0]["event_data"]
        failure_metadata = failure_event.get("metadata")
        if not isinstance(failure_metadata, dict):
            failure_metadata = {}

        metadata = result_event.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            result_event["metadata"] = metadata

        metadata["has_payment_failure"] = True
        for field in self.FAILURE_METADATA_FIELDS:
            if failure_metadata.get(field) is not None:
                metadata[field] = failure_metadata[field]
        # `is not None` (not truthiness): a legitimate 0.0 must be kept
        if failure_event.get("amount") is not None:
            metadata["failed_amount"] = failure_event["amount"]

        logger.info(
            f"Merged payment_failure into {result_event.get('type')} notification "
            f"(reason: {failure_metadata.get('failure_reason', 'unknown')})"
        )

    def _warn_if_missing_email(
        self,
        result_event: dict[str, Any],
        result_customer: dict[str, Any],
        stored_items: list[dict[str, Any]],
    ) -> None:
        """Log warning if subscription/trial event has no email after aggregation.

        Args:
            result_event: Aggregated event data.
            result_customer: Aggregated customer data.
            stored_items: Original list of stored items for diagnostic info.
        """
        if result_customer.get("email"):
            return

        event_type = result_event.get("type", "")
        if event_type not in ("trial_started", "subscription_created"):
            return

        event_types = [item["event_data"].get("type") for item in stored_items]
        emails_found = [
            item["customer_data"].get("email", "none") or "none"
            for item in stored_items
        ]
        logger.warning(
            f"No customer email found for {event_type} after aggregating "
            f"{len(stored_items)} events. Event types: {event_types}, "
            f"Emails checked: {emails_found}"
        )

    def _send_notification(
        self,
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
        provider_name: str,
        workspace: Workspace | None,
    ) -> bool:
        """Build and send notification to Slack.

        Args:
            event_data: Aggregated event data.
            customer_data: Aggregated customer data.
            provider_name: Name of the provider.
            workspace: Workspace model instance.

        Returns:
            True if notification was sent successfully (or suppressed),
            False if there was a failure that should be retried.
        """
        from plugins.base import PluginType
        from plugins.destinations.base import BaseDestinationPlugin
        from plugins.registry import PluginRegistry

        from .event_consolidation import event_consolidation_service

        event_type = event_data.get("type", "")
        customer_id = event_data.get("customer_id", "")
        workspace_id = str(workspace.uuid) if workspace else ""
        external_id = event_data.get("external_id", "")

        # Add workspace_id to event_data for insight detection (tenant-scoped
        # anniversary dedup), mirroring webhook_router._process_immediately
        event_data["workspace_id"] = workspace_id

        # Check if this event should be suppressed due to consolidation
        # (e.g., $0 trial invoices). Suppression is scoped to the
        # transaction-level correlator so an unrelated second transaction
        # for the same customer is never suppressed.
        should_notify = event_consolidation_service.should_send_notification(
            event_type=event_type,
            customer_id=customer_id,
            workspace_id=workspace_id,
            amount=event_data.get("amount"),
            correlation_id=event_consolidation_service.extract_correlation_id(
                event_data
            ),
        )

        if not should_notify:
            logger.info(
                f"Suppressing notification for {event_type} (consolidated/filtered)"
            )
            event_consolidation_service.record_event(
                event_type=event_type,
                customer_id=customer_id,
                workspace_id=workspace_id,
                external_id=external_id,
            )
            return True  # Suppressed events count as success

        # Build and format rich notification
        try:
            formatted = settings.EVENT_PROCESSOR.process_event_rich(
                event_data, customer_data, target="slack", workspace=workspace
            )
        except Exception as e:
            logger.error(f"Failed to build notification: {e}", exc_info=True)
            return False  # Retry later

        # Get Slack webhook URL
        slack_webhook_url = self._get_slack_webhook_url(workspace)

        if not slack_webhook_url:
            logger.warning(
                f"No Slack webhook URL configured for workspace "
                f"{workspace.uuid if workspace else 'unknown'}, "
                f"skipping notification"
            )
            return True  # No webhook = nothing to do, consider success

        registry = PluginRegistry.instance()
        slack_plugin = registry.get(PluginType.DESTINATION, "slack")

        if slack_plugin is None or not isinstance(slack_plugin, BaseDestinationPlugin):
            logger.error("Slack destination plugin not found or not configured")
            return False  # Retry later

        try:
            slack_plugin.send(formatted, {"webhook_url": slack_webhook_url})
            logger.info(f"Sent {event_type} notification for customer {customer_id}")

            # Record the event after successful send
            event_consolidation_service.record_event(
                event_type=event_type,
                customer_id=customer_id,
                workspace_id=workspace_id,
                external_id=external_id,
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to send Slack notification for workspace "
                f"{workspace.uuid if workspace else 'unknown'}: {e}"
            )
            return False  # Retry later

    def _get_slack_webhook_url(self, workspace: Workspace | None) -> str | None:
        """Get Slack webhook URL for a workspace.

        Args:
            workspace: Workspace model instance.

        Returns:
            Slack webhook URL or None if not configured.
        """
        if not workspace:
            return None

        try:
            slack_integration = Integration.objects.get(
                workspace=workspace,
                integration_type="slack_notifications",
                is_active=True,
            )
            incoming_webhook = slack_integration.oauth_credentials.get(
                "incoming_webhook", {}
            )
            return cast("str | None", incoming_webhook.get("url"))
        except Integration.DoesNotExist:
            logger.warning(
                f"No active Slack integration found for workspace {workspace.uuid}"
            )
            return None

    def start_periodic_recovery(
        self, interval_seconds: int = RECOVERY_SWEEP_INTERVAL_SECONDS
    ) -> None:
        """Start the background thread that retries undelivered events.

        Once queued, events are our responsibility (the router has already
        acknowledged the webhook and suppressed provider retries), so a
        failed Slack send must be retried by us. A startup-only sweep is
        not enough: without a periodic sweep, a Slack outage with no
        coincidental redeploy loses notifications permanently.

        Idempotent per process; the thread is a daemon so it never blocks
        shutdown.

        Args:
            interval_seconds: Seconds between recovery sweeps.
        """
        with self._lock:
            if PendingEventQueue._recovery_thread_started:
                return
            PendingEventQueue._recovery_thread_started = True

        try:
            thread = threading.Thread(
                target=self._periodic_recovery_loop,
                args=(interval_seconds,),
                daemon=True,
                name="pending-webhook-recovery",
            )
            thread.start()
        except Exception:
            # Reset so a later call can retry; a stuck True would silently
            # disable periodic recovery for the process lifetime.
            with self._lock:
                PendingEventQueue._recovery_thread_started = False
            raise
        logger.info(
            f"Started periodic pending-webhook recovery (every {interval_seconds}s)"
        )

    def _periodic_recovery_loop(self, interval_seconds: int) -> None:
        """Run recovery sweeps forever, surviving individual failures.

        Args:
            interval_seconds: Seconds between recovery sweeps.
        """
        while True:
            time.sleep(interval_seconds)
            try:
                # One worker per fleet sweeps each interval (atomic SET NX).
                # Overlap would be harmless anyway - per-key processing
                # locks and the orphan age check guard each event group.
                sweep_ttl = max(interval_seconds - 30, 30)
                if cache.add(SWEEP_LOCK_KEY, "1", timeout=sweep_ttl):
                    self.recover_orphaned_events()
            except Exception:
                logger.exception("Periodic pending-webhook recovery sweep failed")

    def recover_orphaned_events(self) -> int:
        """Recover and process pending events that missed their timer.

        Called on server startup (events queued by a previous instance
        that died before processing them) and by the periodic recovery
        sweep (events whose delivery failed, or whose in-memory timer
        died with its worker).

        Only processes events older than ORPHAN_MIN_AGE_SECONDS to avoid
        racing with active timers on other server instances.

        Returns:
            Number of orphaned event groups processed.
        """
        redis_client = self._get_redis_client()
        if not redis_client:
            return 0

        processed_count = 0

        try:
            for key in self._scan_pending_keys(redis_client):
                if self._recover_single_event(key):
                    processed_count += 1

            if processed_count > 0:
                logger.info(f"Recovered {processed_count} orphaned event groups")

        except Exception as e:
            logger.error(f"Error during orphan recovery scan: {e}", exc_info=True)
        finally:
            # Sweeps run in the long-lived recovery thread (or the startup
            # thread); return their ORM connections instead of holding one
            # PostgreSQL connection open per thread between sweeps.
            connections.close_all()

        return processed_count

    def _get_redis_client(self) -> Any | None:
        """Get a raw Redis client for key scanning.

        The raw client is used ONLY for SCAN; reads and writes always go
        through the cache API so key prefixing and serialization stay
        consistent.

        Returns:
            Redis client or None if unavailable.
        """
        return get_raw_redis_client()

    def _scan_pending_keys(self, redis_client):
        """Scan Redis for pending webhook keys.

        The cache backend stores entries under its full key (KEY_PREFIX
        and version, e.g. "notipus:1:pending_webhook:..."), so the SCAN
        pattern must carry that prefix and it must be stripped again
        before the keys are used with the cache API. An unprefixed scan
        matches nothing in production.

        Args:
            redis_client: Redis client instance.

        Yields:
            Logical cache keys ("pending_webhook:{workspace}:{idem}").
        """
        prefix = cache.make_key("")
        pattern = f"{prefix}pending_webhook:*"
        cursor = 0

        while True:
            cursor, keys = redis_client.scan(cursor, match=pattern, count=100)

            for key in keys:
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                if key.startswith(prefix):
                    key = key[len(prefix) :]
                yield key

            if cursor == 0:
                break

    def _recover_single_event(self, key: str) -> bool:
        """Attempt to recover a single orphaned event group.

        Args:
            key: Redis key for the pending event.

        Returns:
            True if event was processed, False otherwise.
        """
        try:
            # Parse key: "pending_webhook:{workspace_id}:{idempotency_key}"
            parts = key.split(":", 2)
            if len(parts) != 3:
                return False

            _, workspace_id, idempotency_key = parts

            # Get stored events (a poisoned entry is purged and skipped)
            stored_items = self._read_pending_items(key)
            if not stored_items:
                return False

            # Check if events are old enough to be orphaned
            if not self._is_orphaned(stored_items):
                return False

            # Get workspace
            workspace = self._get_workspace_for_recovery(workspace_id, key)
            if workspace_id != "global" and workspace is None:
                return False  # Workspace not found, already logged and cleaned up

            logger.info(
                f"Recovering orphaned events for "
                f"idempotency_key {idempotency_key[:20]}..."
            )

            # Process the events
            self._process_events(
                idempotency_key=idempotency_key,
                workspace_id=workspace_id,
                provider_name="stripe",
                workspace=workspace,
            )
            return True

        except Exception as e:
            logger.error(f"Error recovering orphaned event {key}: {e}", exc_info=True)
            return False

    def _is_orphaned(self, stored_items: list[dict[str, Any]]) -> bool:
        """Check if stored events are old enough to be considered orphaned.

        Args:
            stored_items: List of stored event items.

        Returns:
            True if events are orphaned (old enough to process).
        """
        if not stored_items:
            return False

        first_event = stored_items[0]
        event_data = first_event.get("event_data", {})
        event_timestamp = event_data.get("_queued_at", 0)

        # Events without timestamp are assumed orphaned
        if event_timestamp <= 0:
            return True

        age_seconds = time.time() - float(event_timestamp)
        return bool(age_seconds >= ORPHAN_MIN_AGE_SECONDS)

    def _get_workspace_for_recovery(
        self, workspace_id: str, cache_key: str
    ) -> Workspace | None:
        """Get workspace for orphan recovery.

        Args:
            workspace_id: Workspace UUID string or "global".
            cache_key: Redis key (for cleanup if workspace not found).

        Returns:
            Workspace instance, or None for global/not found.
        """
        if workspace_id == "global":
            return None

        try:
            return Workspace.objects.get(uuid=workspace_id)
        except Workspace.DoesNotExist:
            logger.warning(
                f"Workspace {workspace_id} not found, skipping orphaned events"
            )
            cache.delete(cache_key)
            return None


# Module-level singleton instance
pending_event_queue = PendingEventQueue()

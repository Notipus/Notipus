"""Pending event queue for delayed webhook processing.

This module implements a delayed processing system for Stripe webhooks.
Events are queued and processed after a delay to allow related events
(e.g., subscription.created and invoice.paid) to arrive before sending
a single consolidated notification.

The delay ensures we have complete data (like customer_email from invoice
events) even when processing subscription events that arrive first.

On server startup, orphaned events (from previous server instances) are
recovered and processed to prevent data loss on ephemeral infrastructure.

Thread Safety:
- Uses Redis atomic operations (SETNX) for distributed locking
- Uses JSON append pattern with optimistic locking for event storage
- Timer scheduling uses threading.Lock for in-process safety
"""

import json
import logging
import threading
import time
from typing import Any

from core.models import Integration, Workspace
from django.conf import settings
from django.core.cache import cache

from .thread_mapping import thread_mapping_service

logger = logging.getLogger(__name__)

# Minimum age (in seconds) before an orphaned event is processed on startup.
# This prevents processing events that were just queued and have active timers.
ORPHAN_MIN_AGE_SECONDS = 35  # Slightly longer than DELAY_SECONDS

# Lock TTL for distributed processing lock (seconds)
# Should be longer than max expected processing time
PROCESSING_LOCK_TTL = 60

# Maximum retries for storing events (optimistic locking)
MAX_STORE_RETRIES = 3


class PendingEventQueue:
    """Queue for delayed processing of webhook events.

    When Stripe events arrive, they share an idempotency_key for related
    events (subscription.created, invoice.paid, invoice.payment_succeeded).
    This queue collects all events with the same key and processes them
    together after a delay, ensuring we have complete data for notifications.

    Attributes:
        DELAY_SECONDS: Time to wait before processing (default 30s).
        TTL_SECONDS: Redis TTL for pending events (default 5 min).
    """

    DELAY_SECONDS = 30
    TTL_SECONDS = 300  # 5 min TTL for pending events

    # Track active timers to avoid duplicate scheduling
    # Key: "{workspace_id}:{idempotency_key}" -> Timer
    _active_timers: dict[str, threading.Timer] = {}
    _lock = threading.Lock()

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

        Uses atomic Redis operations to prevent race conditions when
        multiple events arrive simultaneously.

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

        # Use atomic append with retry loop to handle concurrent writes
        for attempt in range(MAX_STORE_RETRIES):
            try:
                self._atomic_append(key, new_item)
                return
            except Exception as e:
                if attempt == MAX_STORE_RETRIES - 1:
                    logger.error(
                        f"Failed to store event after {MAX_STORE_RETRIES} attempts: {e}"
                    )
                    raise
                # Small backoff before retry
                time.sleep(0.01 * (attempt + 1))

    def _atomic_append(self, key: str, item: dict[str, Any]) -> None:
        """Atomically append an item to a list in Redis.

        Uses Redis WATCH/MULTI/EXEC for optimistic locking to ensure
        concurrent appends don't overwrite each other.

        Falls back to non-atomic append if Redis client is unavailable
        (e.g., in tests or with non-Redis cache backends).

        Args:
            key: Redis key for the list.
            item: Item to append.
        """
        redis_client = self._get_redis_client_for_atomic()
        if redis_client is None:
            # Fallback to non-atomic append
            self._simple_append(key, item)
            return

        # Use Redis pipeline with WATCH for optimistic locking
        pipe = redis_client.pipeline(True)  # True = use MULTI/EXEC
        try:
            # Watch the key for changes
            pipe.watch(key)

            # Get current value
            current = pipe.get(key)
            if current:
                if isinstance(current, bytes):
                    current = current.decode("utf-8")
                existing = json.loads(current)
            else:
                existing = []

            # Append new item
            existing.append(item)

            # Start transaction
            pipe.multi()
            pipe.setex(key, self.TTL_SECONDS, json.dumps(existing))
            pipe.execute()

        except Exception as e:
            # WatchError means another client modified the key - retry
            pipe.reset()
            raise e

    def _get_redis_client_for_atomic(self):
        """Get Redis client for atomic operations.

        Returns:
            Redis client or None if unavailable.
        """
        try:
            client = cache.client.get_client()
            # Verify it's a real Redis client by checking for concrete type
            # MagicMock will have __class__.__name__ == 'MagicMock'
            client_class = client.__class__.__name__
            if "Mock" in client_class or "mock" in client_class:
                return None
            if hasattr(client, "pipeline"):
                return client
            return None
        except (AttributeError, Exception):
            return None

    def _simple_append(self, key: str, item: dict[str, Any]) -> None:
        """Simple non-atomic append (fallback for non-Redis backends).

        Args:
            key: Cache key for the list.
            item: Item to append.
        """
        existing = cache.get(key) or []
        existing.append(item)
        cache.set(key, existing, timeout=self.TTL_SECONDS)

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
                self._process_events,
                args=[idempotency_key, workspace_id, provider_name, workspace],
            )
            timer.daemon = True  # Don't block shutdown
            timer.start()

            self._active_timers[timer_key] = timer

            logger.info(
                f"Scheduled processing in {self.DELAY_SECONDS}s for "
                f"idempotency_key {idempotency_key}"
            )

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
            stored_items = cache.get(key) or []

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

            if success:
                # Delete pending events only after successful send
                cache.delete(key)
            else:
                # Leave events for retry (orphan recovery will pick them up)
                logger.warning(
                    f"Notification failed for {idempotency_key}, events left for retry"
                )
        finally:
            # Always release the lock
            self._release_lock(lock_key)

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

    def _aggregate_events(
        self, stored_items: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Combine multiple events, prioritizing best data.

        Priority rules:
        - event_type: Prefer subscription_created/trial_started over invoice events
        - customer_email: Take from ANY event that has it (invoice events have it)
        - Other fields: Take from first event, update if better data found

        Args:
            stored_items: List of stored items with event_data and customer_data.

        Returns:
            Tuple of (aggregated_event_data, aggregated_customer_data).
        """
        if not stored_items:
            return {}, {}

        # Start with first item as base
        result_event = stored_items[0]["event_data"].copy()
        result_customer = stored_items[0]["customer_data"].copy()

        # Event type priority (higher = preferred)
        type_priority = {
            "trial_started": 100,
            "subscription_created": 90,
            "subscription_updated": 80,
            "subscription_deleted": 80,
            "checkout_completed": 70,
            "payment_success": 50,
            "invoice_paid": 40,
            "payment_failure": 60,  # Important, keep if present
        }

        best_type_priority = type_priority.get(result_event.get("type", ""), 0)

        for item in stored_items[1:]:
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

            # Priority: prefer subscription/trial events for the notification type
            event_type = event_data.get("type", "")
            event_priority = type_priority.get(event_type, 0)

            if event_priority > best_type_priority:
                result_event["type"] = event_type
                best_type_priority = event_priority

                # Copy metadata from the preferred event type
                if event_data.get("metadata"):
                    result_event["metadata"] = event_data["metadata"]

            # Merge other customer data if missing
            for field in ["first_name", "last_name", "company_name"]:
                if customer_data.get(field) and not result_customer.get(field):
                    result_customer[field] = customer_data[field]

        logger.info(
            f"Aggregated {len(stored_items)} events: type={result_event.get('type')}, "
            f"email={result_customer.get('email') or 'MISSING'}"
        )

        self._warn_if_missing_email(result_event, result_customer, stored_items)

        return result_event, result_customer

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

        # Check if this event should be suppressed due to consolidation
        # (e.g., $0 trial invoices)
        should_notify = event_consolidation_service.should_send_notification(
            event_type=event_type,
            customer_id=customer_id,
            workspace_id=workspace_id,
            amount=event_data.get("amount"),
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

        # Get Slack credentials (webhook URL and optionally bot_token for threading)
        slack_credentials = self._get_slack_credentials(workspace)

        if not slack_credentials:
            logger.warning(
                f"No Slack credentials configured for workspace "
                f"{workspace.uuid if workspace else 'unknown'}, "
                f"skipping notification"
            )
            return True  # No credentials = nothing to do, consider success

        registry = PluginRegistry.instance()
        slack_plugin = registry.get(PluginType.DESTINATION, "slack")

        if slack_plugin is None or not isinstance(slack_plugin, BaseDestinationPlugin):
            logger.error("Slack destination plugin not found or not configured")
            return False  # Retry later

        # Check for thread mapping (for Zendesk ticket updates)
        options: dict[str, Any] = {}
        entity_type, entity_id = self._get_thread_info(workspace, event_data)

        if entity_type and entity_id and workspace:
            # Look up existing thread
            thread_info = thread_mapping_service.get_thread_ts(
                workspace, entity_type, entity_id
            )
            if thread_info:
                options["thread_ts"] = thread_info.thread_ts
                options["channel"] = thread_info.channel_id
                logger.debug(
                    f"Found existing thread for {entity_type}:{entity_id}: "
                    f"{thread_info.thread_ts}"
                )

        try:
            result = slack_plugin.send(formatted, slack_credentials, options)
            logger.info(f"Sent {event_type} notification for customer {customer_id}")

            # Store thread mapping for new threads (Zendesk tickets)
            if (
                entity_type
                and entity_id
                and workspace
                and result.get("thread_ts")
                and not options.get("thread_ts")  # Only store if this is a new thread
            ):
                channel_id = result.get("channel") or slack_credentials.get("channel")
                if channel_id:
                    thread_mapping_service.store_thread_ts(
                        workspace=workspace,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        channel_id=channel_id,
                        thread_ts=result["thread_ts"],
                    )

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
            return incoming_webhook.get("url")
        except Integration.DoesNotExist:
            logger.warning(
                f"No active Slack integration found for workspace {workspace.uuid}"
            )
            return None

    def _get_slack_credentials(
        self, workspace: Workspace | None
    ) -> dict[str, Any] | None:
        """Get Slack credentials for a workspace (webhook URL, bot token, channel).

        For threading support, we need the bot_token and channel_id.
        Falls back to webhook-only if bot token isn't available.

        Args:
            workspace: Workspace model instance.

        Returns:
            Dict with 'webhook_url' and optionally 'bot_token' and 'channel',
            or None if not configured.
        """
        if not workspace:
            return None

        try:
            slack_integration = Integration.objects.get(
                workspace=workspace,
                integration_type="slack_notifications",
                is_active=True,
            )
            oauth_credentials = slack_integration.oauth_credentials or {}
            incoming_webhook = oauth_credentials.get("incoming_webhook", {})

            credentials: dict[str, Any] = {}

            # Get webhook URL
            webhook_url = incoming_webhook.get("url")
            if webhook_url:
                credentials["webhook_url"] = webhook_url

            # Get bot token for API-based sending (enables threading)
            bot_token = oauth_credentials.get("access_token")
            if bot_token:
                credentials["bot_token"] = bot_token

            # Get channel ID (from webhook or explicit setting)
            channel_id = incoming_webhook.get("channel_id")
            if channel_id:
                credentials["channel"] = channel_id

            return credentials if credentials else None

        except Integration.DoesNotExist:
            logger.warning(
                f"No active Slack integration found for workspace {workspace.uuid}"
            )
            return None

    def _get_thread_info(
        self,
        workspace: Workspace,
        event_data: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """Get thread info for Zendesk ticket events.

        Args:
            workspace: Workspace model instance.
            event_data: Event data dictionary.

        Returns:
            Tuple of (entity_type, entity_id) for thread lookup, or (None, None).
        """
        event_type = event_data.get("type", "")
        if not event_type.startswith("support_ticket"):
            return None, None

        provider = event_data.get("provider", "")
        metadata = event_data.get("metadata", {})
        ticket_id = metadata.get("ticket_id") or event_data.get("external_id")

        if provider == "zendesk" and ticket_id:
            return f"{provider}_ticket", str(ticket_id)

        return None, None

    def recover_orphaned_events(self) -> int:
        """Recover and process orphaned events from Redis.

        Called on server startup to process events that were queued by
        a previous server instance that died before processing them.

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

        return processed_count

    def _get_redis_client(self):
        """Get Redis client for key scanning.

        Returns:
            Redis client or None if unavailable.
        """
        try:
            return cache.client.get_client()
        except (AttributeError, Exception) as e:
            logger.warning(f"Cannot access Redis client for orphan recovery: {e}")
            return None

    def _scan_pending_keys(self, redis_client):
        """Scan Redis for pending webhook keys.

        Args:
            redis_client: Redis client instance.

        Yields:
            Decoded key strings matching pending_webhook:* pattern.
        """
        pattern = "pending_webhook:*"
        cursor = 0

        while True:
            cursor, keys = redis_client.scan(cursor, match=pattern, count=100)

            for key in keys:
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
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

            # Get stored events
            stored_items = cache.get(key)
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

        age_seconds = time.time() - event_timestamp
        return age_seconds >= ORPHAN_MIN_AGE_SECONDS

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

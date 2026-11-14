"""Replay stored Shopify webhooks that were never processed.

This command reads raw webhook payloads from Redis storage and re-processes
them through the notification pipeline. Use this to recover from situations
where webhooks were received but notifications were lost due to worker
recycling before timers could fire.

Each stored webhook is rebuilt with the live parser
(``ShopifySourcePlugin.build_event_from_payload``), checked against the
same dedup key the live endpoint records
(``shopify:{type}:sha256:{body_hash}``), and delivered through the same
immediate path to every configured destination. Webhooks that were
already processed live are therefore skipped, unknown topics are
skipped, and one bad payload never stops the run.

Records stored before ``body_sha256`` was captured cannot be matched
against the live dedup key, so they are skipped unless ``--force``.

Usage:
    python manage.py replay_shopify_webhooks --workspace my-shop
    python manage.py replay_shopify_webhooks --workspace my-shop --dry-run
    python manage.py replay_shopify_webhooks --workspace my-shop --days 7
"""

import json
import logging
from typing import Any

from core.models import Workspace
from django.core.management.base import BaseCommand, CommandError
from plugins.sources.shopify import ShopifySourcePlugin
from webhooks.services.event_consolidation import event_consolidation_service
from webhooks.services.webhook_storage import webhook_storage_service
from webhooks.webhook_router import (
    _get_dedup_key,
    _process_immediately,
    _record_dedup_marker,
)

logger = logging.getLogger(__name__)

STAT_KEYS = (
    "processed",
    "skipped_dedup",
    "skipped_filter",
    "skipped_topic",
    "skipped_unverifiable",
    "errors",
)


class Command(BaseCommand):
    """Replay stored Shopify webhooks that were never processed."""

    help = "Replay stored Shopify webhooks that were never processed"

    def add_arguments(self, parser: Any) -> None:
        """Add command arguments.

        Args:
            parser: The argument parser.
        """
        parser.add_argument(
            "--workspace",
            type=str,
            required=True,
            help="Workspace slug to replay webhooks for",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Number of days to look back (default: 7)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without sending notifications",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Process even if already in dedup cache (use with caution)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Execute the command.

        Args:
            *args: Unused positional arguments.
            **options: Parsed command options.
        """
        workspace = self._get_workspace(options["workspace"])
        days: int = options["days"]
        dry_run: bool = options["dry_run"]
        force: bool = options["force"]

        self._print_header(workspace, days, dry_run)

        webhooks = self._get_stored_webhooks(workspace, days)
        stats = dict.fromkeys(STAT_KEYS, 0)
        for webhook in webhooks:
            try:
                result = self._process_single_webhook(
                    webhook, workspace, dry_run, force
                )
            except Exception as e:
                # One bad payload must not stop the rest of the replay.
                logger.exception("Replay of a stored Shopify webhook failed")
                self.stdout.write(self.style.ERROR(f"  Error: {e}"))
                result = "errors"
            stats[result] += 1

        self._print_summary(stats, dry_run)

    def _get_workspace(self, slug: str) -> Workspace:
        """Get workspace by slug.

        Args:
            slug: Workspace slug.

        Returns:
            The workspace.

        Raises:
            CommandError: If no such workspace exists.
        """
        try:
            return Workspace.objects.get(slug=slug)
        except Workspace.DoesNotExist as e:
            raise CommandError(f"Workspace '{slug}' not found") from e

    def _print_header(self, workspace: Workspace, days: int, dry_run: bool) -> None:
        """Print command header.

        Args:
            workspace: The workspace being replayed.
            days: Look-back window.
            dry_run: Whether this is a dry run.
        """
        self.stdout.write(f"Replaying Shopify webhooks for workspace: {workspace.name}")
        self.stdout.write(f"Looking back {days} days")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN - no notifications will be sent")
            )

    def _get_stored_webhooks(
        self, workspace: Workspace, days: int
    ) -> list[dict[str, Any]]:
        """Load stored Shopify webhooks for the workspace, oldest first.

        Every topic for an order (create, paid, fulfilled, ...) is kept:
        they are separate events. Exact redeliveries (same topic, same
        signed body) are collapsed to one.

        Args:
            workspace: The workspace.
            days: Look-back window.

        Returns:
            Stored webhook records in arrival order.
        """
        webhooks = webhook_storage_service.get_recent_webhooks(
            days=days,
            limit=10_000,
            provider="shopify",
            workspace_uuid=str(workspace.uuid),
        )
        self.stdout.write(f"Found {len(webhooks)} stored webhooks")

        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for webhook in reversed(webhooks):
            body_hash = webhook.get("body_sha256")
            topic = (webhook.get("headers") or {}).get("X-Shopify-Topic") or ""
            if body_hash:
                if (topic, body_hash) in seen:
                    continue
                seen.add((topic, body_hash))
            unique.append(webhook)
        return unique

    def _process_single_webhook(
        self,
        webhook: dict[str, Any],
        workspace: Workspace,
        dry_run: bool,
        force: bool,
    ) -> str:
        """Rebuild and (unless dry-run) deliver one stored webhook.

        Args:
            webhook: The stored webhook record.
            workspace: The workspace.
            dry_run: Report only, send nothing.
            force: Ignore dedup and missing body hashes.

        Returns:
            The stats key to increment.
        """
        headers = webhook.get("headers") or {}
        topic = headers.get("X-Shopify-Topic") or ""
        if topic == "test" or topic not in ShopifySourcePlugin.EVENT_TYPE_MAPPING:
            self.stdout.write(f"  Skipping unsupported topic {topic or '(none)'}")
            return "skipped_topic"

        body = webhook.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)
        if not isinstance(body, dict):
            raise ValueError("stored body is not a JSON object")

        body_hash = webhook.get("body_sha256")
        if not body_hash and not force:
            self.stdout.write(
                f"  Skipping {topic} {body.get('id')} - stored without a body "
                "hash, cannot check the live dedup key (use --force)"
            )
            return "skipped_unverifiable"

        plugin = ShopifySourcePlugin()
        is_test = str(headers.get("X-Shopify-Test") or "").lower() == "true"
        event_data = plugin.build_event_from_payload(topic, body, is_test=is_test)
        if event_data is None:
            return "skipped_topic"
        if body_hash:
            event_data["content_hash"] = body_hash

        workspace_id = str(workspace.uuid)
        dedup_key = _get_dedup_key(event_data)
        label = self._label(event_data, topic)

        if not force and event_consolidation_service.is_duplicate(
            workspace_id, dedup_key
        ):
            self.stdout.write(f"  Skipping {label} - already processed")
            return "skipped_dedup"

        customer_data = plugin.get_customer_data(event_data.get("customer_id") or "")

        if dry_run:
            self.stdout.write(f"  [DRY RUN] Would send: {label}")
            return "processed"

        return self._deliver(
            event_data, customer_data, workspace, workspace_id, dedup_key, label, force
        )

    def _deliver(
        self,
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
        workspace: Workspace,
        workspace_id: str,
        dedup_key: str,
        label: str,
        force: bool,
    ) -> str:
        """Deliver through the live immediate path and record dedup.

        The dedup key is claimed first (unless forced) so a replay never
        races a live delivery of the same event.

        Args:
            event_data: The rebuilt event.
            customer_data: The customer data.
            workspace: The workspace.
            workspace_id: The workspace UUID string.
            dedup_key: The live dedup key.
            label: Human-readable description for output.
            force: Deliver even if the event was already processed.

        Returns:
            The stats key to increment.
        """
        if (
            not force
            and event_consolidation_service.claim_event(workspace_id, dedup_key)
            != "claimed"
        ):
            self.stdout.write(f"  Skipping {label} - processed concurrently")
            return "skipped_dedup"
        try:
            response = _process_immediately(
                event_data, customer_data, "customer_shopify", workspace
            )
        except Exception:
            event_consolidation_service.release_claim(workspace_id, dedup_key)
            raise

        _record_dedup_marker(event_data, workspace_id, dedup_key)
        message = json.loads(response.content).get("message", "")
        if "consolidated" in message:
            self.stdout.write(f"  Skipping {label} - filtered by consolidation")
            return "skipped_filter"
        self.stdout.write(self.style.SUCCESS(f"  Sent: {label}"))
        return "processed"

    def _label(self, event_data: dict[str, Any], topic: str) -> str:
        """Describe an event for command output (no customer PII).

        Args:
            event_data: The rebuilt event.
            topic: The Shopify topic.

        Returns:
            A short description.
        """
        metadata = event_data.get("metadata") or {}
        ref = metadata.get("order_number") or event_data.get("external_id") or "?"
        amount = event_data.get("amount")
        currency = event_data.get("currency") or ""
        money = f" - {currency} {float(amount):.2f}" if amount is not None else ""
        return f"{topic} #{ref}{money}"

    def _print_summary(self, stats: dict[str, int], dry_run: bool) -> None:
        """Print command summary.

        Args:
            stats: Counters per outcome.
            dry_run: Whether this was a dry run.
        """
        self.stdout.write("")
        self.stdout.write("=" * 50)
        self.stdout.write(f"Processed: {stats['processed']}")
        self.stdout.write(f"Skipped (already processed): {stats['skipped_dedup']}")
        self.stdout.write(f"Skipped (filtered): {stats['skipped_filter']}")
        self.stdout.write(f"Skipped (unsupported topic): {stats['skipped_topic']}")
        self.stdout.write(
            f"Skipped (no body hash, use --force): {stats['skipped_unverifiable']}"
        )
        self.stdout.write(f"Errors: {stats['errors']}")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("This was a dry run. Run without --dry-run to send.")
            )

"""Replay stored Shopify webhooks that were never processed.

This command reads raw webhook payloads from Redis storage and re-processes
them through the notification pipeline. Use this to recover from situations
where webhooks were received but notifications were lost due to worker
recycling before timers could fire.

Usage:
    python manage.py replay_shopify_webhooks --workspace screenly
    python manage.py replay_shopify_webhooks --workspace screenly --dry-run
    python manage.py replay_shopify_webhooks --workspace screenly --days 7
"""

import json
import logging
from typing import Any

from core.models import Integration, Workspace
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from plugins.base import PluginType
from plugins.destinations.base import BaseDestinationPlugin
from plugins.registry import PluginRegistry
from plugins.sources.shopify import ShopifySourcePlugin
from webhooks.services.event_consolidation import event_consolidation_service
from webhooks.services.webhook_storage import webhook_storage_service

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Replay stored Shopify webhooks that were never processed."""

    help = "Replay stored Shopify webhooks that were never processed"

    def add_arguments(self, parser: Any) -> None:
        """Add command arguments."""
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
        """Execute the command."""
        workspace_slug = options["workspace"]
        days = options["days"]
        dry_run = options["dry_run"]
        force = options["force"]

        workspace = self._get_workspace(workspace_slug)
        slack_webhook_url = self._get_slack_webhook_url(workspace)
        slack_plugin = self._get_slack_plugin()

        self._print_header(workspace, days, dry_run)

        orders = self._get_unique_orders(workspace, days)
        stats = self._process_orders(
            orders, workspace, slack_webhook_url, slack_plugin, dry_run, force
        )
        self._print_summary(stats, dry_run)

    def _get_workspace(self, slug: str) -> Workspace:
        """Get workspace by slug."""
        try:
            return Workspace.objects.get(slug=slug)
        except Workspace.DoesNotExist as e:
            raise CommandError(f"Workspace '{slug}' not found") from e

    def _get_slack_webhook_url(self, workspace: Workspace) -> str:
        """Get Slack webhook URL for workspace."""
        try:
            slack_integration = Integration.objects.get(
                workspace=workspace,
                integration_type="slack_notifications",
                is_active=True,
            )
            url = slack_integration.oauth_credentials.get("incoming_webhook", {}).get(
                "url"
            )
            if not url:
                raise CommandError("Slack webhook URL not configured")
            return str(url)
        except Integration.DoesNotExist as e:
            raise CommandError("No active Slack integration found") from e

    def _get_slack_plugin(self) -> BaseDestinationPlugin:
        """Get Slack destination plugin."""
        registry = PluginRegistry.instance()
        registry.discover()
        plugin = registry.get(PluginType.DESTINATION, "slack")
        if plugin is None or not isinstance(plugin, BaseDestinationPlugin):
            raise CommandError("Slack plugin not available")
        return plugin

    def _print_header(self, workspace: Workspace, days: int, dry_run: bool) -> None:
        """Print command header."""
        self.stdout.write(f"Replaying Shopify webhooks for workspace: {workspace.name}")
        self.stdout.write(f"Looking back {days} days")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN - no notifications will be sent")
            )

    def _get_unique_orders(
        self, workspace: Workspace, days: int
    ) -> dict[str, dict[str, Any]]:
        """Get unique orders from stored webhooks."""
        webhooks = webhook_storage_service.get_recent_webhooks(
            days=days,
            limit=1000,
            provider="shopify",
            workspace_uuid=str(workspace.uuid),
        )
        self.stdout.write(f"Found {len(webhooks)} stored webhooks")

        orders: dict[str, dict[str, Any]] = {}
        for webhook in reversed(webhooks):
            body = webhook.get("body", "{}")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    continue

            order_id = str(body.get("id", ""))
            if not order_id or order_id in orders:
                continue

            orders[order_id] = {
                "body": body,
                "topic": webhook.get("headers", {}).get("X-Shopify-Topic"),
            }

        self.stdout.write(f"Found {len(orders)} unique orders to process")
        return orders

    def _process_orders(
        self,
        orders: dict[str, dict[str, Any]],
        workspace: Workspace,
        slack_webhook_url: str,
        slack_plugin: BaseDestinationPlugin,
        dry_run: bool,
        force: bool,
    ) -> dict[str, int]:
        """Process all orders and return stats."""
        stats = {"processed": 0, "skipped_dedup": 0, "skipped_filter": 0, "errors": 0}
        workspace_id = str(workspace.uuid)

        for order_id, order_data in orders.items():
            result = self._process_single_order(
                order_id,
                order_data,
                workspace,
                workspace_id,
                slack_webhook_url,
                slack_plugin,
                dry_run,
                force,
            )
            stats[result] += 1

        return stats

    def _process_single_order(
        self,
        order_id: str,
        order_data: dict[str, Any],
        workspace: Workspace,
        workspace_id: str,
        slack_webhook_url: str,
        slack_plugin: BaseDestinationPlugin,
        dry_run: bool,
        force: bool,
    ) -> str:
        """Process a single order. Returns stat key to increment."""
        body = order_data["body"]
        topic = order_data["topic"]

        # Check dedup
        if not force and event_consolidation_service.is_duplicate(
            workspace_id, order_id
        ):
            self.stdout.write(f"  Skipping order {order_id} - already processed")
            return "skipped_dedup"

        # Build event and customer data
        event_data, customer_data = self._build_event_data(
            body, topic, order_id, workspace_id
        )

        # Check consolidation filter
        if not event_consolidation_service.should_send_notification(
            event_type=event_data["type"],
            customer_id=event_data["customer_id"],
            workspace_id=workspace_id,
            amount=event_data.get("amount"),
        ):
            self.stdout.write(
                f"  Skipping order {order_id} - filtered by consolidation"
            )
            return "skipped_filter"

        # Build notification
        try:
            formatted = settings.EVENT_PROCESSOR.process_event_rich(
                event_data, customer_data, target="slack", workspace=workspace
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"  Error building notification for {order_id}: {e}")
            )
            return "errors"

        # Send or dry-run
        return self._send_notification(
            formatted,
            event_data,
            customer_data,
            body,
            order_id,
            workspace_id,
            slack_webhook_url,
            slack_plugin,
            dry_run,
        )

    def _build_event_data(
        self,
        body: dict[str, Any],
        topic: str | None,
        order_id: str,
        workspace_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build event and customer data from webhook body."""
        customer = body.get("customer", {})
        customer_id = str(customer.get("id", "")) if customer else ""

        event_type = ShopifySourcePlugin.EVENT_TYPE_MAPPING.get(
            topic or "", "payment_success"
        )

        # Extract line items for display in notification
        line_items = []
        for item in body.get("line_items", []):
            line_items.append(
                {
                    "name": item.get("name", item.get("title", "Unknown Product")),
                    "sku": item.get("sku", ""),
                    "quantity": item.get("quantity", 1),
                    "price": float(item.get("price", 0)),
                    "variant_title": item.get("variant_title", ""),
                }
            )

        event_data = {
            "type": event_type,
            "customer_id": customer_id,
            "provider": "shopify",
            "external_id": order_id,
            "amount": float(body.get("total_price", 0)),
            "currency": body.get("currency", "USD"),
            "workspace_id": workspace_id,
            "metadata": {
                "order_number": body.get("order_number"),
                "financial_status": body.get("financial_status"),
                "line_items": line_items,
            },
        }

        customer_data = {
            "email": customer.get("email", ""),
            "first_name": customer.get("first_name", ""),
            "last_name": customer.get("last_name", ""),
            "company": customer.get("company", ""),
        }

        return event_data, customer_data

    def _send_notification(
        self,
        formatted: dict[str, Any],
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
        body: dict[str, Any],
        order_id: str,
        workspace_id: str,
        slack_webhook_url: str,
        slack_plugin: BaseDestinationPlugin,
        dry_run: bool,
    ) -> str:
        """Send notification or print dry-run message."""
        amount = event_data.get("amount", 0)
        currency = event_data.get("currency", "USD")
        order_num = body.get("order_number")
        email = customer_data.get("email", "no email")

        if dry_run:
            self.stdout.write(
                f"  [DRY RUN] Would send: Order #{order_num} "
                f"- {currency} {amount:.2f} - {email}"
            )
            return "processed"

        try:
            slack_plugin.send(formatted, {"webhook_url": slack_webhook_url})
            event_consolidation_service.record_event(
                event_type=event_data["type"],
                customer_id=event_data["customer_id"],
                workspace_id=workspace_id,
                external_id=order_id,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  Sent: Order #{order_num} - {currency} {amount:.2f}"
                )
            )
            return "processed"
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Error sending for {order_id}: {e}"))
            return "errors"

    def _print_summary(self, stats: dict[str, int], dry_run: bool) -> None:
        """Print command summary."""
        self.stdout.write("")
        self.stdout.write("=" * 50)
        self.stdout.write(f"Processed: {stats['processed']}")
        self.stdout.write(f"Skipped (already processed): {stats['skipped_dedup']}")
        self.stdout.write(f"Skipped (filtered): {stats['skipped_filter']}")
        self.stdout.write(f"Errors: {stats['errors']}")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("This was a dry run. Run without --dry-run to send.")
            )

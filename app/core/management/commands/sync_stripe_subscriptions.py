"""Management command to sync Stripe subscription state to workspaces.

This command fetches all subscriptions from Stripe and updates the
corresponding workspace billing state. Use for initial sync or recovery.
"""

import logging
from argparse import ArgumentParser
from typing import Any, cast

import stripe
from core.models import Workspace
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from webhooks.services.billing import STRIPE_STATUS_MAPPING, BillingService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Django management command to sync Stripe subscriptions globally.

    Fetches all subscriptions from Stripe and updates matching workspaces.

    Usage:
        python manage.py sync_stripe_subscriptions
        python manage.py sync_stripe_subscriptions --dry-run
    """

    help = "Sync subscription state from Stripe to all workspaces"

    def add_arguments(self, parser: "ArgumentParser") -> None:
        """Add command line arguments.

        Args:
            parser: The argument parser instance.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be synced without making changes",
        )

    def _configure_stripe(self) -> None:
        """Configure Stripe API and verify connection."""
        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.api_version = settings.STRIPE_API_VERSION

        try:
            account = stripe.Account.retrieve()
            self.stdout.write(f"Connected to Stripe account: {account.id}")
        except stripe.AuthenticationError as err:
            raise CommandError(
                "Failed to connect to Stripe. Check your STRIPE_SECRET_KEY."
            ) from err

    def _fetch_all_subscriptions(self) -> list:
        """Fetch all subscriptions from Stripe with pagination."""
        self.stdout.write("Fetching subscriptions from Stripe...")

        subscriptions = []
        starting_after = None

        while True:
            params: dict = {"limit": 100, "expand": ["data.items.data.price"]}
            if starting_after:
                params["starting_after"] = starting_after

            response = stripe.Subscription.list(**params)
            subscriptions.extend(response.data)

            if not response.has_more:
                break
            if response.data:
                starting_after = response.data[-1].id

        self.stdout.write(f"Found {len(subscriptions)} subscription(s) in Stripe")
        return subscriptions

    def _build_customer_subscription_map(
        self, subscriptions: list
    ) -> dict[str, stripe.Subscription]:
        """Build map of customer_id to most relevant subscription."""
        customer_subscriptions: dict[str, stripe.Subscription] = {}

        for sub in subscriptions:
            customer_id = sub.customer
            if isinstance(customer_id, stripe.Customer):
                customer_id = customer_id.id

            existing = customer_subscriptions.get(customer_id)

            if existing is None:
                customer_subscriptions[customer_id] = sub
            elif sub.status in ("active", "trialing", "past_due"):
                if existing.status not in ("active", "trialing"):
                    customer_subscriptions[customer_id] = sub

        return customer_subscriptions

    def handle(self, *args, **options) -> None:
        """Execute the command.

        Args:
            *args: Positional arguments.
            **options: Command options including dry_run.
        """
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )
            self.stdout.write("")

        self._configure_stripe()
        self.stdout.write("")

        results = {
            "synced": 0,
            "skipped_no_workspace": 0,
            "errors": 0,
        }

        subscriptions = self._fetch_all_subscriptions()
        self.stdout.write("")

        customer_subscriptions = self._build_customer_subscription_map(subscriptions)
        customer_count = len(customer_subscriptions)
        self.stdout.write(f"Processing {customer_count} unique customer(s)")
        self.stdout.write("-" * 50)

        for customer_id, sub in customer_subscriptions.items():
            self._process_subscription(customer_id, sub, dry_run, results)

        self._print_summary(results, dry_run)

    def _safe_get(self, obj: Any, key: str) -> Any:
        """Safely get attribute from dict-like or object."""
        if obj is None:
            return None
        if hasattr(obj, "get"):
            return obj.get(key)
        return getattr(obj, key, None)

    def _get_product_name(self, sub: stripe.Subscription) -> str | None:
        """Extract product name from subscription."""
        # Use safe access - handles both dict-like and object access
        items = self._safe_get(sub, "items")
        items_data = self._safe_get(items, "data")

        if not items_data or len(items_data) == 0:
            return None

        first_item = items_data[0]
        price = self._safe_get(first_item, "price")
        product = self._safe_get(price, "product")

        if product is None:
            return None
        if isinstance(product, stripe.Product):
            return product.name
        if isinstance(product, str):
            try:
                return stripe.Product.retrieve(product).name
            except stripe.StripeError:
                return None
        return cast("str | None", self._safe_get(product, "name"))

    def _normalize_plan_name(self, product_name: str | None) -> str | None:
        """Convert product name to internal plan name."""
        if not product_name:
            return None
        return product_name.lower().replace("notipus ", "").replace(" plan", "").strip()

    def _process_subscription(
        self,
        customer_id: str,
        sub: stripe.Subscription,
        dry_run: bool,
        results: dict,
    ) -> None:
        """Process a single subscription and sync to workspace.

        Args:
            customer_id: Stripe customer ID.
            sub: Stripe Subscription object.
            dry_run: If True, don't make actual changes.
            results: Dictionary to track operation counts.
        """
        try:
            workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()

            if not workspace:
                self.stdout.write(f"  SKIP: No workspace for customer {customer_id}")
                results["skipped_no_workspace"] += 1
                return

            internal_status = STRIPE_STATUS_MAPPING.get(sub.status, "active")
            plan_name = self._normalize_plan_name(self._get_product_name(sub))

            changes = self._get_changes(workspace, internal_status, plan_name)

            if not changes:
                self.stdout.write(
                    f"  OK: {workspace.name} ({customer_id}) - already in sync"
                )
                results["synced"] += 1
                return

            self._apply_changes(workspace, customer_id, changes, dry_run, results)

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"  ERROR processing {customer_id}: {e!s}")
            )
            results["errors"] += 1
            logger.exception(f"Error processing customer {customer_id}")

    def _get_changes(
        self, workspace: Workspace, status: str, plan: str | None
    ) -> list[str]:
        """Determine what changes need to be made."""
        changes = []
        if workspace.subscription_status != status:
            changes.append(f"status: {workspace.subscription_status} -> {status}")
        if plan and workspace.subscription_plan != plan:
            changes.append(f"plan: {workspace.subscription_plan} -> {plan}")
        return changes

    def _apply_changes(
        self,
        workspace: Workspace,
        customer_id: str,
        changes: list[str],
        dry_run: bool,
        results: dict,
    ) -> None:
        """Apply sync changes to workspace."""
        change_str = ", ".join(changes)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"  WOULD UPDATE: {workspace.name} - {change_str}")
            )
            results["synced"] += 1
            return

        if BillingService.sync_workspace_from_stripe(customer_id):
            self.stdout.write(
                self.style.SUCCESS(f"  SYNCED: {workspace.name} - {change_str}")
            )
            results["synced"] += 1
        else:
            self.stdout.write(
                self.style.ERROR(f"  ERROR: Failed to sync {workspace.name}")
            )
            results["errors"] += 1

    def _print_summary(self, results: dict, dry_run: bool) -> None:
        """Print a summary of operations performed.

        Args:
            results: Dictionary with operation counts.
            dry_run: Whether this was a dry run.
        """
        self.stdout.write("")
        self.stdout.write("=" * 50)

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN SUMMARY"))
            self.stdout.write(f"  Would sync {results['synced']} workspace(s)")
        else:
            self.stdout.write(self.style.SUCCESS("SUMMARY"))
            self.stdout.write(f"  Synced {results['synced']} workspace(s)")

        if results["skipped_no_workspace"]:
            self.stdout.write(
                f"  Skipped {results['skipped_no_workspace']} (no matching workspace)"
            )

        if results["errors"]:
            self.stdout.write(self.style.ERROR(f"  Errors: {results['errors']}"))

        self.stdout.write("=" * 50)

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
from webhooks.services.billing import map_stripe_status

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
            # status="all" so canceled subscriptions are returned too;
            # otherwise a workspace whose only subscription was canceled is
            # never seen here and stays "active" locally forever.
            params: dict = {
                "limit": 100,
                "status": "all",
                "expand": ["data.items.data.price"],
            }
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

    def _group_subscriptions_by_customer(
        self, subscriptions: list
    ) -> dict[str, list[stripe.Subscription]]:
        """Group every subscription by its Stripe customer id.

        All subscriptions are retained (not collapsed to one per customer)
        so that per-workspace selection can honor a stored
        ``stripe_subscription_id`` rather than an arbitrary heuristic.

        Args:
            subscriptions: Subscriptions fetched from Stripe.

        Returns:
            Mapping of customer id to its list of subscriptions, preserving
            the order returned by Stripe.
        """
        customer_subscriptions: dict[str, list[stripe.Subscription]] = {}

        for sub in subscriptions:
            customer_id = sub.customer
            if isinstance(customer_id, stripe.Customer):
                customer_id = customer_id.id

            customer_subscriptions.setdefault(customer_id, []).append(sub)

        return customer_subscriptions

    def _most_relevant_subscription(
        self, subs: list[stripe.Subscription]
    ) -> stripe.Subscription:
        """Pick a subscription using the live-first fallback heuristic.

        Prefers a live (active/trialing/past_due) subscription; otherwise
        returns the first one so a customer whose only subscription was
        canceled still downgrades the workspace instead of being skipped.

        Args:
            subs: Non-empty list of a customer's subscriptions.

        Returns:
            The selected subscription.
        """
        for sub in subs:
            if sub.status in ("active", "trialing", "past_due"):
                return sub
        return subs[0]

    def _select_subscription(
        self, workspace: Workspace, subs: list[stripe.Subscription]
    ) -> stripe.Subscription:
        """Choose the subscription that reflects the workspace's billing.

        When the workspace records a ``stripe_subscription_id``, the
        subscription with that id wins so an add-on or duplicate
        subscription on the same customer can't overwrite the plan billing
        is actually tied to. The live-first heuristic is used only when no
        id is stored or the stored id no longer matches any subscription.

        Args:
            workspace: Workspace being synced.
            subs: Non-empty list of the customer's subscriptions.

        Returns:
            The selected subscription.
        """
        stored_id = workspace.stripe_subscription_id
        if stored_id:
            for sub in subs:
                if sub.id == stored_id:
                    return sub

        return self._most_relevant_subscription(subs)

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

        customer_subscriptions = self._group_subscriptions_by_customer(subscriptions)
        customer_count = len(customer_subscriptions)
        self.stdout.write(f"Processing {customer_count} unique customer(s)")
        self.stdout.write("-" * 50)

        for customer_id, subs in customer_subscriptions.items():
            self._process_customer(customer_id, subs, dry_run, results)

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

    def _process_customer(
        self,
        customer_id: str,
        subs: list[stripe.Subscription],
        dry_run: bool,
        results: dict,
    ) -> None:
        """Process a customer's subscriptions and sync to its workspace.

        Args:
            customer_id: Stripe customer ID.
            subs: The customer's subscriptions (non-empty).
            dry_run: If True, don't make actual changes.
            results: Dictionary to track operation counts.
        """
        try:
            workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()

            if not workspace:
                self.stdout.write(f"  SKIP: No workspace for customer {customer_id}")
                results["skipped_no_workspace"] += 1
                return

            sub = self._select_subscription(workspace, subs)
            internal_status = map_stripe_status(sub.status)
            plan_name = self._normalize_plan_name(self._get_product_name(sub))

            changes = self._get_changes(workspace, internal_status, plan_name)

            if not changes:
                self.stdout.write(
                    f"  OK: {workspace.name} ({customer_id}) - already in sync"
                )
                results["synced"] += 1
                return

            self._apply_changes(
                workspace, sub, internal_status, plan_name, changes, dry_run, results
            )

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
        sub: stripe.Subscription,
        status: str,
        plan: str | None,
        changes: list[str],
        dry_run: bool,
        results: dict,
    ) -> None:
        """Persist the state derived from the selected subscription.

        The state from ``sub`` (the subscription chosen for this workspace)
        is written directly, so honoring the stored ``stripe_subscription_id``
        during selection actually takes effect instead of being re-derived
        from an arbitrary subscription.

        Args:
            workspace: Workspace to update.
            sub: The subscription selected for this workspace.
            status: Internal subscription status derived from ``sub``.
            plan: Normalized plan name derived from ``sub``, or None.
            changes: Human-readable change descriptions for output.
            dry_run: If True, don't make actual changes.
            results: Dictionary to track operation counts.
        """
        change_str = ", ".join(changes)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"  WOULD UPDATE: {workspace.name} - {change_str}")
            )
            results["synced"] += 1
            return

        update_data: dict[str, Any] = {"subscription_status": status}
        if plan:
            update_data["subscription_plan"] = plan
        if sub.id:
            update_data["stripe_subscription_id"] = sub.id

        Workspace.objects.filter(id=workspace.id).update(**update_data)
        self.stdout.write(
            self.style.SUCCESS(f"  SYNCED: {workspace.name} - {change_str}")
        )
        results["synced"] += 1

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

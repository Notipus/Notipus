"""Management command to set up Stripe products and prices for plans.

This command creates Stripe Products and Prices for each paid plan in the
database, then updates the Plan model with the actual Stripe Price IDs.
"""

import json
import logging
from decimal import Decimal
from typing import Any, cast

from core.models import Plan
from core.services.stripe import StripeAPI
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Django management command to sync plans with Stripe.

    Creates Stripe Products and Prices for paid plans and updates
    the database with the resulting Price IDs.

    Usage:
        python manage.py setup_stripe_plans
        python manage.py setup_stripe_plans --dry-run
        python manage.py setup_stripe_plans --plan basic
        python manage.py setup_stripe_plans --force
        python manage.py setup_stripe_plans --archive-plan trial
    """

    help = "Set up Stripe Products and Prices for subscription plans"

    def add_arguments(self, parser):
        """Add command line arguments.

        Args:
            parser: The argument parser instance.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be created without making changes",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Recreate prices even if they already exist",
        )
        parser.add_argument(
            "--plan",
            type=str,
            help="Only sync a specific plan by name (e.g., basic, pro, enterprise)",
        )
        parser.add_argument(
            "--archive-plan",
            type=str,
            help="Archive a plan in Stripe (deactivates product and prices)",
        )

    def handle(self, *args, **options):
        """Execute the command.

        Args:
            *args: Positional arguments.
            **options: Command options including dry_run, force, plan, archive_plan.
        """
        dry_run = options["dry_run"]
        force = options["force"]
        plan_name = options.get("plan")
        archive_plan_name = options.get("archive_plan")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )
            self.stdout.write("")

        # Initialize Stripe API
        stripe_api = StripeAPI()

        # Verify Stripe connection
        account_info = stripe_api.get_account_info()
        if not account_info:
            raise CommandError(
                "Failed to connect to Stripe. Check your STRIPE_SECRET_KEY."
            )

        self.stdout.write(
            f"Connected to Stripe account: {account_info.get('id', 'unknown')}"
        )
        self.stdout.write("")

        # Handle archive operation if requested
        if archive_plan_name:
            self._archive_plan(archive_plan_name, stripe_api, dry_run)
            return

        # Get plans to process
        plans = self._get_plans_to_process(plan_name)

        if not plans:
            self.stdout.write(self.style.WARNING("No paid plans found to process."))
            return

        self.stdout.write(f"Found {len(plans)} plan(s) to process:")
        for plan in plans:
            self.stdout.write(f"  - {plan.display_name} (${plan.price_monthly}/month)")
        self.stdout.write("")

        # Process each plan
        results = {
            "created_products": 0,
            "created_prices": 0,
            "updated_plans": 0,
            "skipped": 0,
            "errors": 0,
        }

        for plan in plans:
            try:
                self._process_plan(plan, stripe_api, dry_run, force, results)
            except Exception as e:
                results["errors"] += 1
                self.stdout.write(
                    self.style.ERROR(f"Error processing {plan.name}: {e!s}")
                )
                logger.exception(f"Error processing plan {plan.name}")

        # Print summary
        self._print_summary(results, dry_run)

    def _get_plans_to_process(self, plan_name: str | None) -> list[Plan]:
        """Get the list of plans to process.

        Args:
            plan_name: Optional specific plan name to filter by.

        Returns:
            List of Plan objects to process.
        """
        # Get active plans with price > 0 (paid plans only)
        queryset = Plan.objects.filter(is_active=True, price_monthly__gt=0)

        if plan_name:
            queryset = queryset.filter(name=plan_name)
            if not queryset.exists():
                raise CommandError(
                    f"Plan '{plan_name}' not found or is not a paid plan."
                )

        return list(queryset.order_by("price_monthly"))

    def _process_plan(
        self,
        plan: Plan,
        stripe_api: StripeAPI,
        dry_run: bool,
        force: bool,
        results: dict,
    ) -> None:
        """Process a single plan - create product and prices in Stripe.

        Args:
            plan: The Plan model instance.
            stripe_api: The StripeAPI client.
            dry_run: If True, don't make actual changes.
            force: If True, recreate prices even if they exist.
            results: Dictionary to track operation counts.
        """
        self.stdout.write(f"\nProcessing: {plan.display_name}")
        self.stdout.write("-" * 40)

        # Check if plan already has Stripe prices
        has_monthly = bool(plan.stripe_price_id_monthly)
        has_yearly = bool(plan.stripe_price_id_yearly)

        if has_monthly and has_yearly and not force:
            # Verify prices still exist in Stripe
            monthly_price = stripe_api.get_price_by_lookup_key(f"{plan.name}_monthly")
            yearly_price = stripe_api.get_price_by_lookup_key(f"{plan.name}_yearly")

            if monthly_price and yearly_price:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Skipping - already has valid Stripe prices:\n"
                        f"    Monthly: {plan.stripe_price_id_monthly}\n"
                        f"    Yearly: {plan.stripe_price_id_yearly}"
                    )
                )
                results["skipped"] += 1
                return

        # Get or create the Stripe Product
        product = self._get_or_create_product(plan, stripe_api, dry_run, results)
        if not product and not dry_run:
            results["errors"] += 1
            self.stdout.write(
                self.style.ERROR(f"  Failed to create product for {plan.name}")
            )
            return

        product_id = product["id"] if product else "prod_DRYRUN"

        # Create monthly price
        monthly_price = self._create_price_if_needed(
            plan=plan,
            stripe_api=stripe_api,
            product_id=product_id,
            interval="month",
            amount=plan.price_monthly,
            dry_run=dry_run,
            force=force,
            results=results,
        )

        # Create yearly price
        yearly_price = self._create_price_if_needed(
            plan=plan,
            stripe_api=stripe_api,
            product_id=product_id,
            interval="year",
            amount=plan.price_yearly or (plan.price_monthly * 10),  # Default to 10x
            dry_run=dry_run,
            force=force,
            results=results,
        )

        # Update Plan model with Stripe IDs
        if not dry_run:
            updated = False
            if monthly_price and monthly_price.get("id"):
                plan.stripe_price_id_monthly = monthly_price["id"]
                updated = True
            if yearly_price and yearly_price.get("id"):
                plan.stripe_price_id_yearly = yearly_price["id"]
                updated = True

            if updated:
                plan.save(
                    update_fields=["stripe_price_id_monthly", "stripe_price_id_yearly"]
                )
                results["updated_plans"] += 1
                self.stdout.write(
                    self.style.SUCCESS("  Updated plan with Stripe price IDs")
                )
        else:
            self.stdout.write("  Would update plan with new Stripe price IDs")

    def _get_or_create_product(
        self,
        plan: Plan,
        stripe_api: StripeAPI,
        dry_run: bool,
        results: dict,
    ) -> dict[Any, Any] | None:
        """Get existing Stripe Product or create a new one.

        Args:
            plan: The Plan model instance.
            stripe_api: The StripeAPI client.
            dry_run: If True, don't make actual changes.
            results: Dictionary to track operation counts.

        Returns:
            Product data dictionary or None on failure.
        """
        # Check if product already exists by metadata
        existing = stripe_api.get_product_by_metadata("plan_name", plan.name)

        if existing:
            self.stdout.write(f"  Found existing product: {existing['id']}")
            return cast("dict[Any, Any]", existing)

        # Create new product
        if dry_run:
            self.stdout.write(
                f"  Would create product: {plan.display_name}\n"
                f"    Description: {plan.description or 'N/A'}\n"
                f"    Metadata: plan_name={plan.name}"
            )
            return {"id": "prod_DRYRUN", "name": plan.display_name}

        # Build metadata
        metadata = {
            "plan_name": plan.name,
            "max_users": str(plan.max_users),
            "max_integrations": str(plan.max_integrations),
            "max_monthly_notifications": str(plan.max_monthly_notifications),
        }

        # Add features as JSON string
        if plan.features:
            metadata["features"] = json.dumps(plan.features)

        product = stripe_api.create_product(
            name=plan.display_name,
            description=plan.description or f"Notipus {plan.display_name}",
            metadata=metadata,
        )

        if product:
            results["created_products"] += 1
            self.stdout.write(self.style.SUCCESS(f"  Created product: {product['id']}"))

        return cast("dict[Any, Any] | None", product)

    def _create_price_if_needed(
        self,
        plan: Plan,
        stripe_api: StripeAPI,
        product_id: str,
        interval: str,
        amount: Decimal,
        dry_run: bool,
        force: bool,
        results: dict,
    ) -> dict[Any, Any] | None:
        """Create a Stripe Price if needed.

        Args:
            plan: The Plan model instance.
            stripe_api: The StripeAPI client.
            product_id: The Stripe Product ID.
            interval: Billing interval ('month' or 'year').
            amount: Price amount in dollars.
            dry_run: If True, don't make actual changes.
            force: If True, recreate even if exists.
            results: Dictionary to track operation counts.

        Returns:
            Price data dictionary or None.
        """
        lookup_key = f"{plan.name}_{interval}ly"
        amount_cents = int(amount * 100)

        # Check if price already exists
        if not force:
            existing = stripe_api.get_price_by_lookup_key(lookup_key)
            if existing:
                self.stdout.write(
                    f"  Found existing {interval}ly price: {existing['id']}"
                )
                return cast("dict[Any, Any]", existing)

        if dry_run:
            self.stdout.write(
                f"  Would create {interval}ly price:\n"
                f"    Amount: ${amount:.2f} ({amount_cents} cents)\n"
                f"    Lookup key: {lookup_key}"
            )
            return {"id": f"price_DRYRUN_{interval}ly"}

        price = stripe_api.create_price(
            product_id=product_id,
            unit_amount=amount_cents,
            currency="usd",
            interval=interval,
            lookup_key=lookup_key,
        )

        if price:
            results["created_prices"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  Created {interval}ly price: {price['id']} "
                    f"(${amount:.2f}/{interval})"
                )
            )

        return cast("dict[Any, Any] | None", price)

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
            self.stdout.write(
                f"  Would create {results['created_products']} product(s)"
            )
            self.stdout.write(f"  Would create {results['created_prices']} price(s)")
        else:
            self.stdout.write(self.style.SUCCESS("SUMMARY"))
            self.stdout.write(f"  Created {results['created_products']} product(s)")
            self.stdout.write(f"  Created {results['created_prices']} price(s)")
            self.stdout.write(f"  Updated {results['updated_plans']} plan(s)")

        self.stdout.write(f"  Skipped {results['skipped']} plan(s)")

        if results["errors"]:
            self.stdout.write(self.style.ERROR(f"  Errors: {results['errors']}"))

        self.stdout.write("=" * 50)

    def _format_price_info(self, price: dict) -> str:
        """Format price information for display.

        Args:
            price: Price dictionary from Stripe.

        Returns:
            Formatted price info string.
        """
        price_info = f"{price['id']}"
        if price.get("lookup_key"):
            price_info += f" ({price['lookup_key']})"
        if price.get("recurring"):
            interval = price["recurring"].get("interval", "unknown")
            price_info += f" - {interval}ly"
        return price_info

    def _archive_prices(
        self, prices: list[dict], stripe_api: StripeAPI, dry_run: bool
    ) -> None:
        """Archive all prices in the list.

        Args:
            prices: List of price dictionaries to archive.
            stripe_api: The StripeAPI client.
            dry_run: If True, don't make actual changes.
        """
        for price in prices:
            price_info = self._format_price_info(price)
            if dry_run:
                self.stdout.write(f"  Would archive price: {price_info}")
            elif stripe_api.archive_price(price["id"]):
                self.stdout.write(self.style.SUCCESS(f"  Archived price: {price_info}"))
            else:
                self.stdout.write(
                    self.style.ERROR(f"  Failed to archive price: {price_info}")
                )

    def _archive_plan(
        self, plan_name: str, stripe_api: StripeAPI, dry_run: bool
    ) -> None:
        """Archive a plan's product and prices in Stripe.

        Archives (deactivates) the Stripe product and all associated prices
        for the specified plan. Archived products/prices cannot be used for
        new subscriptions but existing subscriptions remain active.

        Args:
            plan_name: The plan name to archive (e.g., 'trial').
            stripe_api: The StripeAPI client.
            dry_run: If True, don't make actual changes.
        """
        self.stdout.write(f"Archiving plan: {plan_name}")
        self.stdout.write("-" * 40)

        # Find product by metadata
        product = stripe_api.get_product_by_metadata("plan_name", plan_name)
        if not product:
            self.stdout.write(
                self.style.WARNING(f"No Stripe product found for plan '{plan_name}'")
            )
            self.stdout.write("  The plan may not have been synced to Stripe yet.")
            return

        self.stdout.write(f"Found product: {product['id']} ({product['name']})")

        # Archive all prices for this product
        prices = stripe_api.list_prices_for_product(product["id"])
        if prices:
            self.stdout.write(f"Found {len(prices)} price(s) to archive:")
            self._archive_prices(prices, stripe_api, dry_run)
        else:
            self.stdout.write("  No active prices found for this product.")

        # Archive the product
        if dry_run:
            self.stdout.write(f"Would archive product: {product['id']}")
        elif stripe_api.archive_product(product["id"]):
            self.stdout.write(self.style.SUCCESS(f"Archived product: {product['id']}"))
        else:
            self.stdout.write(
                self.style.ERROR(f"Failed to archive product: {product['id']}")
            )

        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN - No changes were made to Stripe")
            )
        else:
            msg = f"Successfully archived plan '{plan_name}' in Stripe"
            self.stdout.write(self.style.SUCCESS(msg))

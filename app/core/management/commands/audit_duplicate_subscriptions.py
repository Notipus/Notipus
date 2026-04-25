"""Find Stripe customers with multiple active subscriptions.

A historical bug in the checkout flow allowed a workspace to start a new
subscription even when one was already active, leaving customers with two
or more parallel subscriptions all billing in parallel. This command lists
those customers so they can be reconciled (cancel the duplicates, refund
the overlapping charges). Read-only by default.
"""

import logging
from argparse import ArgumentParser
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import stripe
from core.models import Workspace
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

LIVE_STATUSES = ("active", "trialing", "past_due")


class Command(BaseCommand):
    """Report customers with two or more live Stripe subscriptions.

    Usage:
        # Scan all customers (slow on large accounts)
        python manage.py audit_duplicate_subscriptions

        # Spot-check a single customer
        python manage.py audit_duplicate_subscriptions --customer cus_ABC123
    """

    help = "Report Stripe customers with 2+ active subscriptions"

    def add_arguments(self, parser: "ArgumentParser") -> None:
        parser.add_argument(
            "--customer",
            metavar="CUSTOMER_ID",
            help=(
                "Limit the audit to a single Stripe customer id. "
                "Skips the global scan; useful for spot-checks against a "
                "specific customer reported via support."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.api_version = settings.STRIPE_API_VERSION

        try:
            account = stripe.Account.retrieve()
            self.stdout.write(f"Connected to Stripe account: {account.id}")
        except stripe.AuthenticationError as err:
            raise CommandError(
                "Failed to connect to Stripe. Check your STRIPE_SECRET_KEY."
            ) from err

        only_customer = options.get("customer")
        subscriptions = self._fetch_subscriptions(only_customer=only_customer)
        by_customer: dict[str, list[stripe.Subscription]] = defaultdict(list)
        for sub in subscriptions:
            customer_id = sub.customer
            if hasattr(customer_id, "id"):
                customer_id = customer_id.id
            by_customer[customer_id].append(sub)

        duplicates = {
            cid: subs
            for cid, subs in by_customer.items()
            if sum(1 for s in subs if s.status in LIVE_STATUSES) >= 2
        }

        self.stdout.write("")
        if not duplicates:
            self.stdout.write(self.style.SUCCESS("No duplicate subscriptions found."))
            return

        self.stdout.write(
            self.style.WARNING(
                f"Found {len(duplicates)} customer(s) with 2+ live subscriptions:"
            )
        )
        self.stdout.write("=" * 70)

        for customer_id, subs in duplicates.items():
            self._print_customer_block(customer_id, subs)

        self.stdout.write("=" * 70)
        self.stdout.write(
            "Next: cancel each duplicate via Stripe Dashboard or "
            "stripe.Subscription.delete(...), refund the overlapping charges, "
            "then run sync_stripe_subscriptions to reconcile local state."
        )

    def _fetch_subscriptions(
        self, only_customer: str | None = None
    ) -> list[stripe.Subscription]:
        if only_customer:
            self.stdout.write(
                f"Fetching subscriptions from Stripe for customer {only_customer}..."
            )
        else:
            self.stdout.write("Fetching subscriptions from Stripe (all customers)...")

        result: list[stripe.Subscription] = []
        starting_after: str | None = None

        while True:
            params: dict[str, Any] = {"limit": 100, "status": "all"}
            if only_customer:
                params["customer"] = only_customer
            if starting_after:
                params["starting_after"] = starting_after

            response = stripe.Subscription.list(**params)
            result.extend(response.data)

            if not response.has_more or not response.data:
                break
            starting_after = response.data[-1].id

        self.stdout.write(f"Fetched {len(result)} subscription(s)")
        return result

    def _print_customer_block(
        self, customer_id: str, subs: list[stripe.Subscription]
    ) -> None:
        workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()
        emails = self._workspace_emails(workspace)

        live = [s for s in subs if s.status in LIVE_STATUSES]
        live.sort(key=lambda s: getattr(s, "created", 0))

        self.stdout.write("")
        self.stdout.write(f"Customer: {customer_id}")
        if workspace:
            self.stdout.write(
                f"  Workspace: {workspace.name} (uuid={workspace.uuid}, "
                f"id={workspace.id})"
            )
        else:
            self.stdout.write("  Workspace: <no local workspace matches this customer>")
        if emails:
            self.stdout.write(f"  Members: {', '.join(emails)}")
        self.stdout.write(f"  Live subscriptions ({len(live)}):")

        for sub in live:
            created = getattr(sub, "created", None)
            created_str = (
                datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                if created
                else "?"
            )
            amount = self._sub_amount(sub)
            self.stdout.write(
                f"    - {sub.id}  status={sub.status}  "
                f"created={created_str}  ~={amount}"
            )

    @staticmethod
    def _workspace_emails(workspace: Workspace | None) -> list[str]:
        if not workspace:
            return []
        return [
            m.user.email
            for m in workspace.members.select_related("user").all()
            if m.user.email
        ]

    @staticmethod
    def _sub_amount(sub: stripe.Subscription) -> str:
        try:
            items = getattr(sub, "items", None)
            data = getattr(items, "data", []) if items else []
            if not data:
                return "?"
            price = getattr(data[0], "price", None)
            unit = getattr(price, "unit_amount", None) if price else None
            currency = (getattr(price, "currency", "usd") or "usd").upper()
            if unit is None:
                return "?"
            return f"{unit / 100:.2f} {currency}"
        except Exception:
            return "?"

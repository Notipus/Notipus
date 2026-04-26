"""Find Stripe customers with multiple live subscriptions.

A historical bug in the checkout flow allowed a workspace to start a new
subscription even when one was already live, leaving customers with two
or more parallel subscriptions all billing in parallel. "Live" here means
any of active/trialing/past_due — past_due in particular is easy to miss
and still bills the customer. This command lists those customers so they
can be reconciled (cancel the duplicates, refund the overlapping charges).
Read-only by default.
"""

from argparse import ArgumentParser
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import stripe
from core.models import Workspace
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

LIVE_STATUSES = ("active", "trialing", "past_due")


class Command(BaseCommand):
    """Report customers with two or more live Stripe subscriptions.

    Usage:
        # Scan all customers (slow on large accounts)
        python manage.py audit_duplicate_subscriptions

        # Spot-check a single customer
        python manage.py audit_duplicate_subscriptions --customer cus_ABC123

        # Limit the scan to recent subscriptions and a max number of records
        python manage.py audit_duplicate_subscriptions \
            --created-after 2026-01-01 --max-results 5000
    """

    help = (
        "Report Stripe customers with 2+ live subscriptions (active/trialing/past_due)"
    )

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
        parser.add_argument(
            "--created-after",
            metavar="DATE",
            help=(
                "Only inspect subscriptions created on or after this date "
                "(YYYY-MM-DD or unix timestamp). Lets a large account scan "
                "skip ancient subscriptions and stay under Stripe rate limits."
            ),
        )
        parser.add_argument(
            "--max-results",
            type=int,
            metavar="N",
            help=(
                "Stop after inspecting N subscriptions. Useful as a guard "
                "rail when running against a very large Stripe account; "
                "the audit aborts early once the cap is reached."
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
        created_after = self._parse_created_after(options.get("created_after"))
        max_results = options.get("max_results")

        by_customer = self._stream_live_subs(
            only_customer=only_customer,
            created_after=created_after,
            max_results=max_results,
        )

        duplicates = {cid: subs for cid, subs in by_customer.items() if len(subs) >= 2}

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

    @staticmethod
    def _parse_created_after(raw: str | None) -> int | None:
        """Convert a CLI date argument to a unix timestamp.

        Accepts ``YYYY-MM-DD`` (interpreted as UTC midnight) or a raw
        unix timestamp. Returns None when no value was supplied.
        """
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as err:
            raise CommandError(
                f"--created-after: expected YYYY-MM-DD or unix timestamp, got {raw!r}"
            ) from err
        return int(dt.timestamp())

    def _stream_live_subs(
        self,
        only_customer: str | None,
        created_after: int | None,
        max_results: int | None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Stream Stripe subscriptions and group live ones by customer.

        Uses auto_paging_iter so we don't buffer the full result set in
        memory, and only retains a compact record for each *live* sub —
        canceled/incomplete ones are discarded as they stream by, since
        they can't form a duplicate.
        """
        if only_customer:
            self.stdout.write(
                f"Fetching subscriptions from Stripe for customer {only_customer}..."
            )
        else:
            self.stdout.write("Fetching subscriptions from Stripe (all customers)...")

        params: dict[str, Any] = {"limit": 100, "status": "all"}
        if only_customer:
            params["customer"] = only_customer
        if created_after is not None:
            params["created"] = {"gte": created_after}

        by_customer: dict[str, list[dict[str, Any]]] = defaultdict(list)
        scanned = 0
        try:
            for sub in stripe.Subscription.list(**params).auto_paging_iter():
                # Check the cap *before* counting this sub so the reported
                # `scanned` value matches the number we actually inspected
                # (i.e. exactly --max-results, not max_results+1).
                if max_results is not None and scanned >= max_results:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Reached --max-results={max_results}, stopping early. "
                            "Re-run with --created-after or a higher --max-results "
                            "to continue."
                        )
                    )
                    break
                scanned += 1
                if sub.status not in LIVE_STATUSES:
                    continue

                customer_id = sub.customer
                if hasattr(customer_id, "id"):
                    customer_id = customer_id.id
                by_customer[customer_id].append(self._compact_sub(sub))
        except stripe.StripeError as exc:
            # Surface a clean, actionable message to the operator instead
            # of a stack trace if Stripe is unavailable mid-scan. The
            # partial `by_customer` is discarded so a half-paginated scan
            # can't be mistaken for a complete one.
            raise CommandError(
                f"Failed to fetch subscriptions from Stripe after "
                f"scanning {scanned} record(s): {exc}. Please retry."
            ) from exc

        self.stdout.write(f"Scanned {scanned} subscription(s)")
        return by_customer

    @staticmethod
    def _compact_sub(sub: stripe.Subscription) -> dict[str, Any]:
        """Reduce a Stripe Subscription to the fields the report prints.

        Avoids retaining the full Stripe object for every live subscription,
        which keeps memory bounded even on accounts with thousands of subs.
        """
        amount: str = "?"
        try:
            items = getattr(sub, "items", None)
            data = getattr(items, "data", []) if items else []
            if data:
                price = getattr(data[0], "price", None)
                unit = getattr(price, "unit_amount", None) if price else None
                currency = (getattr(price, "currency", "usd") or "usd").upper()
                if unit is not None:
                    amount = f"{unit / 100:.2f} {currency}"
        except Exception:
            amount = "?"

        return {
            "id": sub.id,
            "status": sub.status,
            "created": getattr(sub, "created", None),
            "amount": amount,
        }

    def _print_customer_block(
        self, customer_id: str, subs: list[dict[str, Any]]
    ) -> None:
        workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()
        emails = self._workspace_emails(workspace)

        subs_sorted = sorted(subs, key=lambda s: s.get("created") or 0)

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
        self.stdout.write(f"  Live subscriptions ({len(subs_sorted)}):")

        for sub in subs_sorted:
            created = sub.get("created")
            created_str = (
                datetime.fromtimestamp(created, tz=UTC).isoformat() if created else "?"
            )
            self.stdout.write(
                f"    - {sub['id']}  status={sub['status']}  "
                f"created={created_str}  ~={sub['amount']}"
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

"""Billing service for handling Stripe webhook events.

This module processes billing-related webhook events from Stripe
and updates workspace subscription status accordingly.

Handlers distinguish expected no-ops (missing customer id, no matching
workspace) from unexpected errors: expected no-ops are logged and return
normally, while unexpected errors (e.g. database failures) propagate so
the webhook view returns 5xx and Stripe redelivers the event.

Handlers that write webhook-derived state and then re-sync from Stripe
serialize per customer via a short Redis lock (see ``stripe_sync_lock``)
so concurrent webhooks can't interleave their write-then-sync sequences
and leave the workspace on stale data.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, cast

import stripe
from core.models import Workspace
from core.services.stripe import StripeAPI
from django.db.models import Q

from .redis_client import get_raw_redis_client

logger = logging.getLogger(__name__)

# Map Stripe statuses to our internal statuses.
#
# "incomplete" means the first invoice was never paid (card declined,
# SCA abandoned) — it is neither a trial nor an active subscription, so
# it maps to "suspended": a non-trial, non-active state that keeps
# feature access off until Stripe transitions the subscription to
# active/trialing (payment completed) or incomplete_expired (gave up).
# "paused" (trial ended without a payment method) likewise means the
# customer never paid, so it also maps to "suspended".
STRIPE_STATUS_MAPPING: dict[str, str] = {
    "active": "active",
    "trialing": "trial",
    "past_due": "past_due",
    "canceled": "cancelled",
    "unpaid": "past_due",
    "incomplete": "suspended",
    "incomplete_expired": "cancelled",
    "paused": "suspended",
}


def map_stripe_status(stripe_status: str) -> str:
    """Map a Stripe subscription status to an internal workspace status.

    Unknown statuses (e.g. one Stripe adds in a future API version) map
    to "suspended" with a warning instead of defaulting to "active":
    granting feature access on a status we don't understand is exactly
    the failure mode that let never-paid states (incomplete, paused)
    through before. Suspension is recoverable — the next webhook or sync
    with a mapped status corrects it.

    Args:
        stripe_status: Status string from a Stripe subscription.

    Returns:
        Internal Workspace.STATUS_CHOICES key.
    """
    internal_status = STRIPE_STATUS_MAPPING.get(stripe_status)
    if internal_status is None:
        logger.warning(
            f"Unknown Stripe subscription status {stripe_status!r}; "
            f"treating workspace as suspended"
        )
        return "suspended"
    return internal_status


# Redis lock tuning for the per-customer write-then-sync serialization.
# The lock auto-expires after LOCK_TIMEOUT even if the holder crashed;
# waiters give up after LOCK_BLOCKING and let Stripe redeliver the event.
_SYNC_LOCK_TIMEOUT_SECONDS = 30
_SYNC_LOCK_BLOCKING_SECONDS = 10

# Plan keys accepted for Workspace.subscription_plan.
_VALID_PLAN_KEYS = frozenset(key for key, _label in Workspace.STRIPE_PLANS)


class StripeSyncLockTimeout(Exception):
    """Raised when the per-customer billing lock can't be acquired in time.

    Propagates out of the webhook handler so the view returns 5xx and
    Stripe redelivers the event once the concurrent handler finishes.
    """


def _get_lock_client() -> Any | None:
    """Return the raw Redis client backing the default cache, if any.

    Returns:
        A Redis client supporting ``lock()``, or None when the cache
        backend is not Redis (e.g. DummyCache in tests) so callers can
        fail open.
    """
    client = get_raw_redis_client()
    if client is None:
        return None
    # Don't treat MagicMocks from broadly-patched test caches as clients.
    if "Mock" in client.__class__.__name__:
        return None
    if not hasattr(client, "lock"):
        return None
    return client


@contextmanager
def stripe_sync_lock(customer_id: str) -> Iterator[None]:
    """Serialize billing writes for one Stripe customer.

    Wraps a handler's write-then-sync sequence in a short Redis lock keyed
    on the customer id (one workspace per Stripe customer) so two
    concurrent webhooks can't interleave — e.g. webhook A's late
    ``sync_workspace_from_stripe`` overwriting webhook B's newer plan.

    Fails open (runs unlocked) when Redis is unavailable, since processing
    the webhook slightly racily beats dropping it. If the lock is held by
    another handler for longer than the blocking timeout, raises
    StripeSyncLockTimeout so the webhook 5xxs and Stripe redelivers.

    Args:
        customer_id: The Stripe customer ID to serialize on.

    Yields:
        None. The handler body runs while the lock is held.

    Raises:
        StripeSyncLockTimeout: Lock held by another handler beyond the
            blocking timeout.
    """
    client = _get_lock_client()
    if client is None:
        yield
        return

    lock = client.lock(
        f"stripe_sync_lock:{customer_id}",
        timeout=_SYNC_LOCK_TIMEOUT_SECONDS,
        blocking_timeout=_SYNC_LOCK_BLOCKING_SECONDS,
    )
    try:
        acquired = lock.acquire()
    except Exception as e:
        # Redis hiccup mid-acquire: fail open rather than dropping the event.
        logger.warning(
            f"Could not acquire stripe sync lock for {customer_id}: {e!s}; "
            f"proceeding without lock"
        )
        yield
        return

    if not acquired:
        raise StripeSyncLockTimeout(
            f"Timed out waiting for stripe sync lock for customer {customer_id}"
        )

    try:
        yield
    finally:
        try:
            lock.release()
        except Exception as e:
            # Lock will expire on its own after the timeout.
            logger.warning(f"Error releasing stripe sync lock: {e!s}")


class BillingService:
    """Service for handling billing-related webhook events from Stripe.

    Provides static methods for processing various subscription and
    payment events and updating workspace records.
    """

    @staticmethod
    def _get_active_subscription(subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
        """Get the most relevant subscription from a list.

        Prefers active/trialing subscriptions over cancelled ones.

        Args:
            subscriptions: List of subscription dictionaries.

        Returns:
            The most relevant subscription dictionary.
        """
        for sub in subscriptions:
            if sub["status"] in ("active", "trialing", "past_due"):
                return sub
        return subscriptions[0]

    @staticmethod
    def _extract_billing_anchor(sub_data: dict[str, Any]) -> int | None:
        """Extract the billing cycle anchor from subscription data.

        Every write site uses ``current_period_end`` (the next renewal
        timestamp). Writing ``current_period_start`` anywhere would let a
        late-retried ``subscription.created`` webhook regress the anchor
        to a past timestamp after a ``subscription.updated`` already wrote
        the future one.

        Args:
            sub_data: Subscription data from a Stripe webhook or API call.

        Returns:
            Unix timestamp of the period end, or None if absent.
        """
        return cast(int | None, sub_data.get("current_period_end"))

    @staticmethod
    def _normalize_plan_name(raw_plan_name: str) -> str | None:
        """Normalize a plan display string to a STRIPE_PLANS key.

        Converts values like "Enterprise Plan", "Notipus Pro Plan", or
        "PRO" to their canonical plan keys, and rejects anything that
        doesn't map to a known plan so arbitrary (user-suppliable)
        display strings never reach ``Workspace.subscription_plan``.

        Args:
            raw_plan_name: Plan name string, e.g. from checkout metadata.

        Returns:
            A valid plan key ("free"/"basic"/"pro"/"enterprise"), or None
            when the value doesn't normalize to a known plan.
        """
        normalized = (
            raw_plan_name.lower().replace("notipus ", "").replace(" plan", "").strip()
        )
        if normalized in _VALID_PLAN_KEYS:
            return normalized
        return None

    @staticmethod
    def _extract_plan_name_from_subscription(
        subscription: dict[str, Any],
    ) -> str | None:
        """Extract plan name from subscription items.

        Prefers Product metadata.plan_name (most reliable) over
        normalizing the product name string. Either source is validated
        against the STRIPE_PLANS choices; unrecognized values are
        rejected rather than written verbatim.

        Args:
            subscription: Subscription dictionary with items.

        Returns:
            Plan name string or None.
        """
        if not subscription.get("items"):
            return None

        first_item = subscription["items"][0]

        # Prefer plan_name from Product metadata (most reliable)
        for raw_name in (first_item.get("plan_name"), first_item.get("product_name")):
            if not raw_name:
                continue
            plan_name = BillingService._normalize_plan_name(raw_name)
            if plan_name:
                return plan_name
            logger.warning(
                f"Unrecognized plan name {raw_name!r} on subscription "
                f"{subscription.get('id')}; ignoring"
            )

        return None

    @staticmethod
    def sync_workspace_from_stripe(customer_id: str) -> bool:
        """Sync workspace subscription state from Stripe.

        Fetches the current subscription state directly from Stripe API
        and updates the workspace. This ensures we have accurate state
        even if webhooks were missed or processed out of order.

        Args:
            customer_id: The Stripe customer ID.

        Returns:
            True if sync was successful, False otherwise.

        Raises:
            Exception: Unexpected errors (e.g. database failures) propagate
                so webhook callers return 5xx and Stripe retries. Only
                Stripe API errors are swallowed (the sync is best-effort
                refinement after the primary update already succeeded).
        """
        workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()

        if not workspace:
            logger.warning(f"No workspace found for customer {customer_id} during sync")
            return False

        try:
            stripe_api = StripeAPI()
            subscriptions = stripe_api.get_customer_subscriptions(
                customer_id, status="all"
            )
        except stripe.StripeError as e:
            logger.error(f"Error syncing workspace from Stripe: {e!s}")
            return False

        if not subscriptions:
            logger.info(f"No subscriptions found for customer {customer_id}")
            return True

        active_sub = BillingService._get_active_subscription(subscriptions)
        stripe_status = active_sub.get("status", "active")
        internal_status = map_stripe_status(stripe_status)
        plan_name = BillingService._extract_plan_name_from_subscription(active_sub)

        # Build update data
        update_data: dict[str, Any] = {"subscription_status": internal_status}

        if plan_name:
            update_data["subscription_plan"] = plan_name

        if active_sub.get("id"):
            update_data["stripe_subscription_id"] = active_sub["id"]

        billing_anchor = BillingService._extract_billing_anchor(active_sub)
        if billing_anchor is not None:
            update_data["billing_cycle_anchor"] = billing_anchor

        if stripe_status == "trialing":
            # trial_end is authoritative; current_period_end only usually
            # coincides with it (support-extended trials diverge).
            trial_end = active_sub.get("trial_end") or active_sub.get(
                "current_period_end"
            )
            if trial_end:
                update_data["trial_end_date"] = datetime.fromtimestamp(
                    trial_end, tz=timezone.utc
                )

        Workspace.objects.filter(id=workspace.id).update(**update_data)

        logger.info(
            f"Synced workspace {workspace.name} from Stripe: "
            f"status={internal_status}, plan={plan_name}"
        )
        return True

    @staticmethod
    def _get_customer_id(data: dict[str, Any], data_type: str) -> str | None:
        """Extract customer ID from webhook data.

        Args:
            data: Webhook data dictionary.
            data_type: Description of data type for logging.

        Returns:
            Customer ID string, or None if not found.
        """
        customer_id = data.get("customer")
        if not customer_id:
            logger.error(f"Missing customer ID in {data_type} data")
            return None
        return cast(str, customer_id)

    @staticmethod
    def _claim_subscription_id(customer_id: str, sub_id: str | None) -> None:
        """Record sub_id as the workspace's subscription iff none is set.

        A webhook for an add-on subscription must not displace the
        recorded primary subscription id, so the write is conditional on
        the field being empty (atomic via the filtered UPDATE).
        sync_workspace_from_stripe — which derives the id from the
        customer's active subscription — remains the authority for
        changing an existing value.

        Args:
            customer_id: Stripe customer whose workspace may claim the id.
            sub_id: Subscription id from the webhook payload, if any.
        """
        if not sub_id:
            return
        Workspace.objects.filter(
            stripe_customer_id=customer_id, stripe_subscription_id=""
        ).update(stripe_subscription_id=sub_id)

    @staticmethod
    def handle_subscription_created(subscription: dict[str, Any]) -> None:
        """Handle subscription created event.

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        customer_id = BillingService._get_customer_id(subscription, "subscription")
        if not customer_id:
            return

        # Map the real Stripe status instead of assuming "active": a
        # subscription created in trialing/incomplete must not grant
        # active access (incomplete = first invoice never paid).
        stripe_status = subscription.get("status", "active")
        internal_status = map_stripe_status(stripe_status)

        # Don't set subscription_plan here - sync_workspace_from_stripe will
        # properly extract and normalize the plan name from the Product.
        # Previously this was setting plan_id (a Price ID) which is wrong.
        update_data: dict[str, Any] = {"subscription_status": internal_status}

        billing_anchor = BillingService._extract_billing_anchor(subscription)
        if billing_anchor is not None:
            update_data["billing_cycle_anchor"] = billing_anchor

        with stripe_sync_lock(customer_id):
            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(**update_data)

            if updated_count > 0:
                # Only claim the subscription id when none is recorded:
                # a created event for an add-on subscription must not
                # displace the workspace's primary one.
                BillingService._claim_subscription_id(
                    customer_id, subscription.get("id")
                )
                logger.info(
                    f"Subscription created for customer {customer_id}, syncing..."
                )
                # Sync full state from Stripe (properly extracts plan name)
                BillingService.sync_workspace_from_stripe(customer_id)
            else:
                logger.warning(f"No workspace found for customer {customer_id}")

    @staticmethod
    def handle_subscription_updated(subscription: dict[str, Any]) -> None:
        """Handle subscription updated event (plan changes, status changes).

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        customer_id = BillingService._get_customer_id(subscription, "subscription")
        if not customer_id:
            return

        # Extract subscription status
        status = subscription.get("status", "active")

        # Map Stripe statuses to our internal statuses
        internal_status = map_stripe_status(status)

        # Don't set subscription_plan here - sync_workspace_from_stripe will
        # properly extract and normalize the plan name from the Product.
        # Previously this was setting plan_id (a Price ID) which is wrong.
        update_data: dict[str, Any] = {"subscription_status": internal_status}

        # Update billing cycle anchor if present
        billing_anchor = BillingService._extract_billing_anchor(subscription)
        if billing_anchor is not None:
            update_data["billing_cycle_anchor"] = billing_anchor

        with stripe_sync_lock(customer_id):
            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(**update_data)

            if updated_count > 0:
                # Ensure a subscription id is recorded even if the sync
                # below fails (it's what scopes deletion/payment-failure
                # handling); only claims when currently empty.
                BillingService._claim_subscription_id(
                    customer_id, subscription.get("id")
                )
                logger.info(
                    f"Updated subscription status to {internal_status} "
                    f"for customer {customer_id}, syncing..."
                )
                # Sync full state from Stripe (properly extracts plan name)
                BillingService.sync_workspace_from_stripe(customer_id)
            else:
                logger.warning(f"No workspace found for customer {customer_id}")

    @staticmethod
    def handle_subscription_deleted(subscription: dict[str, Any]) -> None:
        """Handle subscription deleted/cancelled event.

        Only cancels the workspace when the deleted subscription is the
        one the workspace's billing state is derived from — a customer
        cancelling an add-on subscription must not lose main-product
        access. Either way the workspace is re-synced from Stripe so the
        final state reflects any remaining live subscription.

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        customer_id = BillingService._get_customer_id(subscription, "subscription")
        if not customer_id:
            return

        deleted_sub_id = subscription.get("id")

        with stripe_sync_lock(customer_id):
            workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()
            if workspace is None:
                logger.warning(f"No workspace found for customer {customer_id}")
                return

            if not deleted_sub_id:
                # Without an id we can't tell whether this deletion is the
                # workspace's own subscription — don't cancel on a guess;
                # let the sync below derive the true state from Stripe.
                logger.warning(
                    f"Subscription deleted event for customer {customer_id} "
                    f"has no subscription id; re-syncing without cancelling"
                )
                BillingService.sync_workspace_from_stripe(customer_id)
                return

            if (
                workspace.stripe_subscription_id
                and deleted_sub_id != workspace.stripe_subscription_id
            ):
                logger.info(
                    f"Deleted subscription {deleted_sub_id} does not match "
                    f"workspace subscription {workspace.stripe_subscription_id} "
                    f"for customer {customer_id}; re-syncing without cancelling"
                )
                BillingService.sync_workspace_from_stripe(customer_id)
                return

            Workspace.objects.filter(id=workspace.id).update(
                subscription_status="cancelled"
            )
            logger.info(f"Marked subscription as cancelled for customer {customer_id}")
            # Reconcile: if another live subscription exists on Stripe
            # (e.g. the recorded id was stale), sync restores it.
            BillingService.sync_workspace_from_stripe(customer_id)

    @staticmethod
    def _apply_paid_subscription_invoice(
        invoice: dict[str, Any], event_name: str
    ) -> None:
        """Apply a paid invoice to the workspace it belongs to.

        Mirrors handle_payment_failed's scoping: only invoices attached to
        the workspace's own subscription may change its state. Without
        this, a paid one-off or add-on invoice would reactivate a
        cancelled workspace, and the $0 invoice at trial start would flip
        a trialing workspace to "active" with payment_method_added=True.

        After the direct write, state is re-synced from Stripe under the
        per-customer lock so a late-retried paid-invoice event can't
        resurrect a workspace whose subscription has since been deleted
        (sync sees the canceled subscription and restores "cancelled").
        The sync is also what advances billing_cycle_anchor: it reads the
        subscription's current_period_end (the next renewal), whereas the
        invoice's own period_end is the just-billed period's end, so the
        anchor is intentionally not written from the invoice here.

        Args:
            invoice: Invoice data from Stripe webhook.
            event_name: Originating event name, for logging.
        """
        customer_id = invoice.get("customer")
        if not customer_id:
            logger.error(f"Missing customer ID in {event_name} invoice data")
            return

        invoice_sub_id = BillingService._extract_invoice_subscription_id(invoice)
        if invoice_sub_id is None:
            logger.info(
                f"Ignoring paid one-off invoice for customer {customer_id} "
                f"(no subscription attached)"
            )
            return

        with stripe_sync_lock(customer_id):
            workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()
            if workspace is None:
                logger.warning(f"No workspace found for customer {customer_id}")
                return

            if not workspace.stripe_subscription_id:
                # Can't tell whether this invoice belongs to the workspace's
                # subscription — derive the true state from Stripe instead
                # of activating on a guess (sync also records the sub id).
                logger.info(
                    f"Workspace for customer {customer_id} has no recorded "
                    f"subscription id; re-syncing from Stripe for {event_name}"
                )
                BillingService.sync_workspace_from_stripe(customer_id)
                return

            if invoice_sub_id != workspace.stripe_subscription_id:
                logger.info(
                    f"Ignoring paid invoice for subscription {invoice_sub_id} "
                    f"(workspace subscription is "
                    f"{workspace.stripe_subscription_id}) "
                    f"for customer {customer_id}"
                )
                return

            update_data: dict[str, Any] = {}
            amount_paid = invoice.get("amount_paid") or 0
            if amount_paid > 0:
                # A real payment on the workspace's subscription: activate
                # and record that a working payment method exists. $0
                # invoices (trial start) must not activate — the sync
                # below derives the correct trial state instead.
                update_data["subscription_status"] = "active"
                update_data["payment_method_added"] = True

            # Deliberately do NOT write billing_cycle_anchor here. The
            # invoice's top-level ``period_end`` is the end of the
            # just-billed period (≈ now for a renewal), not the NEXT
            # renewal that _extract_billing_anchor's contract requires;
            # writing it would regress the anchor a subscription handler
            # already advanced to the subscription's current_period_end.
            # The sync below reads the subscription and sets the anchor
            # from current_period_end, keeping it on the next renewal.

            if update_data:
                Workspace.objects.filter(id=workspace.id).update(**update_data)
                logger.info(
                    f"Applied {event_name} for customer {customer_id} "
                    f"(amount_paid={amount_paid})"
                )

            # Authoritative refinement: corrects trial state for $0
            # invoices and undoes activation if the subscription has
            # since been deleted (late webhook retry).
            BillingService.sync_workspace_from_stripe(customer_id)

    @staticmethod
    def handle_payment_success(invoice: dict[str, Any]) -> None:
        """Handle successful payment event (invoice.payment_succeeded).

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        BillingService._apply_paid_subscription_invoice(invoice, "payment_success")

    @staticmethod
    def _extract_invoice_subscription_id(invoice: dict[str, Any]) -> str | None:
        """Extract the subscription id an invoice belongs to, if any.

        Args:
            invoice: Invoice data from Stripe webhook. The ``subscription``
                field may be an id string, an expanded object, or absent
                (one-off invoices).

        Returns:
            Subscription id string, or None for one-off invoices.
        """
        invoice_sub = invoice.get("subscription")
        if isinstance(invoice_sub, dict):
            invoice_sub = invoice_sub.get("id")
        return cast(str | None, invoice_sub) or None

    @staticmethod
    def handle_payment_failed(invoice: dict[str, Any]) -> None:
        """Handle failed payment event.

        Only marks the workspace past_due when the failed invoice belongs
        to the workspace's own subscription. One-off (ad-hoc/portal)
        invoices are ignored — nothing would ever restore "active" when
        such an invoice is later paid, voided, or forgotten.

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        customer_id = invoice.get("customer")
        if not customer_id:
            logger.error("Missing customer ID in invoice data")
            return

        invoice_sub_id = BillingService._extract_invoice_subscription_id(invoice)
        if invoice_sub_id is None:
            logger.info(
                f"Ignoring failed one-off invoice for customer {customer_id} "
                f"(no subscription attached)"
            )
            return

        with stripe_sync_lock(customer_id):
            workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()
            if workspace is None:
                logger.warning(f"No workspace found for customer {customer_id}")
                return

            if not workspace.stripe_subscription_id:
                # We can't tell whether this invoice belongs to the workspace's
                # subscription — don't punish on a guess. Re-sync instead: it
                # records the subscription id and derives the true status
                # (Stripe reports the sub as past_due if this invoice was its).
                logger.warning(
                    f"Workspace for customer {customer_id} has no recorded "
                    f"subscription id; re-syncing from Stripe instead of marking "
                    f"past_due for invoice subscription {invoice_sub_id}"
                )
                BillingService.sync_workspace_from_stripe(customer_id)
                return

            if invoice_sub_id != workspace.stripe_subscription_id:
                logger.info(
                    f"Ignoring failed invoice for subscription {invoice_sub_id} "
                    f"(workspace subscription is {workspace.stripe_subscription_id}) "
                    f"for customer {customer_id}"
                )
                return

            Workspace.objects.filter(id=workspace.id).update(
                subscription_status="past_due"
            )
            logger.warning(
                f"Updated payment status to past_due for customer {customer_id}"
            )

    @staticmethod
    def handle_checkout_completed(session: dict[str, Any]) -> None:
        """Handle checkout.session.completed event.

        This is triggered when a customer completes checkout and the
        subscription is created. Links the subscription to the organization.

        Args:
            session: Checkout session data from Stripe webhook.
        """
        customer_id = session.get("customer")
        if not customer_id:
            logger.error("Missing customer ID in checkout session")
            return

        # Extract metadata with workspace and plan info
        metadata = session.get("metadata", {}) or {}
        workspace_id = metadata.get("workspace_id") or metadata.get("organization_id")
        raw_plan_name = metadata.get("plan_name")

        subscription_id = session.get("subscription")

        # Update workspace with new subscription status
        update_data: dict[str, Any] = {
            "subscription_status": "active",
            "payment_method_added": True,
        }

        # Checkout metadata is client-influenced: normalize through the
        # STRIPE_PLANS choices and refuse anything unrecognized instead
        # of persisting arbitrary display strings via .update() (which
        # bypasses model validation).
        plan_name: str | None = None
        if raw_plan_name:
            plan_name = BillingService._normalize_plan_name(raw_plan_name)
            if plan_name:
                update_data["subscription_plan"] = plan_name
            else:
                logger.warning(
                    f"Ignoring unrecognized plan_name {raw_plan_name!r} in "
                    f"checkout session metadata for customer {customer_id}"
                )

        if subscription_id:
            update_data["stripe_subscription_id"] = subscription_id

        with stripe_sync_lock(customer_id):
            # Find workspace by customer ID or workspace ID from metadata
            if workspace_id:
                # The checkout view persists stripe_customer_id via
                # get_or_create_customer() before the session is even
                # created, so a genuine completion matches the workspace on
                # both id and customer. Filtering on the customer too stops
                # forged/stale metadata from updating an unrelated
                # workspace; a still-empty customer id is claimed
                # atomically (same filtered-UPDATE pattern as
                # _claim_subscription_id) rather than silently matching
                # zero rows, covering flows where the view's write didn't
                # land.
                updated_count = Workspace.objects.filter(
                    Q(stripe_customer_id=customer_id) | Q(stripe_customer_id=""),
                    id=workspace_id,
                ).update(stripe_customer_id=customer_id, **update_data)
            else:
                updated_count = Workspace.objects.filter(
                    stripe_customer_id=customer_id
                ).update(**update_data)

            if updated_count > 0:
                logger.info(
                    f"Checkout completed for customer {customer_id}, "
                    f"subscription: {subscription_id}, plan: {plan_name}"
                )
                # Verify/sync full state from Stripe (catches any drift)
                BillingService.sync_workspace_from_stripe(customer_id)
                # The checkout view's duplicate-subscription guard runs at
                # session *creation*; two sessions for different plans can
                # both complete. Detect it here, at the completion edge.
                BillingService._warn_on_duplicate_live_subscriptions(customer_id)
            else:
                logger.warning(
                    f"No workspace found for checkout session. "
                    f"Customer: {customer_id}, Workspace ID: {workspace_id}"
                )

    @staticmethod
    def _warn_on_duplicate_live_subscriptions(customer_id: str) -> None:
        """Log an error when a customer ends up with 2+ live subscriptions.

        The checkout view's has_live_subscription guard runs when the
        session is created, so two sessions for different plans opened
        before either completes can both be paid. Detection (not
        auto-cancel — refunds are a human decision) points operators at
        the audit_duplicate_subscriptions command. Best-effort: detection
        failures must not fail the webhook.

        Args:
            customer_id: Stripe customer ID to check.
        """
        try:
            stripe_api = StripeAPI()
            live_ids: list[str] = []
            for status in ("active", "trialing", "past_due"):
                subs = stripe_api.get_customer_subscriptions(
                    customer_id, status=status, max_results=2
                )
                live_ids.extend(sub["id"] for sub in subs if sub.get("id"))
                if len(live_ids) >= 2:
                    break

            if len(live_ids) >= 2:
                logger.error(
                    f"Customer {customer_id} has multiple live subscriptions "
                    f"after checkout: {live_ids}. The customer is likely "
                    f"double-billed; run manage.py audit_duplicate_subscriptions "
                    f"and cancel/refund the extra subscription."
                )
        except Exception as e:
            logger.warning(
                f"Could not check for duplicate subscriptions for customer "
                f"{customer_id}: {e!s}"
            )

    @staticmethod
    def handle_trial_ending(subscription: dict[str, Any]) -> None:
        """Handle trial ending notification (3 days before trial ends).

        This event is fired when a subscription's trial is about to end.
        Can be used to send reminder notifications to customers.

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        customer_id = BillingService._get_customer_id(subscription, "trial ending")
        if not customer_id:
            return

        trial_end = subscription.get("trial_end")

        # Find workspace and log the event
        ws = Workspace.objects.filter(stripe_customer_id=customer_id).first()

        if ws:
            logger.info(
                f"Trial ending soon for workspace {ws.name} "
                f"(customer: {customer_id}), trial_end: {trial_end}"
            )
            # TODO: Send notification email to workspace admins
            # TODO: Trigger Slack notification if configured
        else:
            logger.warning(f"Trial ending for unknown customer {customer_id}")

    @staticmethod
    def handle_invoice_paid(invoice: dict[str, Any]) -> None:
        """Handle invoice.paid event.

        This confirms that an invoice was paid successfully.
        Same scoped application as payment_success.

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        BillingService._apply_paid_subscription_invoice(invoice, "invoice_paid")

    @staticmethod
    def handle_payment_action_required(invoice: dict[str, Any]) -> None:
        """Handle invoice.payment_action_required event.

        This is triggered when a payment requires customer action,
        such as 3D Secure authentication.

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        customer_id = invoice.get("customer")
        if not customer_id:
            logger.error("Missing customer ID in action required invoice")
            return

        hosted_invoice_url = invoice.get("hosted_invoice_url")

        # Find workspace and log the event
        ws = Workspace.objects.filter(stripe_customer_id=customer_id).first()

        if ws:
            logger.warning(
                f"Payment action required for workspace {ws.name} "
                f"(customer: {customer_id}). Invoice URL: {hosted_invoice_url}"
            )
            # TODO: Send notification email to workspace admins
            # with link to complete payment
        else:
            logger.warning(
                f"Payment action required for unknown customer: {customer_id}"
            )

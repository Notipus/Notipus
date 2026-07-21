"""Stripe source plugin implementation.

This module implements the BaseSourcePlugin interface for Stripe,
handling webhook validation, parsing, and customer data retrieval
using the official Stripe SDK.
"""

import logging
from decimal import Decimal
from typing import Any, ClassVar, cast

import stripe
from core.encryption import InvalidToken, decrypt, encrypt, looks_like_token
from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.sources.base import (
    BaseSourcePlugin,
    InvalidDataError,
    mask_sensitive_headers,
)
from webhooks.utils.currency import from_minor_units

logger = logging.getLogger(__name__)

# Cache key prefix and TTL for customer email lookup.
#
# The TTL must span the longest gap between an email-carrying event (an
# invoice) and an email-less subscription event that needs the lookup.
# customer.subscription.trial_will_end fires ~3 days before a trial ends -
# up to a month after the $0 trial-creation invoice cached the email. The
# old 1-hour TTL guaranteed those notifications rendered with no customer
# identity at all ("Trial ending soon / Stripe"). 45 days covers 30-day
# trials with headroom; every later invoice refreshes the entry.
CUSTOMER_EMAIL_CACHE_PREFIX = "stripe_customer_email:"
CUSTOMER_EMAIL_CACHE_TTL = 45 * 24 * 60 * 60  # 45 days

# Maximum allowed gap between a subscription's trial_end and the invoice's
# period_start for the invoice to count as the first post-trial invoice.
# Stripe sets period_start equal to trial_end on the first paid invoice;
# a small tolerance absorbs clock drift and invoice-finalization lag.
TRIAL_CONVERSION_TOLERANCE_SECONDS = 3600


class StripeSourcePlugin(BaseSourcePlugin):
    """Handle Stripe webhooks using official Stripe SDK.

    This plugin validates webhook signatures using Stripe's built-in
    verification and parses various subscription and payment events.

    Attributes:
        PROVIDER_NAME: Provider identifier used in event data.
        EVENT_TYPE_MAPPING: Maps Stripe event types to internal types.
    """

    PROVIDER_NAME: ClassVar[str] = "stripe"

    EVENT_TYPE_MAPPING: ClassVar[dict[str, str]] = {
        "customer.subscription.created": "subscription_created",
        "customer.subscription.updated": "subscription_updated",
        "customer.subscription.deleted": "subscription_deleted",
        "customer.subscription.trial_will_end": "trial_ending",
        "invoice.payment_succeeded": "payment_success",
        "invoice.payment_failed": "payment_failure",
        "invoice.paid": "invoice_paid",
        "invoice.payment_action_required": "payment_action_required",
        "checkout.session.completed": "checkout_completed",
        "test": "test",
    }

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Stripe source plugin.
        """
        return PluginMetadata(
            name="stripe",
            display_name="Stripe",
            version="1.0.0",
            description="Stripe webhook handler for payments and subscriptions",
            plugin_type=PluginType.SOURCE,
            capabilities={
                PluginCapability.WEBHOOK_VALIDATION,
                PluginCapability.CUSTOMER_DATA,
            },
            priority=100,
        )

    def __init__(
        self, webhook_secret: str = "", process_billing_events: bool = False
    ) -> None:
        """Initialize Stripe plugin with webhook secret.

        Args:
            webhook_secret: Stripe webhook signing secret.
            process_billing_events: When True, dispatch parsed events to
                BillingService, mutating Notipus workspace subscription
                state. Only the global billing endpoint
                (/webhook/billing/stripe/, Notipus's own Stripe account)
                may set this. Tenant notification endpoints
                (/webhook/customer/<uuid>/stripe/) validate against a
                tenant-supplied secret, so events arriving there are
                tenant-controlled and must never reach BillingService.
        """
        super().__init__(webhook_secret)
        self.process_billing_events = process_billing_events
        # Store webhook data for customer lookup (we can't call Stripe API
        # because we don't have the customer's API key - only the webhook)
        self._current_webhook_data: dict[str, Any] | None = None
        # Configure Stripe API version for webhook signature verification
        stripe.api_version = settings.STRIPE_API_VERSION

    def validate_webhook(self, request: HttpRequest) -> bool:
        """Validate webhook signature using Stripe SDK.

        Args:
            request: The incoming HTTP request.

        Returns:
            True if signature is valid, False otherwise.
        """
        # Pre-validation: log only minimal, non-attacker-controlled fields
        # so unauthenticated callers cannot flood logs with payload content.
        logger.debug(
            "Validate Stripe webhook data",
            extra={
                "content_type": request.content_type,
                "content_length": request.headers.get("Content-Length"),
            },
        )

        # Never bypass validation: an empty webhook secret means we cannot
        # verify the signature, so reject (even with DEBUG=True). Without
        # this guard, Stripe's construct_event with an empty secret lets an
        # attacker forge a valid signature over arbitrary payloads.
        if not self.webhook_secret:
            logger.error(
                "SECURITY: Webhook secret not configured! "
                "Rejecting webhook to prevent unauthorized access."
            )
            return False

        signature = request.headers.get("Stripe-Signature")
        payload = request.body

        if not signature:
            return False

        try:
            # Use Stripe's built-in webhook validation
            stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
            return True
        except stripe.SignatureVerificationError as e:
            logger.error(f"Stripe webhook signature verification failed: {e!s}")
            return False
        except Exception as e:
            logger.error(f"Stripe webhook validation error: {e!s}")
            return False

    def _extract_stripe_event_info(
        self, event: Any
    ) -> tuple[str, Any] | tuple[None, None]:
        """Extract event type and data from Stripe event object.

        Args:
            event: Stripe event object.

        Returns:
            Tuple of (event_type, event_data), or (None, None) for unsupported
            event types that should be acknowledged but not processed.

        Raises:
            InvalidDataError: If event type is missing or data is missing.
        """
        body_event_type = event.type
        if not body_event_type:
            raise InvalidDataError("Missing event type")

        event_type = self.EVENT_TYPE_MAPPING.get(body_event_type)
        if not event_type:
            # Unsupported event types are acknowledged but not processed.
            # Stripe sends many event types; we only handle a subset.
            logger.info(f"Ignoring unsupported Stripe event type: {body_event_type}")
            return None, None

        data = event.data.object
        if not data:
            raise InvalidDataError("Missing data parameter")

        # Capture previous_attributes for detecting changes (upgrades/downgrades)
        # Stripe provides this for update events to show what changed
        previous_attributes = getattr(event.data, "previous_attributes", None)
        if previous_attributes:
            # Convert to dict if it's a Stripe object
            if hasattr(previous_attributes, "to_dict"):
                data["_previous_attributes"] = previous_attributes.to_dict()
            else:
                data["_previous_attributes"] = dict(previous_attributes)

        return event_type, data

    def _extract_idempotency_key(self, event: Any) -> str | None:
        """Extract idempotency key from Stripe event.

        The idempotency key is shared across all events triggered by the same
        Stripe API request. This allows deduplication across event types
        (e.g., subscription.created and invoice.paid from same action).

        Args:
            event: Stripe event object.

        Returns:
            Idempotency key string, or None if not available.
        """
        try:
            request_info = getattr(event, "request", None)
            if request_info:
                # Handle both object attribute and dict access
                if hasattr(request_info, "idempotency_key"):
                    key: str | None = request_info.idempotency_key
                    return key
                elif isinstance(request_info, dict):
                    return cast("str | None", request_info.get("idempotency_key"))
            return None
        except Exception:
            # Don't fail webhook processing if we can't get idempotency key
            return None

    def _extract_item_amount(self, item: dict[str, Any]) -> int | None:
        """Extract the per-unit amount in cents from a subscription item.

        Supports both the old API shape (``item.plan.amount``) and the
        newer prices API shape (``item.price.unit_amount``).

        Args:
            item: A single subscription item dict from items.data[].

        Returns:
            Per-unit amount in cents, or None if not determinable.
        """
        plan = item.get("plan")
        if isinstance(plan, dict) and plan.get("amount") is not None:
            return int(plan["amount"])

        price = item.get("price")
        if isinstance(price, dict) and price.get("unit_amount") is not None:
            return int(price["unit_amount"])

        return None

    def _sum_item_amounts(self, items_data: Any) -> int | None:
        """Sum ``amount * quantity`` across subscription items.

        Args:
            items_data: The items.data[] list from a subscription payload
                (or from _previous_attributes.items).

        Returns:
            Total amount in cents, or None if no item amount is determinable.
        """
        if not isinstance(items_data, list):
            return None

        total = 0
        found = False
        for item in items_data:
            if not isinstance(item, dict):
                continue
            unit_amount = self._extract_item_amount(item)
            if unit_amount is None:
                continue
            quantity = item.get("quantity")
            if not isinstance(quantity, int) or quantity < 1:
                quantity = 1
            total += unit_amount * quantity
            found = True

        return total if found else None

    def _extract_subscription_amount(self, sub_data: dict[str, Any]) -> int:
        """Extract the total recurring amount in cents for a subscription.

        Modern multi-item subscriptions have a null top-level ``plan``, so
        the item amounts (``items[].plan.amount * quantity`` or
        ``items[].price.unit_amount * quantity``) are summed first, with
        the top-level plan as a legacy single-item fallback.

        Args:
            sub_data: Subscription payload dictionary.

        Returns:
            Total amount in cents, or 0 if not determinable.
        """
        items = sub_data.get("items")
        if isinstance(items, dict):
            items_total = self._sum_item_amounts(items.get("data"))
            if items_total is not None:
                return items_total

        plan = sub_data.get("plan")
        if isinstance(plan, dict) and plan.get("amount") is not None:
            return int(plan["amount"])

        return 0

    def _get_previous_plan_amount(self, data: dict[str, Any]) -> int | None:
        """Extract previous plan amount from subscription update data.

        Checks both direct plan changes and multi-item subscription changes,
        summing all previous item amounts (with quantities) for the latter.

        Args:
            data: Event data dictionary with _previous_attributes.

        Returns:
            Previous amount in cents, or None if not available.
        """
        prev_attrs = data.get("_previous_attributes", {})
        if not prev_attrs:
            return None

        # Check direct plan change
        prev_plan = prev_attrs.get("plan", {})
        if prev_plan and prev_plan.get("amount") is not None:
            return cast("int | None", prev_plan.get("amount"))

        # Check items for multi-item subscriptions
        prev_items = prev_attrs.get("items", {})
        if isinstance(prev_items, dict):
            return self._sum_item_amounts(prev_items.get("data"))

        return None

    def _detect_change_direction(
        self, current_amount: int, prev_amount: int | None
    ) -> str | None:
        """Determine if subscription change is upgrade, downgrade, or other.

        Args:
            current_amount: Current plan amount in cents.
            prev_amount: Previous plan amount in cents, or None.

        Returns:
            "upgrade", "downgrade", "other", or None if undetermined.
        """
        if prev_amount is None:
            return None

        if current_amount > prev_amount:
            return "upgrade"
        elif current_amount < prev_amount:
            return "downgrade"
        return "other"

    def _handle_stripe_billing(self, event_type: str, data: dict[str, Any]) -> Decimal:
        """Handle billing service calls and return amount in major units.

        Stripe sends amounts in each currency's minor unit, so the
        payload's currency drives the conversion: 100 minor units per
        dollar/euro, but 1 per yen (zero-decimal currencies) and 1000
        per dinar (three-decimal currencies).

        Args:
            event_type: The normalized event type.
            data: Event data dictionary.

        Returns:
            Amount in major units as a Decimal.
        """
        amount_minor = self._get_amount_and_dispatch_billing(event_type, data)
        amount: Decimal = from_minor_units(amount_minor, self._event_currency(data))
        return amount

    def _event_currency(self, data: dict[str, Any]) -> str:
        """Extract the currency code from a Stripe payload.

        Invoices and checkout sessions carry a top-level ``currency``,
        but some subscription payloads only carry it on the nested plan
        or item prices, so those are checked before defaulting to USD.

        Args:
            data: Raw event data (invoice, subscription, or session).

        Returns:
            Upper-cased ISO 4217 currency code, defaulting to USD.
        """
        currency = data.get("currency") or self._nested_plan_currency(data)
        return str(currency or "USD").upper()

    def _nested_plan_currency(self, data: dict[str, Any]) -> str | None:
        """Find a currency on a subscription's plan or item prices.

        Checks the top-level plan (legacy single-item subscriptions),
        then each item's plan and price objects.

        Args:
            data: Raw subscription event data.

        Returns:
            Currency code string, or None if not present anywhere.
        """
        plan = data.get("plan")
        if isinstance(plan, dict) and plan.get("currency"):
            return str(plan["currency"])

        items = data.get("items")
        if not isinstance(items, dict):
            return None
        items_data = items.get("data")
        if not isinstance(items_data, list):
            return None

        for item in items_data:
            if not isinstance(item, dict):
                continue
            item_plan = item.get("plan")
            if isinstance(item_plan, dict) and item_plan.get("currency"):
                return str(item_plan["currency"])
            price = item.get("price")
            if isinstance(price, dict) and price.get("currency"):
                return str(price["currency"])

        return None

    def _dispatch_billing_handler(
        self, handler_name: str, data: dict[str, Any]
    ) -> None:
        """Invoke a BillingService handler iff billing processing is enabled.

        Tenant notification endpoints construct this plugin with
        process_billing_events=False (the default), so tenant-signed
        events can never mutate Notipus workspace billing state.

        Args:
            handler_name: Name of the BillingService static method.
            data: Event data dictionary to pass to the handler.
        """
        if not self.process_billing_events:
            return

        from webhooks.services.billing import BillingService

        getattr(BillingService, handler_name)(data)

    def _get_amount_and_dispatch_billing(
        self, event_type: str, data: dict[str, Any]
    ) -> int:
        """Get amount in cents and dispatch to appropriate billing handler.

        Billing handlers only run when this plugin instance was created
        with process_billing_events=True (the global billing endpoint);
        amount extraction and notification metadata always run.

        Args:
            event_type: The normalized event type.
            data: Event data dictionary (may be mutated with metadata).

        Returns:
            Amount in cents.
        """
        if event_type == "subscription_created":
            return self._handle_subscription_created(data)

        if event_type == "subscription_updated":
            return self._handle_subscription_updated(data)

        if event_type == "subscription_deleted":
            self._dispatch_billing_handler("handle_subscription_deleted", data)
            return 0

        if event_type == "payment_success":
            return self._handle_payment_success(data)

        if event_type == "payment_failure":
            self._dispatch_billing_handler("handle_payment_failed", data)
            return int(data.get("amount_due", 0))

        if event_type == "checkout_completed":
            self._dispatch_billing_handler("handle_checkout_completed", data)
            return int(data.get("amount_total", 0))

        if event_type == "trial_ending":
            self._dispatch_billing_handler("handle_trial_ending", data)
            return 0

        if event_type == "invoice_paid":
            self._dispatch_billing_handler("handle_invoice_paid", data)
            return int(data.get("amount_paid", 0))

        if event_type == "payment_action_required":
            self._dispatch_billing_handler("handle_payment_action_required", data)
            return int(data.get("amount_due", 0))

        return 0

    def _handle_subscription_created(self, data: dict[str, Any]) -> int:
        """Handle subscription_created event with trial detection.

        Args:
            data: Event data dictionary (mutated with trial flags if trialing).

        Returns:
            Amount in cents (0 for trials, plan amount otherwise).
        """
        self._dispatch_billing_handler("handle_subscription_created", data)

        # Check if this is a trial subscription
        if data.get("status") == "trialing":
            return self._flag_as_trial(data)

        return self._extract_subscription_amount(data)

    def _flag_as_trial(self, data: dict[str, Any]) -> int:
        """Flag subscription data as trial and extract trial metadata.

        Args:
            data: Event data dictionary (mutated with trial flags).

        Returns:
            0 (no payment for trials).
        """
        data["_is_trial"] = True
        data["_trial_end"] = data.get("trial_end")
        # Sum item amounts: top-level plan is null on multi-item subscriptions
        data["_plan_amount_cents"] = self._extract_subscription_amount(data)

        # Calculate trial days from trial_start and trial_end (Unix timestamps)
        trial_start = data.get("trial_start")
        trial_end = data.get("trial_end")
        if trial_start and trial_end:
            data["_trial_days"] = (trial_end - trial_start) // 86400

        return 0  # No payment for trials

    def _handle_subscription_updated(self, data: dict[str, Any]) -> int:
        """Handle subscription_updated event with change detection.

        Args:
            data: Event data dictionary (mutated with _change_direction).

        Returns:
            Current plan amount in cents.
        """
        current_amount: int = self._extract_subscription_amount(data)
        prev_amount = self._get_previous_plan_amount(data)
        change_direction = self._detect_change_direction(current_amount, prev_amount)

        if change_direction:
            data["_change_direction"] = change_direction

        self._dispatch_billing_handler("handle_subscription_updated", data)
        return current_amount

    def _handle_payment_success(self, data: dict[str, Any]) -> int:
        """Handle payment_success event with trial conversion detection.

        Args:
            data: Event data dictionary (mutated with _is_trial_conversion).

        Returns:
            Amount paid in cents.
        """
        # Use amount_paid, not amount_due (amount_due is 0 after payment succeeds)
        amount_cents: int = int(data.get("amount_paid", 0))

        # Detect trial conversion: first real payment after trial.
        # billing_reason "subscription_cycle" alone is NOT sufficient - it
        # fires on every recurring cycle, not just the first one after a
        # trial. Require the invoice period to start at the trial's end.
        billing_reason = data.get("billing_reason", "")
        if (
            billing_reason == "subscription_cycle"
            and amount_cents > 0
            and self._is_first_invoice_after_trial(data)
        ):
            data["_is_trial_conversion"] = True

        self._dispatch_billing_handler("handle_payment_success", data)
        return amount_cents

    def _extract_invoice_trial_end(self, data: dict[str, Any]) -> int | None:
        """Extract the subscription's trial_end timestamp from an invoice.

        The invoice's ``subscription`` field (old API) or
        ``parent.subscription_details.subscription`` (new API) may be an
        expanded subscription object carrying ``trial_end``. When it is only
        an id string, the trial end is not derivable from the payload.

        Args:
            data: Raw invoice event data.

        Returns:
            Unix timestamp of the trial end, or None if not available.
        """
        subscription = data.get("subscription")
        if isinstance(subscription, dict):
            trial_end = subscription.get("trial_end")
            if isinstance(trial_end, int):
                return trial_end

        parent = data.get("parent")
        if isinstance(parent, dict):
            sub_details = parent.get("subscription_details")
            if isinstance(sub_details, dict):
                nested_sub = sub_details.get("subscription")
                if isinstance(nested_sub, dict):
                    trial_end = nested_sub.get("trial_end")
                    if isinstance(trial_end, int):
                        return trial_end

        return None

    def _is_first_invoice_after_trial(self, data: dict[str, Any]) -> bool:
        """Check whether an invoice is the first paid one after a trial.

        Stateless check derived from the invoice payload alone: on the
        first post-trial invoice, Stripe sets the invoice's period_start
        equal to the subscription's trial_end. A small tolerance absorbs
        clock drift and invoice-finalization lag.

        Args:
            data: Raw invoice event data.

        Returns:
            True if the invoice period starts at (or immediately after)
            the subscription's trial end.
        """
        trial_end = self._extract_invoice_trial_end(data)
        if trial_end is None:
            return False

        period_start = data.get("period_start")
        if not isinstance(period_start, int):
            return False

        return 0 <= period_start - trial_end <= TRIAL_CONVERSION_TOLERANCE_SECONDS

    def _build_stripe_event_data(
        self,
        event_type: str,
        customer_id: str,
        data: dict[str, Any],
        amount: Decimal,
        idempotency_key: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Build Stripe event data structure.

        Args:
            event_type: The normalized event type.
            customer_id: Customer identifier.
            data: Raw event data.
            amount: Payment amount in major currency units.
            idempotency_key: Stripe request idempotency key for deduplication.
            event_id: Stripe event id (``evt_...``) from the outer event
                envelope, used for exact deduplication. Distinct from
                ``external_id``, which is the underlying object id
                (e.g. ``sub_...`` or ``in_...``).

        Returns:
            Standardized event data dictionary.
        """
        event_data: dict[str, Any] = {
            "type": event_type,
            "customer_id": customer_id,
            "provider": self.PROVIDER_NAME,
            "external_id": data.get("id", ""),
            "event_id": event_id,
            "status": data.get("status"),
            "created_at": data.get("created"),
            "currency": self._event_currency(data),
            # float() at the boundary: event dicts are JSON-serialized
            # (Redis pending-event queue), which rejects Decimal.
            "amount": float(amount),
            "metadata": {},
            "idempotency_key": idempotency_key,
        }

        # Add metadata based on event flags and type
        self._add_event_metadata(event_data, event_type, data)

        return event_data

    def _add_event_metadata(
        self, event_data: dict[str, Any], event_type: str, data: dict[str, Any]
    ) -> None:
        """Add metadata to event data based on flags and event type.

        Args:
            event_data: Event data dictionary to mutate.
            event_type: The normalized event type.
            data: Raw event data with internal flags.
        """
        metadata = event_data["metadata"]

        # Add trial conversion flag if detected
        if data.get("_is_trial_conversion"):
            metadata["is_trial_conversion"] = True

        # Add trial metadata for trial_started events
        if data.get("_is_trial"):
            self._add_trial_metadata(metadata, data)

        # Add change direction for subscription updates (upgrade/downgrade)
        if data.get("_change_direction"):
            metadata["change_direction"] = data["_change_direction"]

        # Add subscription metadata for subscription events
        subscription_events = {
            "subscription_created",
            "subscription_updated",
            "subscription_deleted",
            "trial_started",
        }
        if event_type in subscription_events:
            self._add_subscription_metadata(metadata, event_type, data)

        # Add invoice metadata for payment events
        if event_type in ("payment_success", "payment_failure"):
            self._add_invoice_metadata(metadata, data)

    def _add_trial_metadata(
        self, metadata: dict[str, Any], data: dict[str, Any]
    ) -> None:
        """Add trial-related metadata.

        Args:
            metadata: Metadata dictionary to mutate.
            data: Raw event data with trial flags.
        """
        metadata["is_trial"] = True
        if data.get("_trial_end"):
            metadata["trial_end"] = data["_trial_end"]
        if data.get("_trial_days"):
            metadata["trial_days"] = data["_trial_days"]
        # "is not None" so a $0 trial plan amount (e.g. a free tier) is
        # still surfaced in metadata rather than silently dropped.
        plan_amount_cents = data.get("_plan_amount_cents")
        if plan_amount_cents is not None:
            # float() keeps event metadata JSON-serializable (Redis queue)
            metadata["plan_amount"] = float(
                from_minor_units(plan_amount_cents, self._event_currency(data))
            )

    def _add_subscription_metadata(
        self, metadata: dict[str, Any], event_type: str, data: dict[str, Any]
    ) -> None:
        """Add subscription-related metadata.

        Args:
            metadata: Metadata dictionary to mutate.
            event_type: The normalized event type.
            data: Raw event data.
        """
        metadata["subscription_id"] = data.get("id", "")

        # Map Stripe interval to billing period.
        # Top-level plan is null on multi-item subscriptions, so guard it.
        plan = data.get("plan") or {}
        interval = plan.get("interval")
        if interval:
            interval_map = {
                "month": "monthly",
                "year": "annual",
                "week": "weekly",
                "day": "daily",
            }
            metadata["billing_period"] = interval_map.get(interval, interval)

        # Add plan name if available
        plan_name = plan.get("nickname") or plan.get("name")
        if plan_name:
            metadata["plan_name"] = plan_name

        # For subscription updates, extract previous amount for upgrade headlines
        if event_type == "subscription_updated":
            prev_attrs = data.get("_previous_attributes", {})
            prev_plan = prev_attrs.get("plan", {})
            if prev_plan and prev_plan.get("amount") is not None:
                # Prefer the previous plan's own currency if it changed
                currency = str(
                    prev_plan.get("currency") or self._event_currency(data)
                ).upper()
                # float() keeps event metadata JSON-serializable (Redis queue)
                metadata["previous_amount"] = float(
                    from_minor_units(prev_plan["amount"], currency)
                )
                # Store the currency (and billing period, when derivable)
                # the previous amount is denominated in, so formatters can
                # render the "old" side of upgrade/downgrade headlines
                # correctly when the currency or interval changed.
                metadata["previous_currency"] = currency
                prev_interval = prev_plan.get("interval")
                if prev_interval:
                    metadata["previous_billing_period"] = self.INTERVAL_MAP.get(
                        prev_interval, prev_interval
                    )

    def _get_name_from_structured_fields(self, item: dict[str, Any]) -> str | None:
        """Try to get plan name from structured Stripe line item fields.

        Checks plan/price objects (old API, pre-basil) and the pricing
        field (new API, 2025-03-31+) for human-readable plan names.

        Args:
            item: A single line item dict from lines.data[].

        Returns:
            Plan name string, or None if no structured name found.
        """
        # Old API: plan object (pre-basil)
        plan = item.get("plan")
        if isinstance(plan, dict):
            name = plan.get("nickname") or plan.get("name")
            if name:
                return str(name)

        # Old API: price object (pre-basil)
        price = item.get("price")
        if isinstance(price, dict):
            name = price.get("nickname")
            if name:
                return str(name)
            product = price.get("product")
            if isinstance(product, dict) and product.get("name"):
                return str(product["name"])

        # New API (2025-03-31+): pricing.price_details (if expanded)
        pricing = item.get("pricing")
        if isinstance(pricing, dict):
            return self._get_name_from_pricing(pricing)

        return None

    def _get_name_from_pricing(self, pricing: dict[str, Any]) -> str | None:
        """Extract plan name from the new API pricing field.

        Args:
            pricing: The pricing dict from a line item.

        Returns:
            Plan name, or None if not found or not expanded.
        """
        price_details = pricing.get("price_details")
        if not isinstance(price_details, dict):
            return None

        price_obj = price_details.get("price")
        if isinstance(price_obj, dict) and price_obj.get("nickname"):
            return str(price_obj["nickname"])

        product_obj = price_details.get("product")
        if isinstance(product_obj, dict) and product_obj.get("name"):
            return str(product_obj["name"])

        return None

    def _extract_plan_name_from_line_item(self, item: dict[str, Any]) -> str | None:
        """Extract plan name from an invoice line item.

        Tries structured fields first (plan/price objects from older API
        versions, or expanded pricing objects), then falls back to parsing
        the description string.

        Args:
            item: A single line item dict from lines.data[].

        Returns:
            Plan name string, or None if not found.
        """
        return self._get_name_from_structured_fields(
            item
        ) or self._parse_plan_name_from_description(item.get("description", ""))

    def _parse_plan_name_from_description(self, description: str) -> str | None:
        """Parse plan name from Stripe's generated line item description.

        Stripe generates descriptions in predictable formats:
        - "2 screen × Business Plan Monthly (at $26.60 / month)"
        - "Trial period for Business Plan Monthly (per screen)"
        - "Business Plan Monthly (per screen)"
        - "Business Plan Monthly"

        Args:
            description: Line item description string.

        Returns:
            Extracted plan name, or None if parsing fails.
        """
        if not description:
            return None

        text = description.strip()

        # Strip "Trial period for " prefix
        trial_prefix = "Trial period for "
        if text.startswith(trial_prefix):
            text = text[len(trial_prefix) :]

        # Strip "<quantity> <unit> × " prefix (e.g. "2 screen × ")
        if "×" in text:
            _, _, text = text.partition("×")
            text = text.strip()

        # Strip trailing parenthetical (e.g. "(at $26.60 / month)")
        if "(" in text:
            text, _, _ = text.partition("(")
            text = text.strip()

        return text if len(text) > 2 else None

    # Maps Stripe interval names to display billing periods
    INTERVAL_MAP: ClassVar[dict[str, str]] = {
        "month": "monthly",
        "year": "annual",
        "week": "weekly",
        "day": "daily",
    }

    def _billing_period_from_days(self, days: int) -> str | None:
        """Map a number of days to a billing period.

        Args:
            days: Number of days in the billing period.

        Returns:
            Billing period string, or None if unrecognized.
        """
        if 25 <= days <= 35:
            return "monthly"
        if 85 <= days <= 95:
            return "quarterly"
        if 360 <= days <= 370:
            return "annual"
        if 5 <= days <= 9:
            return "weekly"
        return None

    def _billing_period_from_structured_fields(
        self, item: dict[str, Any]
    ) -> str | None:
        """Extract billing period from structured line item fields.

        Checks the old API shape (``plan.interval``) and the prices API
        shape (``price.recurring.interval``).

        Args:
            item: A single line item dict from lines.data[].

        Returns:
            Billing period string, or None if not found.
        """
        plan = item.get("plan")
        if isinstance(plan, dict):
            interval = plan.get("interval")
            if interval and interval in self.INTERVAL_MAP:
                return self.INTERVAL_MAP[interval]

        price = item.get("price")
        if isinstance(price, dict):
            recurring = price.get("recurring")
            if isinstance(recurring, dict):
                interval = recurring.get("interval")
                if interval and interval in self.INTERVAL_MAP:
                    return self.INTERVAL_MAP[interval]

        return None

    def _extract_billing_period_from_line_item(
        self, item: dict[str, Any]
    ) -> str | None:
        """Extract billing period from an invoice line item.

        Tries structured fields first (plan.interval), then calculates
        from the line item period timestamps, then falls back to
        parsing the description.

        Args:
            item: A single line item dict from lines.data[].

        Returns:
            Billing period string (monthly, annual, weekly, daily), or None.
        """
        # 1. Structured fields: plan.interval or price.recurring.interval
        structured = self._billing_period_from_structured_fields(item)
        if structured:
            return structured

        # 2. Calculate from period start/end timestamps
        period = item.get("period")
        if isinstance(period, dict):
            start = period.get("start")
            end = period.get("end")
            if isinstance(start, int) and isinstance(end, int) and end > start:
                result = self._billing_period_from_days((end - start) // 86400)
                if result:
                    return result

        # 3. Fallback: look for "/ month" or "/ year" in description
        description = item.get("description", "")
        if description and "/" in description:
            _, _, after_slash = description.rpartition("/")
            word = after_slash.strip().rstrip(")").split()[0].lower()
            if word in self.INTERVAL_MAP:
                return self.INTERVAL_MAP[word]

        return None

    def _extract_subscription_id(self, data: dict[str, Any]) -> str | None:
        """Extract subscription ID from invoice data.

        Checks both the old Stripe API path (data.subscription) and the new
        API path (data.parent.subscription_details.subscription).

        Args:
            data: Raw event data.

        Returns:
            Subscription ID string, or None if not found.
        """
        subscription_id = data.get("subscription")
        if not subscription_id:
            parent = data.get("parent", {})
            if isinstance(parent, dict):
                sub_details = parent.get("subscription_details", {})
                if isinstance(sub_details, dict):
                    subscription_id = sub_details.get("subscription")
        # The subscription may be an expanded object rather than an id string
        if isinstance(subscription_id, dict):
            subscription_id = subscription_id.get("id")
        return subscription_id or None

    def _add_line_item_metadata(
        self, metadata: dict[str, Any], data: dict[str, Any]
    ) -> None:
        """Extract plan name, billing period, and quantity from line items.

        Args:
            metadata: Metadata dictionary to mutate.
            data: Raw event data containing lines.data array.
        """
        lines = data.get("lines", {})
        line_items = lines.get("data", []) if isinstance(lines, dict) else []
        if not line_items:
            return

        first_item = line_items[0]

        if not metadata.get("plan_name"):
            plan_name = self._extract_plan_name_from_line_item(first_item)
            if plan_name:
                metadata["plan_name"] = plan_name

        # Compute billing_period from the line item unless already known
        if not metadata.get("billing_period"):
            parsed_period = self._extract_billing_period_from_line_item(first_item)
            if parsed_period:
                metadata["billing_period"] = parsed_period

        quantity = first_item.get("quantity")
        if quantity is not None and quantity > 1:
            metadata["quantity"] = quantity

    def _add_invoice_metadata(
        self, metadata: dict[str, Any], data: dict[str, Any]
    ) -> None:
        """Add invoice-related metadata for payment events.

        Extracts subscription ID, plan name, attempt count, next retry date,
        billing reason, invoice number, and billing period from the raw
        Stripe invoice payload.

        Args:
            metadata: Metadata dictionary to mutate.
            data: Raw event data.
        """
        subscription_id = self._extract_subscription_id(data)
        if subscription_id:
            metadata["subscription_id"] = subscription_id

        billing_reason = data.get("billing_reason")
        if billing_reason:
            metadata["billing_reason"] = billing_reason

        attempt_count = data.get("attempt_count")
        if attempt_count is not None:
            metadata["attempt_count"] = attempt_count

        next_payment_attempt = data.get("next_payment_attempt")
        if next_payment_attempt is not None:
            metadata["next_payment_attempt"] = next_payment_attempt

        invoice_number = data.get("number")
        if invoice_number:
            metadata["invoice_number"] = invoice_number

        self._add_line_item_metadata(metadata, data)

        # Last-resort fallback only: assume monthly for subscription invoices
        # whose interval could not be determined from the line items. This
        # must run AFTER line-item inspection so annual invoices are not
        # mislabeled as monthly.
        if (
            billing_reason
            and billing_reason.startswith("subscription_")
            and not metadata.get("billing_period")
        ):
            metadata["billing_period"] = "monthly"

    def _construct_verified_event(self, request: HttpRequest) -> Any:
        """Verify the signature and construct the Stripe event object.

        Fails closed on an empty secret: construct_event with an empty
        secret would let an attacker forge a valid signature over arbitrary
        payloads, so reject before any signature work (even with DEBUG).

        Args:
            request: The incoming HTTP request.

        Returns:
            The verified Stripe event object.

        Raises:
            InvalidDataError: If the secret is unconfigured, the signature
                header is missing, or signature verification fails.
        """
        if not self.webhook_secret:
            logger.error(
                "SECURITY: Webhook secret not configured! "
                "Rejecting webhook to prevent unauthorized access."
            )
            raise InvalidDataError("Webhook secret not configured")

        signature = request.headers.get("Stripe-Signature")
        payload = request.body

        if not signature:
            raise InvalidDataError("Missing Stripe signature")

        try:
            # Use Stripe SDK to construct and validate the event
            return stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret
            )
        except stripe.SignatureVerificationError as e:
            raise InvalidDataError(f"Invalid webhook signature: {e!s}") from e
        except Exception as e:
            raise InvalidDataError(f"Webhook parsing error: {e!s}") from e

    def parse_webhook(
        self, request: HttpRequest, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Parse webhook data using Stripe SDK.

        Args:
            request: The incoming HTTP request.
            **kwargs: Additional arguments (unused).

        Returns:
            Parsed event data dictionary.

        Raises:
            InvalidDataError: If webhook data is invalid or missing fields.
        """
        logger.info(
            "Parsing Stripe webhook data",
            extra={
                "content_type": request.content_type,
                "form_data": (request.POST.dict() if request.POST else None),
                "headers": mask_sensitive_headers(request.headers),
            },
        )

        # Signature verification (and the fail-closed empty-secret guard)
        # is owned by _construct_verified_event.
        event = self._construct_verified_event(request)

        # Extract idempotency_key from the event for cross-event deduplication
        # All events triggered by the same Stripe API request share this key
        idempotency_key = self._extract_idempotency_key(event)

        # Extract the Stripe event id (evt_...) from the outer envelope for
        # exact deduplication. The object id alone would make distinct events
        # for the same object (e.g. created + updated) collide.
        event_id = cast("str | None", getattr(event, "id", None))

        # Extract event info using Stripe event object
        event_type, data = self._extract_stripe_event_info(event)

        # Return None for unsupported event types (acknowledged but not processed)
        if event_type is None or data is None:
            return None

        try:
            # Convert Stripe object to dict for easier processing
            if hasattr(data, "to_dict"):
                data_dict: dict[str, Any] = data.to_dict()
            else:
                data_dict = dict(data)

            # Get customer ID - some events may not require one
            # (checkout sessions use metadata for organization lookup)
            customer_id = str(data_dict.get("customer", "") or "")

            # For checkout_completed and trial_ending, customer ID is optional
            # These events use metadata for organization lookup
            events_without_required_customer = {
                "checkout_completed",
                "trial_ending",
                "payment_action_required",
            }
            if not customer_id and event_type not in events_without_required_customer:
                raise InvalidDataError("Missing customer ID")

            # Handle billing and get amount
            amount = self._handle_stripe_billing(event_type, data_dict)

            # Transform subscription_created to trial_started if it's a trial
            if data_dict.get("_is_trial"):
                event_type = "trial_started"

            # Store webhook data for customer lookup
            self._current_webhook_data = data_dict

            # Cache customer email from invoice events for subscription event lookup
            # Invoice events have customer_email, subscription events don't
            customer_email = data_dict.get("customer_email")
            if customer_id and customer_email:
                self._cache_customer_email(customer_id, customer_email)

            # Build and return event data
            return self._build_stripe_event_data(
                event_type, customer_id, data_dict, amount, idempotency_key, event_id
            )

        except (KeyError, ValueError, AttributeError) as e:
            raise InvalidDataError("Missing required fields") from e

    def get_customer_data(self, customer_id: str) -> dict[str, Any]:
        """Get customer data from stored webhook payload.

        We cannot call Stripe API because we don't have the customer's
        API key - we only receive webhooks. Customer data must be extracted
        from the webhook payload itself.

        For subscription events that lack customer_email, we look up the
        email from cache (populated by invoice events for the same customer).

        Note: The pending event queue aggregates related events (subscription
        + invoice) and will have the email from invoice events by the time
        this is called for the final notification.

        Args:
            customer_id: The Stripe customer identifier, used for cache lookup.

        Returns:
            Dictionary with customer data including:
            - company_name: Empty (not in webhook payload)
            - email: Customer email from webhook or cache
            - first_name: First part of customer name
            - last_name: Last part of customer name
            - customer_id: The Stripe customer ID for fallback display
        """
        if not self._current_webhook_data:
            logger.warning("No webhook data available for customer lookup")
            return self._empty_customer_data()

        data = self._current_webhook_data

        # Extract email - available on invoices but NOT on subscription events
        email = data.get("customer_email") or ""

        # If no email in webhook data, try to get it from cache
        # (cached from invoice events for the same customer)
        if not email and customer_id:
            email = self._get_cached_customer_email(customer_id)

        # Extract name - often null in Stripe but check anyway
        name = data.get("customer_name") or ""
        first_name, last_name = self._split_name(name)

        # Company name is not typically in webhook payload
        # (would need API call to customer.metadata which we can't do)
        company_name = ""

        return {
            "company_name": company_name,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "customer_id": customer_id,
        }

    def _empty_customer_data(self) -> dict[str, Any]:
        """Return empty customer data structure.

        Returns:
            Dictionary with empty customer fields.
        """
        return {
            "company_name": "",
            "email": "",
            "first_name": "",
            "last_name": "",
            "customer_id": "",
        }

    def _split_name(self, full_name: str) -> tuple[str, str]:
        """Split a full name into first and last name.

        Args:
            full_name: The full name string.

        Returns:
            Tuple of (first_name, last_name).
        """
        if not full_name:
            return "", ""

        parts = full_name.strip().split(None, 1)
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[1]

    def _cache_customer_email(self, customer_id: str, email: str) -> None:
        """Cache customer email for lookup by subscription events.

        Invoice events include customer_email, but subscription events don't.
        We cache the email from invoice events so subscription events can
        look it up by customer ID.

        The value is encrypted at rest: customer emails are PII and the
        long TTL keeps them in Redis for weeks (Slack compliance requires
        customer PII encrypted at rest in both Postgres and Redis).

        Args:
            customer_id: Stripe customer ID (e.g., cus_xxx).
            email: Customer email address.
        """
        if not customer_id or not email:
            return
        try:
            cache_key = f"{CUSTOMER_EMAIL_CACHE_PREFIX}{customer_id}"
            cache.set(cache_key, encrypt(email), timeout=CUSTOMER_EMAIL_CACHE_TTL)
            logger.debug(f"Cached customer email for {customer_id}")
        except Exception as e:
            # Don't fail webhook processing if caching fails
            logger.warning(f"Failed to cache customer email: {e}")

    def _get_cached_customer_email(self, customer_id: str) -> str:
        """Retrieve cached customer email by customer ID.

        Tolerates legacy plaintext entries written before encryption was
        introduced; an encrypted entry that no configured key can decrypt
        is treated as a cache miss rather than surfacing ciphertext.

        Args:
            customer_id: Stripe customer ID (e.g., cus_xxx).

        Returns:
            Cached email address, or empty string if not found.
        """
        if not customer_id:
            return ""
        try:
            cache_key = f"{CUSTOMER_EMAIL_CACHE_PREFIX}{customer_id}"
            cached = cache.get(cache_key)
            if cached:
                email = str(cached)
                if looks_like_token(email):
                    try:
                        email = decrypt(email)
                    except InvalidToken:
                        # Evict the entry: it can never decrypt again, and
                        # leaving it would repeat this warning (and a doomed
                        # decrypt attempt) on every lookup until TTL expiry.
                        cache.delete(cache_key)
                        logger.warning(
                            f"Cached email for {customer_id} could not be "
                            "decrypted; evicted and treated as cache miss"
                        )
                        return ""
                logger.debug(f"Found cached email for {customer_id}")
                return email
        except Exception as e:
            logger.warning(f"Failed to get cached customer email: {e}")
        return ""

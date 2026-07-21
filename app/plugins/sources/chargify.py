"""Chargify (Maxio Advanced Billing) source plugin implementation.

This module implements the BaseSourcePlugin interface for Chargify,
handling webhook validation, parsing, and customer data retrieval.
"""

import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, cast

from django.http import HttpRequest
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.sources.base import (
    BaseSourcePlugin,
    CustomerNotFoundError,
    InvalidDataError,
    mask_sensitive_headers,
    signed_content_hash,
)
from webhooks.utils.currency import from_minor_units

logger = logging.getLogger(__name__)


class ChargifySourcePlugin(BaseSourcePlugin):
    """Chargify (Maxio Advanced Billing) source plugin implementation.

    Handles webhook validation using HMAC signatures (SHA-256 preferred,
    with MD5 fallback) and parsing of various subscription and payment
    events. Deduplication is handled at the router level via the
    event consolidation service, keyed on the SHA-256 of the raw
    (HMAC-signed) request body surfaced as ``content_hash`` in the
    parsed event data. The unsigned ``X-Chargify-Webhook-Id`` header is
    never trusted for dedup; it is used only for logging.

    Attributes:
        EVENT_TYPE_MAPPING: Maps routable Chargify event names to the
            internal event types they are parsed into.
        ACKNOWLEDGED_EVENT_TYPES: Known Chargify event names that are
            deliberately not processed. They are acknowledged (200) and
            skipped so Chargify does not retry them forever.
    """

    # Every event listed here has a dispatch branch in
    # _handle_chargify_event. Do not add an event type without also
    # routing it there: advertised-but-unroutable events would be
    # rejected with a 400 and retried by Chargify indefinitely.
    #
    # Every value must be a member of EventProcessor.VALID_EVENT_TYPES:
    # emitting an unknown type raises ValueError downstream, which the
    # router turns into a 5xx and Chargify retries forever.
    EVENT_TYPE_MAPPING: ClassVar[dict[str, str]] = {
        # Payment events
        "payment_success": "payment_success",
        "payment_failure": "payment_failure",
        "renewal_success": "payment_success",
        "renewal_failure": "payment_failure",
        # Subscription change events. These emit subscription_updated by
        # default; a change into a cancellation-like state (see
        # _CANCELED_STATES) emits subscription_canceled instead.
        "subscription_state_change": "subscription_updated",
        "subscription_product_change": "subscription_updated",
        "subscription_billing_date_change": "subscription_updated",
        # Subscription lifecycle events
        "subscription_created": "subscription_created",
        "subscription_updated": "subscription_updated",
        "subscription_cancelled": "subscription_canceled",
        "subscription_expired": "subscription_canceled",
    }

    # Subscription states that represent the end of a subscription. A
    # state change into one of these is emitted as subscription_canceled.
    _CANCELED_STATES: ClassVar[frozenset[str]] = frozenset(
        {"canceled", "cancelled", "expired"}
    )

    # Known Chargify events we receive but do not act on. These must be
    # acknowledged with a 200 (logged and skipped), never rejected with
    # a 400: a 4xx/5xx makes Chargify retry the webhook forever.
    ACKNOWLEDGED_EVENT_TYPES: ClassVar[frozenset[str]] = frozenset(
        {
            "payment_refunded",
            "subscription_reactivated",
            "subscription_renewed",
            "customer_created",
            "customer_updated",
            "customer_deleted",
            "invoice_created",
            "invoice_updated",
            "invoice_paid",
            "signup_success",
            "signup_failure",
            "component_allocation_change",
        }
    )

    # Class-level constants
    _TIMESTAMP_TOLERANCE_SECONDS: ClassVar[int] = 300  # 5 minutes past tolerance
    # Small allowance for clock skew only - future-dated webhooks beyond
    # this are rejected to keep the replay window one-sided.
    _FUTURE_TIMESTAMP_TOLERANCE_SECONDS: ClassVar[int] = 60

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Chargify source plugin.
        """
        return PluginMetadata(
            name="chargify",
            display_name="Chargify (Maxio)",
            version="1.0.0",
            description="Chargify/Maxio webhook handler for subscriptions and payments",
            plugin_type=PluginType.SOURCE,
            capabilities={
                PluginCapability.WEBHOOK_VALIDATION,
                PluginCapability.CUSTOMER_DATA,
            },
            priority=100,
        )

    def __init__(self, webhook_secret: str = "") -> None:
        """Initialize plugin with webhook secret.

        Args:
            webhook_secret: Secret key for webhook signature validation.
        """
        super().__init__(webhook_secret)
        self._current_webhook_data: dict[str, Any] | None = None

    def _validate_webhook_timestamp(self, request: HttpRequest) -> bool:
        """Validate webhook timestamp to prevent replay attacks.

        The timestamp header is required and fails closed when absent: the
        signature only proves the body is authentic, not fresh, so a
        captured signed body could otherwise be replayed indefinitely by
        omitting the timestamp (and varying the unsigned webhook id). A
        missing timestamp is treated as a validation failure so the
        ±window always applies.

        NOTE: This is a window-narrowing mitigation that layers with the
        router's signed-content dedup. Chargify's HMAC covers the body
        only, not the timestamp header, so a captured (body, signature)
        pair can be replayed with a fresh in-tolerance timestamp - but
        because the router dedups on the SHA-256 of the signed body
        (``content_hash``), such a replay is suppressed as a duplicate
        for the dedup window regardless of any headers the attacker
        mints. The timestamp check rejects replays outside the ±window
        entirely, covering replays attempted after the dedup marker
        expires.

        This helper is side-effect-free: it returns a boolean and never
        logs, so the single caller (``validate_webhook``) logs a failure
        once. Logging here would let an unauthenticated caller emit two
        warning lines per request and flood the logs.

        Args:
            request: The incoming HTTP request.

        Returns:
            True if the timestamp is present and within tolerance, False
            if it is missing or invalid.
        """
        timestamp_header = request.headers.get("X-Chargify-Webhook-Timestamp")
        if not timestamp_header:
            # Fail closed: a missing timestamp would bypass the replay
            # window, so reject rather than accept.
            return False

        try:
            webhook_time = datetime.fromisoformat(
                timestamp_header.replace("Z", "+00:00")
            )
            current_time = datetime.now(timezone.utc)
            age_seconds = (current_time - webhook_time).total_seconds()

            # One-sided check: allow up to the tolerance in the past, but
            # only a small clock-skew allowance in the future. A symmetric
            # abs() check would widen the replay window for future-dated
            # webhooks.
            return not (
                age_seconds > self._TIMESTAMP_TOLERANCE_SECONDS
                or age_seconds < -self._FUTURE_TIMESTAMP_TOLERANCE_SECONDS
            )
        except (ValueError, TypeError):
            return False

    def validate_webhook(self, request: HttpRequest) -> bool:
        """Validate webhook signature and timestamp.

        Validates using SHA-256 HMAC if available, falling back to MD5
        for backward compatibility.

        Args:
            request: The incoming HTTP request.

        Returns:
            True if webhook is valid, False otherwise.
        """
        try:
            # Never bypass validation: an empty webhook secret means we
            # cannot verify the signature, so reject (even with DEBUG=True).
            if not self.webhook_secret:
                logger.error(
                    "SECURITY: Webhook secret not configured! "
                    "Rejecting webhook to prevent unauthorized access."
                )
                return False

            # Validate timestamp first
            if not self._validate_webhook_timestamp(request):
                logger.warning("Webhook timestamp validation failed")
                return False

            # Try SHA-256 first, fall back to MD5
            signature = request.headers.get("X-Chargify-Webhook-Signature-Hmac-Sha-256")
            use_sha256 = bool(signature)

            if not signature:
                signature = request.headers.get("X-Chargify-Webhook-Signature")

            webhook_id = request.headers.get("X-Chargify-Webhook-Id")

            logger.debug(
                "Validating Chargify webhook",
                extra={
                    "webhook_id": webhook_id,
                    "has_signature": bool(signature),
                    "signature_type": "sha256" if use_sha256 else "md5",
                    "content_type": request.content_type,
                    "headers": mask_sensitive_headers(request.headers),
                },
            )

            if not signature or not webhook_id:
                logger.warning(
                    "Missing required headers",
                    extra={
                        "webhook_id": webhook_id,
                        "has_signature": bool(signature),
                    },
                )
                return False

            body = request.body
            if use_sha256:
                expected_signature = hmac.new(
                    self.webhook_secret.encode(),
                    body,
                    hashlib.sha256,
                ).hexdigest()
            else:
                # MD5 is deprecated for cryptographic use - log warning
                logger.warning(
                    "Using MD5 signature validation (deprecated). "
                    "Configure Chargify to use SHA-256 signatures for better security.",
                    extra={"webhook_id": webhook_id},
                )
                expected_signature = hmac.new(
                    self.webhook_secret.encode(),
                    body,
                    hashlib.md5,
                ).hexdigest()

            # Log validation context for debugging (never signature values)
            logger.debug(
                "Webhook signature details",
                extra={
                    "webhook_id": webhook_id,
                    "body_length": len(body),
                    "secret_length": len(self.webhook_secret),
                    "signature_type": "sha256" if use_sha256 else "md5",
                },
            )

            is_valid = hmac.compare_digest(
                signature.lower(), expected_signature.lower()
            )
            if not is_valid:
                logger.warning(
                    "Invalid webhook signature",
                    extra={
                        "webhook_id": webhook_id,
                        "signature_type": "sha256" if use_sha256 else "md5",
                    },
                )

            return is_valid

        except Exception as e:
            logger.error(
                "Error validating Chargify webhook",
                extra={
                    "error": str(e),
                    "webhook_id": request.headers.get("X-Chargify-Webhook-Id"),
                },
                exc_info=True,
            )
            return False

    def get_customer_data(self, customer_id: str) -> dict[str, Any]:
        """Get customer data from stored webhook data.

        Args:
            customer_id: The customer identifier.

        Returns:
            Dictionary of customer information.

        Raises:
            CustomerNotFoundError: If no webhook data is available.
        """
        if not self._current_webhook_data:
            raise CustomerNotFoundError("No webhook data available")

        try:
            # Extract customer data from form fields
            customer_data: dict[str, Any] = {
                "company_name": self._current_webhook_data.get(
                    "payload[subscription][customer][organization]", ""
                ),
                "email": self._current_webhook_data.get(
                    "payload[subscription][customer][email]", ""
                ),
                "first_name": self._current_webhook_data.get(
                    "payload[subscription][customer][first_name]", ""
                ),
                "last_name": self._current_webhook_data.get(
                    "payload[subscription][customer][last_name]", ""
                ),
                "customer_id": customer_id,
                # The customer's signup date (NOT the webhook's timestamp)
                # feeds tenure display and anniversary detection. Absent
                # field -> empty -> those features simply stay silent.
                "created_at": self._current_webhook_data.get(
                    "payload[subscription][customer][created_at]", ""
                ),
                "plan_name": self._current_webhook_data.get(
                    "payload[subscription][product][name]", ""
                ),
                "team_size": self._current_webhook_data.get(
                    "payload[subscription][team_size]", ""
                ),
            }

            # Lifetime spend, under the key InsightDetector and
            # NotificationBuilder read (LTV display, VIP, milestones).
            # Only set when the payload actually carries the field:
            # LTV-based detectors treat a missing key as "unknown" and
            # stay silent, whereas a defaulted 0 would let a single big
            # payment falsely "cross" milestones.
            total_revenue_cents = self._current_webhook_data.get(
                "payload[subscription][total_revenue_in_cents]"
            )
            if total_revenue_cents is not None:
                customer_data["total_spent"] = float(total_revenue_cents) / 100

            return customer_data
        except (KeyError, ValueError) as e:
            raise CustomerNotFoundError(
                f"Failed to extract customer data: {e!s}"
            ) from e

    def _validate_chargify_request(self, request: HttpRequest) -> dict[str, Any]:
        """Validate Chargify webhook request and return form data.

        Args:
            request: The incoming HTTP request.

        Returns:
            Form data dictionary.

        Raises:
            InvalidDataError: If content type is invalid or data is missing.
        """
        if request.content_type != "application/x-www-form-urlencoded":
            raise InvalidDataError("Invalid content type")

        data = request.POST.dict()
        if not data:
            raise InvalidDataError("Missing required fields")

        return data

    def _handle_chargify_event(
        self, event_type: str, customer_id: str, data: dict[str, Any], webhook_id: str
    ) -> dict[str, Any]:
        """Route webhook event to appropriate handler.

        Deduplication happens at the router level (event consolidation
        service), keyed on the signed body hash surfaced via
        ``content_hash``.

        Args:
            event_type: The webhook event type (must be a key of
                ``EVENT_TYPE_MAPPING``).
            customer_id: Customer identifier.
            data: Form data dictionary.
            webhook_id: Webhook identifier (used for logging).

        Returns:
            Parsed event data dictionary.

        Raises:
            InvalidDataError: If event type is not routable.
        """
        if event_type in ("payment_success", "renewal_success"):
            # Renewal success is treated as payment success
            return self._parse_payment_success(data)
        elif event_type in ("payment_failure", "renewal_failure"):
            # Renewal failure is treated as payment failure
            return self._parse_payment_failure(data)
        elif event_type in (
            "subscription_state_change",
            "subscription_product_change",
            "subscription_billing_date_change",
        ):
            # Product and billing date changes are handled like state changes
            return self._parse_subscription_state_change(event_type, data)
        elif event_type in self.EVENT_TYPE_MAPPING:
            # Remaining routable events are subscription lifecycle events
            return self._parse_subscription_lifecycle(event_type, data)
        else:
            # Defensive: parse_webhook only dispatches routable events, so
            # this is unreachable through the webhook endpoint.
            logger.warning(
                f"Unroutable Chargify event type received: {event_type}",
                extra={"event_type": event_type, "webhook_id": webhook_id},
            )
            raise InvalidDataError(f"Unsupported event type: {event_type}")

    def parse_webhook(
        self, request: HttpRequest, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Parse Chargify webhook data.

        Known-but-unprocessed and unknown Chargify event types are logged
        and skipped (``None`` is returned, which the router acknowledges
        with a 200). Rejecting a legitimate Chargify event with a 400
        would make Chargify retry it forever.

        Args:
            request: The incoming HTTP request.
            **kwargs: Additional arguments (unused).

        Returns:
            Parsed event data dictionary, or None for events that are
            acknowledged but not processed.

        Raises:
            InvalidDataError: If webhook data is invalid or the
                ``X-Chargify-Webhook-Id`` header is missing.
        """
        # Never log the raw form body: it carries customer PII (email,
        # name, card last-4) and revenue on every request. Log only
        # non-sensitive metadata.
        logger.info(
            "Parsing Chargify webhook data",
            extra={
                "content_type": request.content_type,
                "event_type": request.POST.get("event") if request.POST else None,
                "webhook_id": request.headers.get("X-Chargify-Webhook-Id"),
                "headers": mask_sensitive_headers(request.headers),
            },
        )

        # Validate request and get data
        data = self._validate_chargify_request(request)

        # Store webhook data for customer lookup
        self._current_webhook_data = data

        event_type = data.get("event")
        if not event_type:
            raise InvalidDataError("Missing event type")

        # The webhook id is part of Chargify's webhook contract: every
        # genuine delivery carries it, so its absence indicates a forged
        # or broken request and is rejected (400). It is used only for
        # logging/traceability - dedup keys on the signed body hash, so
        # the unsigned header can no longer mint fresh dedup keys.
        webhook_id = request.headers.get("X-Chargify-Webhook-Id", "")
        if not webhook_id:
            raise InvalidDataError("Missing X-Chargify-Webhook-Id header")

        if event_type not in self.EVENT_TYPE_MAPPING:
            # Legitimate Chargify events we do not process are logged and
            # skipped so the router acknowledges them with a 200.
            log = (
                logger.info
                if event_type in self.ACKNOWLEDGED_EVENT_TYPES
                else logger.warning
            )
            log(
                f"Skipping unprocessed Chargify event type: {event_type}",
                extra={"event_type": event_type, "webhook_id": webhook_id},
            )
            return None

        customer_id = data.get("payload[subscription][customer][id]")
        if not customer_id:
            raise InvalidDataError("Missing customer ID")

        event = self._handle_chargify_event(event_type, customer_id, data, webhook_id)

        # Dedup keys on the signed body, never the unsigned webhook id:
        # Chargify's HMAC covers only the body, so a captured body
        # replayed with a fresh X-Chargify-Webhook-Id must map to the
        # SAME dedup key. Retries resend the identical body (identical
        # hash), while distinct events differ in signed fields (embedded
        # webhook id, transaction id, subscription state/timestamps).
        event["content_hash"] = signed_content_hash(request)
        return event

    def _parse_shopify_order_ref(self, memo: str) -> str | None:
        """Extract Shopify order reference from transaction memo.

        Args:
            memo: Transaction memo text.

        Returns:
            Shopify order reference if found, None otherwise.
        """
        if not memo:
            return None

        # First look for explicit mentions of Shopify order with any format
        match = re.search(r"Shopify Order[^\d]*(\d+)", memo, re.IGNORECASE)
        if match:
            return match.group(1)

        # Then look for any order number mentioned in an amount allocation
        match = re.search(r"allocated to[^$]*?(\d+)", memo, re.IGNORECASE)
        if match:
            return match.group(1)

        # Finally look for a standalone order number in the memo. The word
        # boundary prevents matches inside "reorder"/"preorder", and the
        # 4-digit minimum rejects short numbers (and thus most incidental
        # digits) that cannot be legitimate Shopify order references.
        match = re.search(r"\border\s*[#:]?\s*(\d{4,})\b", memo, re.IGNORECASE)
        if match:
            return match.group(1)

        return None

    def _extract_currency(self, data: dict[str, Any]) -> str:
        """Extract the currency code from a Chargify payload.

        Reads the subscription currency first, then the transaction
        currency. Falls back to USD with a warning when the payload
        carries neither, so a missing field cannot crash the parser.

        Args:
            data: Form data dictionary.

        Returns:
            Upper-cased ISO 4217 currency code.
        """
        currency = data.get("payload[subscription][currency]") or data.get(
            "payload[transaction][currency]"
        )
        if currency:
            return str(currency).upper()

        logger.warning(
            "Chargify payload missing currency; falling back to USD",
            extra={
                "subscription_id": data.get("payload[subscription][id]", ""),
                "transaction_id": data.get("payload[transaction][id]", ""),
            },
        )
        return "USD"

    def _parse_amount_cents(self, amount_cents: Any, currency: str) -> Decimal:
        """Convert an amount in minor units to a major-unit Decimal.

        Shared by all payment handlers so amount validation cannot drift
        between them. The currency determines the conversion factor
        (100 for USD/EUR, 1 for zero-decimal currencies like JPY).

        Args:
            amount_cents: Amount in minor units (typically a string form
                field).
            currency: ISO 4217 currency code for the amount.

        Returns:
            Amount in major units as a Decimal.

        Raises:
            InvalidDataError: If the amount is missing or not a number.
        """
        if amount_cents in (None, ""):
            raise InvalidDataError("Missing amount")

        try:
            amount: Decimal = from_minor_units(str(amount_cents), currency)
            return amount
        except (InvalidOperation, ValueError, TypeError) as e:
            raise InvalidDataError(f"Invalid amount format: {amount_cents}") from e

    @staticmethod
    def _extract_site_subdomain(data: dict[str, Any]) -> str:
        """Extract the Chargify site subdomain from a webhook payload.

        The subdomain identifies the per-site dashboard host
        (``<subdomain>.chargify.com``) and is threaded through event
        metadata so notification action buttons can link to the right
        site. Empty when the payload does not carry site information.

        The value is normalized (stripped, lowercased) here; the
        notification builder additionally validates it as a single DNS
        label before interpolating it into a URL host.

        Args:
            data: Form data dictionary.

        Returns:
            Normalized site subdomain string, or "" when unknown.
        """
        return str(data.get("payload[site][subdomain]", "") or "").strip().lower()

    def _parse_payment_success(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse payment_success webhook data.

        Args:
            data: Form data dictionary.

        Returns:
            Parsed event data dictionary.

        Raises:
            InvalidDataError: If required fields are missing.
        """
        currency = self._extract_currency(data)
        amount = self._parse_amount_cents(
            data.get("payload[transaction][amount_in_cents]"), currency
        )

        customer_data = self.get_customer_data(
            data["payload[subscription][customer][id]"]
        )

        # Extract Shopify order reference from memo
        memo = data.get("payload[transaction][memo]", "")
        shopify_order_ref = self._parse_shopify_order_ref(memo)

        # Safely get subscription data
        subscription_id = data.get("payload[subscription][id]", "")
        plan_name = data.get("payload[subscription][product][name]", "")

        # Extract payment method info
        payment_method = data.get("payload[transaction][payment_method]", "")
        card_type = data.get("payload[transaction][card_type]", "")
        card_last4 = data.get("payload[transaction][card_last_four]", "")

        # Determine billing period from product handle or interval
        billing_period = data.get("payload[subscription][product][interval]", "monthly")

        return {
            "type": "payment_success",
            "customer_id": data["payload[subscription][customer][id]"],
            "amount": float(amount),
            "currency": currency,
            "status": "success",
            "provider": "chargify",
            "metadata": {
                "subscription_id": subscription_id,
                "site_subdomain": self._extract_site_subdomain(data),
                "transaction_id": data.get("payload[transaction][id]", ""),
                "plan_name": plan_name,
                "shopify_order_ref": shopify_order_ref,
                "memo": memo,  # Include full memo for reference
                "billing_period": billing_period,
                "payment_method": payment_method,
                "card_type": card_type,
                "card_last4": card_last4,
            },
            "customer_data": customer_data,
        }

    def _parse_payment_failure(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse payment_failure webhook data.

        Args:
            data: Form data dictionary.

        Returns:
            Parsed event data dictionary.

        Raises:
            InvalidDataError: If required fields are missing.
        """
        currency = self._extract_currency(data)
        amount = self._parse_amount_cents(
            data.get("payload[transaction][amount_in_cents]"), currency
        )

        customer_data = self.get_customer_data(
            data["payload[subscription][customer][id]"]
        )

        # Extract payment method info
        payment_method = data.get("payload[transaction][payment_method]", "")
        card_type = data.get("payload[transaction][card_type]", "")
        card_last4 = data.get("payload[transaction][card_last_four]", "")

        # Determine billing period
        billing_period = data.get("payload[subscription][product][interval]", "monthly")

        # Subscription fields are optional: a payment failure for a
        # one-off charge has no subscription id or product name, and
        # that must not crash the parser (Chargify would retry forever).
        return {
            "type": "payment_failure",
            "customer_id": data["payload[subscription][customer][id]"],
            "amount": float(amount),
            "currency": currency,
            "status": "failed",
            "provider": "chargify",
            "metadata": {
                "subscription_id": data.get("payload[subscription][id]", ""),
                "site_subdomain": self._extract_site_subdomain(data),
                "transaction_id": data.get("payload[transaction][id]", ""),
                "plan_name": data.get("payload[subscription][product][name]", ""),
                "failure_reason": data.get(
                    "payload[transaction][failure_message]", "Unknown error"
                ),
                "billing_period": billing_period,
                "payment_method": payment_method,
                "card_type": card_type,
                "card_last4": card_last4,
            },
            "customer_data": customer_data,
        }

    def _parse_subscription_state_change(
        self, chargify_event: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Parse subscription state/product/billing date change webhooks.

        The emitted type is normalized to one the event processor
        understands: subscription_canceled when the new state is a
        cancellation-like state, subscription_updated otherwise. The
        actual Chargify state is preserved in ``status`` and
        ``metadata.new_state``.

        Payloads may omit fields like the product name - all lookups use
        defaults so a sparse payload cannot crash the parser.

        Args:
            chargify_event: The originating Chargify event type.
            data: Form data dictionary.

        Returns:
            Parsed event data dictionary.
        """
        customer_id = data.get("payload[subscription][customer][id]", "")
        customer_data = self.get_customer_data(customer_id)
        state = data.get("payload[subscription][state]", "unknown")
        internal_type = (
            "subscription_canceled"
            if state.lower() in self._CANCELED_STATES
            else "subscription_updated"
        )
        return {
            "type": internal_type,
            "customer_id": customer_id,
            "status": state,
            "provider": "chargify",
            "metadata": {
                "subscription_id": data.get("payload[subscription][id]", ""),
                "site_subdomain": self._extract_site_subdomain(data),
                "plan_name": data.get("payload[subscription][product][name]", ""),
                "new_state": state,
                "previous_state": data.get("payload[subscription][previous_state]"),
                "cancel_at_period_end": data.get(
                    "payload[subscription][cancel_at_end_of_period]"
                )
                == "true",
                "chargify_event": chargify_event,
            },
            "customer_data": customer_data,
        }

    def _parse_subscription_lifecycle(
        self, event_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Parse subscription lifecycle webhook data.

        Handles ``subscription_created``, ``subscription_updated``,
        ``subscription_cancelled``, and ``subscription_expired``, mapping
        them to the internal types declared in ``EVENT_TYPE_MAPPING``.

        Args:
            event_type: The Chargify event type.
            data: Form data dictionary.

        Returns:
            Parsed event data dictionary.
        """
        customer_id = data.get("payload[subscription][customer][id]", "")
        customer_data = self.get_customer_data(customer_id)
        return {
            "type": self.EVENT_TYPE_MAPPING[event_type],
            "customer_id": customer_id,
            "status": data.get("payload[subscription][state]", "unknown"),
            "provider": "chargify",
            "metadata": {
                "subscription_id": data.get("payload[subscription][id]", ""),
                "site_subdomain": self._extract_site_subdomain(data),
                "plan_name": data.get("payload[subscription][product][name]", ""),
                "previous_state": data.get("payload[subscription][previous_state]"),
                "cancel_at_period_end": data.get(
                    "payload[subscription][cancel_at_end_of_period]"
                )
                == "true",
                "chargify_event": event_type,
            },
            "customer_data": customer_data,
        }

    def get_event_type(self, event_data: dict[str, Any]) -> str:
        """Get event type from webhook data.

        Args:
            event_data: Parsed event data dictionary.

        Returns:
            Event type string.

        Raises:
            InvalidDataError: If event type is missing.
        """
        if not event_data or "type" not in event_data:
            raise InvalidDataError("Invalid event type")
        return cast(str, event_data["type"])

"""Chargify (Maxio Advanced Billing) source plugin implementation.

This module implements the BaseSourcePlugin interface for Chargify,
handling webhook validation, parsing, and customer data retrieval.
"""

import hashlib
import hmac
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, ClassVar, cast

from django.http import HttpRequest
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.sources.base import (
    BaseSourcePlugin,
    CustomerNotFoundError,
    InvalidDataError,
)

logger = logging.getLogger(__name__)


class ChargifySourcePlugin(BaseSourcePlugin):
    """Chargify (Maxio Advanced Billing) source plugin implementation.

    Handles webhook validation using HMAC signatures (SHA-256 preferred,
    with MD5 fallback), deduplication, and parsing of various subscription
    and payment events.

    Attributes:
        EVENT_TYPE_MAPPING: Maps Chargify event names to internal types.
    """

    EVENT_TYPE_MAPPING: ClassVar[dict[str, str]] = {
        # Payment events
        "payment_success": "payment_success",
        "payment_failure": "payment_failure",
        "payment_refunded": "payment_refunded",
        "renewal_success": "payment_success",
        "renewal_failure": "payment_failure",
        # Subscription events
        "subscription_state_change": "subscription_state_change",
        "subscription_product_change": "subscription_product_change",
        "subscription_billing_date_change": "subscription_billing_date_change",
        "subscription_created": "subscription_created",
        "subscription_updated": "subscription_updated",
        "subscription_cancelled": "subscription_cancelled",
        "subscription_reactivated": "subscription_reactivated",
        "subscription_expired": "subscription_expired",
        "subscription_renewed": "subscription_renewed",
        # Customer events
        "customer_created": "customer_created",
        "customer_updated": "customer_updated",
        "customer_deleted": "customer_deleted",
        # Invoice events
        "invoice_created": "invoice_created",
        "invoice_updated": "invoice_updated",
        "invoice_paid": "invoice_paid",
        # Signup events
        "signup_success": "signup_success",
        "signup_failure": "signup_failure",
        # Component events
        "component_allocation_change": "component_allocation_change",
    }

    # Class-level constants
    _CACHE_MAX_SIZE: ClassVar[int] = 1000
    _DEDUP_WINDOW_SECONDS: ClassVar[int] = 300  # 5 minutes
    _TIMESTAMP_TOLERANCE_SECONDS: ClassVar[int] = 300  # 5 minutes tolerance

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
        # Instance-level cache for recently processed webhook IDs.
        # Note: This cache is per-instance, meaning deduplication only works
        # within a single request lifecycle. For cross-request deduplication
        # in production, consider using Redis or another persistent store.
        # The instance-level cache is intentional to ensure test isolation.
        self._webhook_cache: OrderedDict[str, float] = OrderedDict()

    def _check_webhook_duplicate(self, webhook_id: str) -> bool:
        """Check if a webhook ID has been processed recently.

        Implements proper idempotency by tracking recently processed
        webhook IDs with a time-based cleanup.

        Args:
            webhook_id: The webhook identifier to check.

        Returns:
            True if this is a duplicate webhook, False otherwise.
        """
        if not webhook_id:
            logger.warning("No webhook ID provided for deduplication check")
            return False

        now = time.time()

        # Clean up old entries
        cutoff = now - self._DEDUP_WINDOW_SECONDS
        expired_keys = [k for k, v in self._webhook_cache.items() if v <= cutoff]
        for key in expired_keys:
            del self._webhook_cache[key]

        # Check if webhook ID has been processed
        if webhook_id in self._webhook_cache:
            logger.info(f"Duplicate webhook detected: {webhook_id}")
            return True

        # Add to cache
        self._webhook_cache[webhook_id] = now
        if len(self._webhook_cache) > self._CACHE_MAX_SIZE:
            self._webhook_cache.popitem(last=False)  # Remove oldest

        return False

    def _validate_webhook_timestamp(self, request: HttpRequest) -> bool:
        """Validate webhook timestamp to prevent replay attacks.

        Args:
            request: The incoming HTTP request.

        Returns:
            True if timestamp is valid or not present, False if invalid.
        """
        timestamp_header = request.headers.get("X-Chargify-Webhook-Timestamp")
        if not timestamp_header:
            # Timestamp is optional, so continue if not present
            return True

        try:
            webhook_time = datetime.fromisoformat(
                timestamp_header.replace("Z", "+00:00")
            )
            current_time = datetime.now(timezone.utc)
            age_seconds = abs((current_time - webhook_time).total_seconds())

            if age_seconds > self._TIMESTAMP_TOLERANCE_SECONDS:
                logger.warning(
                    "Webhook timestamp outside tolerance window",
                    extra={
                        "webhook_timestamp": timestamp_header,
                        "age_seconds": age_seconds,
                        "tolerance": self._TIMESTAMP_TOLERANCE_SECONDS,
                    },
                )
                return False

            return True
        except (ValueError, TypeError) as e:
            logger.warning(
                "Invalid webhook timestamp format",
                extra={"timestamp": timestamp_header, "error": str(e)},
            )
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
        from django.conf import settings as django_settings

        try:
            # For development/testing ONLY: allow bypassing validation when
            # webhook secret is empty. This MUST NOT work in production.
            if not self.webhook_secret:
                if not django_settings.DEBUG:
                    logger.error(
                        "SECURITY: Webhook secret not configured in production! "
                        "Rejecting webhook to prevent unauthorized access."
                    )
                    return False
                logger.warning(
                    "Webhook secret not configured - bypassing validation for "
                    "development. This would be rejected in production."
                )
                return True

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
                    "headers": dict(request.headers),
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

            # Log raw data for debugging
            logger.debug(
                "Webhook signature details",
                extra={
                    "webhook_id": webhook_id,
                    "body_length": len(body),
                    "secret_length": len(self.webhook_secret),
                    "signature_type": "sha256" if use_sha256 else "md5",
                    "expected_signature": expected_signature,
                    "received_signature": signature,
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
                        "expected_signature": expected_signature,
                        "received_signature": signature,
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
            return {
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
                "created_at": self._current_webhook_data.get("created_at", ""),
                "plan_name": self._current_webhook_data.get(
                    "payload[subscription][product][name]", ""
                ),
                "team_size": self._current_webhook_data.get(
                    "payload[subscription][team_size]", ""
                ),
                "total_revenue": float(
                    self._current_webhook_data.get(
                        "payload[subscription][total_revenue_in_cents]", 0
                    )
                )
                / 100,
            }
        except (KeyError, ValueError) as e:
            raise CustomerNotFoundError(
                f"Failed to extract customer data: {e!s}"
            ) from e

    def _extract_chargify_fields(
        self, data: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Extract subscription, customer, and transaction data from webhook.

        Args:
            data: Raw webhook form data.

        Returns:
            Tuple of (subscription, customer, transaction) dictionaries.
        """
        subscription: dict[str, Any] = {}
        customer: dict[str, Any] = {}
        transaction: dict[str, Any] = {}

        for key, value in data.items():
            if key.startswith("payload[subscription][customer]["):
                field = key.replace("payload[subscription][customer][", "").replace(
                    "]", ""
                )
                customer[field] = value
            elif key.startswith("payload[subscription]["):
                if "customer" not in key:  # Skip customer fields handled above
                    field = key.replace("payload[subscription][", "").replace("]", "")
                    subscription[field] = value
            elif key.startswith("payload[transaction]["):
                field = key.replace("payload[transaction][", "").replace("]", "")
                transaction[field] = value

        return subscription, customer, transaction

    def _determine_chargify_status(
        self, event_type: str, subscription: dict[str, Any]
    ) -> str:
        """Determine status based on event type and subscription state.

        Args:
            event_type: The webhook event type.
            subscription: Subscription data dictionary.

        Returns:
            Status string.
        """
        if event_type == "payment_failure":
            return "failed"
        elif event_type == "payment_success":
            return "success"
        elif event_type == "subscription_state_change":
            return cast(str, subscription.get("state", "unknown"))
        else:
            return cast(str, subscription.get("state", "unknown"))

    def _extract_chargify_amount(
        self, transaction: dict[str, Any], subscription: dict[str, Any]
    ) -> float:
        """Extract amount from transaction or subscription data.

        Args:
            transaction: Transaction data dictionary.
            subscription: Subscription data dictionary.

        Returns:
            Amount in dollars (converted from cents).
        """
        if transaction.get("amount_in_cents"):
            return float(transaction["amount_in_cents"]) / 100
        elif subscription.get("total_revenue_in_cents"):
            return float(subscription["total_revenue_in_cents"]) / 100
        else:
            return 0

    def _build_chargify_customer_data(
        self, customer: dict[str, Any], subscription: dict[str, Any]
    ) -> dict[str, Any]:
        """Build customer data structure.

        Args:
            customer: Customer data dictionary.
            subscription: Subscription data dictionary.

        Returns:
            Standardized customer data dictionary.
        """
        return {
            "id": customer.get("id"),
            "email": customer.get("email"),
            "first_name": customer.get("first_name"),
            "last_name": customer.get("last_name"),
            "company_name": customer.get("organization"),
            "subscription_status": subscription.get("state"),
            "plan_name": subscription.get("product", {}).get("name"),
        }

    def _build_chargify_response(
        self,
        event_type: str,
        customer_id: str,
        amount: float,
        status: str,
        data: dict[str, Any],
        subscription: dict[str, Any],
        customer_data: dict[str, Any],
        failure_reason: str | None,
    ) -> dict[str, Any]:
        """Build final response structure.

        Args:
            event_type: The webhook event type.
            customer_id: Customer identifier.
            amount: Payment amount.
            status: Event status.
            data: Raw webhook data.
            subscription: Subscription data.
            customer_data: Customer data dictionary.
            failure_reason: Reason for failure if applicable.

        Returns:
            Standardized event data dictionary.
        """
        return {
            "type": self.EVENT_TYPE_MAPPING.get(event_type, event_type),
            "customer_id": str(customer_id),
            "amount": amount,
            "currency": "USD",  # Chargify amounts are always in USD
            "status": status,
            "timestamp": datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
            "metadata": {
                "subscription_id": subscription.get("id"),
                "plan": subscription.get("product", {}).get("name"),
                "cancel_at_period_end": (
                    subscription.get("cancel_at_end_of_period") == "true"
                ),
                "failure_reason": failure_reason,
            },
            "customer_data": customer_data,
        }

    def _parse_webhook_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse webhook data into standardized format.

        Args:
            data: Raw webhook form data.

        Returns:
            Standardized event data dictionary.

        Raises:
            InvalidDataError: If required fields are missing.
        """
        event_type = data.get("event")
        if not event_type:
            raise InvalidDataError("Missing event type")

        # Extract fields from nested form data
        subscription, customer, transaction = self._extract_chargify_fields(data)

        # Determine status
        status = self._determine_chargify_status(event_type, subscription)

        # Extract amount
        amount = self._extract_chargify_amount(transaction, subscription)

        # Extract failure reason if present
        failure_reason = None
        if event_type == "payment_failure":
            failure_reason = transaction.get("failure_message")

        # Build customer data
        customer_data = self._build_chargify_customer_data(customer, subscription)

        # Extract customer ID
        customer_id = customer.get("id")
        if not customer_id:
            raise InvalidDataError("Missing customer ID")

        # Store webhook data for customer lookup
        self._current_webhook_data = data

        # Build and return response
        return self._build_chargify_response(
            event_type,
            customer_id,
            amount,
            status,
            data,
            subscription,
            customer_data,
            failure_reason,
        )

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

    def _get_chargify_event_info(self, data: dict[str, Any]) -> tuple[str, str]:
        """Extract event type and customer ID from webhook data.

        Args:
            data: Form data dictionary.

        Returns:
            Tuple of (event_type, customer_id).

        Raises:
            InvalidDataError: If required fields are missing.
        """
        event_type = data.get("event")
        if not event_type:
            raise InvalidDataError("Missing event type")

        customer_id = data.get("payload[subscription][customer][id]")
        if not customer_id:
            raise InvalidDataError("Missing customer ID")

        return event_type, customer_id

    def _handle_chargify_event(
        self, event_type: str, customer_id: str, data: dict[str, Any], webhook_id: str
    ) -> dict[str, Any]:
        """Route webhook event to appropriate handler with deduplication.

        Args:
            event_type: The webhook event type.
            customer_id: Customer identifier.
            data: Form data dictionary.
            webhook_id: Webhook identifier for deduplication.

        Returns:
            Parsed event data dictionary.

        Raises:
            InvalidDataError: If webhook is duplicate or event type unsupported.
        """
        # Check for duplicates using webhook ID (proper idempotency)
        if self._check_webhook_duplicate(webhook_id):
            raise InvalidDataError(f"Duplicate webhook: {webhook_id}")

        if event_type == "payment_success":
            return self._parse_payment_success(data)
        elif event_type == "payment_failure":
            return self._parse_payment_failure(data)
        elif event_type == "subscription_state_change":
            return self._parse_subscription_state_change(data)
        elif event_type == "renewal_success":
            # Renewal success is treated as payment success
            return self._parse_payment_success(data)
        elif event_type == "renewal_failure":
            # Renewal failure is treated as payment failure
            return self._parse_payment_failure(data)
        elif event_type in [
            "subscription_product_change",
            "subscription_billing_date_change",
        ]:
            # These are handled similarly to subscription state changes
            return self._parse_subscription_state_change(data)
        else:
            # For now, unsupported events are logged but don't cause failures
            logger.warning(
                f"Unsupported Chargify event type received: {event_type}",
                extra={"event_type": event_type, "webhook_id": webhook_id},
            )
            raise InvalidDataError(f"Unsupported event type: {event_type}")

    def parse_webhook(
        self, request: HttpRequest, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Parse Chargify webhook data.

        Args:
            request: The incoming HTTP request.
            **kwargs: Additional arguments (unused).

        Returns:
            Parsed event data dictionary.

        Raises:
            InvalidDataError: If webhook data is invalid.
        """
        logger.info(
            "Parsing Chargify webhook data",
            extra={
                "content_type": request.content_type,
                "form_data": (request.POST.dict() if request.POST else None),
                "headers": dict(request.headers),
            },
        )

        # Validate request and get data
        data = self._validate_chargify_request(request)

        # Store webhook data for customer lookup
        self._current_webhook_data = data

        # Get event info
        event_type, customer_id = self._get_chargify_event_info(data)

        # Handle the event
        webhook_id = request.headers.get("X-Chargify-Webhook-Id", "")
        return self._handle_chargify_event(event_type, customer_id, data, webhook_id)

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

        # Finally look for any order number in the memo
        match = re.search(r"order[^\d]*(\d+)", memo, re.IGNORECASE)
        if match:
            return match.group(1)

        return None

    def _parse_payment_success(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse payment_success webhook data.

        Args:
            data: Form data dictionary.

        Returns:
            Parsed event data dictionary.

        Raises:
            InvalidDataError: If required fields are missing.
        """
        amount = data.get("payload[transaction][amount_in_cents]")
        if not amount:
            raise InvalidDataError("Missing amount")

        # Validate amount is a valid number
        try:
            amount_float = float(amount) / 100
        except (ValueError, TypeError) as e:
            raise InvalidDataError(f"Invalid amount format: {amount}") from e

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
            "amount": amount_float,
            "currency": "USD",  # Chargify amounts are in USD
            "status": "success",
            "provider": "chargify",
            "metadata": {
                "subscription_id": subscription_id,
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
        amount = data.get("payload[transaction][amount_in_cents]")
        if not amount:
            raise InvalidDataError("Missing amount")

        customer_data = self.get_customer_data(
            data["payload[subscription][customer][id]"]
        )

        # Extract payment method info
        payment_method = data.get("payload[transaction][payment_method]", "")
        card_type = data.get("payload[transaction][card_type]", "")
        card_last4 = data.get("payload[transaction][card_last_four]", "")

        # Determine billing period
        billing_period = data.get("payload[subscription][product][interval]", "monthly")

        return {
            "type": "payment_failure",
            "customer_id": data["payload[subscription][customer][id]"],
            "amount": float(amount) / 100,  # Convert cents to dollars
            "currency": "USD",  # Chargify amounts are in USD
            "status": "failed",
            "provider": "chargify",
            "metadata": {
                "subscription_id": data["payload[subscription][id]"],
                "transaction_id": data.get("payload[transaction][id]", ""),
                "plan_name": data["payload[subscription][product][name]"],
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

    def _parse_subscription_state_change(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse subscription_state_change webhook data.

        Args:
            data: Form data dictionary.

        Returns:
            Parsed event data dictionary.
        """
        customer_data = self.get_customer_data(
            data["payload[subscription][customer][id]"]
        )
        return {
            "type": "subscription_state_change",
            "customer_id": data["payload[subscription][customer][id]"],
            "status": data["payload[subscription][state]"],
            "metadata": {
                "subscription_id": data["payload[subscription][id]"],
                "plan_name": data["payload[subscription][product][name]"],
                "previous_state": data.get("payload[subscription][previous_state]"),
                "cancel_at_period_end": data.get(
                    "payload[subscription][cancel_at_end_of_period]"
                )
                == "true",
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

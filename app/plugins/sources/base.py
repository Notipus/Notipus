"""Base class for source plugins (webhook providers).

Source plugins handle incoming webhooks from payment providers and other
external services, validating signatures and parsing event data.
"""

import logging
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from plugins.base import BasePlugin, PluginMetadata

logger = logging.getLogger(__name__)

# Webhook signature headers whose values must never be logged.
# Mirrors the masking policy of WebhookStorageService._extract_safe_headers.
_SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "stripe-signature",
        "x-chargify-webhook-signature",
        "x-chargify-webhook-signature-hmac-sha-256",
    }
)
_SENSITIVE_HEADER_PREFIXES: tuple[str, ...] = ("x-shopify-hmac",)


def mask_sensitive_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of request headers with signature values masked.

    Signature headers (Stripe-Signature, X-Chargify-Webhook-Signature*,
    X-Shopify-Hmac-*) are replaced with "[PRESENT]" so their values never
    leak into log sinks.

    Args:
        headers: Request headers mapping (e.g. ``request.headers``).

    Returns:
        Dictionary of headers safe for logging.
    """
    masked: dict[str, Any] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _SENSITIVE_HEADERS or lowered.startswith(
            _SENSITIVE_HEADER_PREFIXES
        ):
            masked[name] = "[PRESENT]"
        else:
            masked[name] = value
    return masked


# Exceptions for source plugins
class WebhookValidationError(Exception):
    """Raised when webhook validation fails.

    This exception indicates that the webhook signature or payload
    could not be verified, typically due to an invalid signature or
    tampered data.
    """


class WebhookError(Exception):
    """Base class for webhook-related errors.

    All webhook-specific exceptions should inherit from this class
    to allow for unified error handling.
    """


class InvalidDataError(WebhookError):
    """Raised when webhook data is invalid or malformed.

    This exception indicates that the webhook payload does not contain
    the expected fields or has invalid data formats.
    """


class CustomerNotFoundError(WebhookError):
    """Raised when customer data cannot be found.

    This exception indicates that the customer referenced in a webhook
    could not be located in the database or external service.
    """


class APIError(Exception):
    """Raised when an external API request fails.

    This exception wraps errors from external API calls to payment
    providers or other services.
    """


class InvalidEventType(WebhookError):
    """Raised when event type is not supported.

    This exception indicates that the webhook contains an event type
    that is not recognized or handled by the provider.
    """


@dataclass(slots=True)
class CustomerData:
    """Customer information from a source provider.

    Attributes:
        id: Unique customer identifier.
        email: Customer email address.
        name: Customer display name.
        created_at: Account creation timestamp.
        subscription_status: Current subscription state.
        subscription_id: Active subscription identifier.
        trial_end_date: When the trial period ends.
        payment_method: Payment method type/identifier.
        tags: Customer tags or labels.
    """

    id: str
    email: str
    name: str
    created_at: datetime
    subscription_status: str | None = None
    subscription_id: str | None = None
    trial_end_date: datetime | None = None
    payment_method: str | None = None
    tags: list[str] | None = None


@dataclass(slots=True)
class SubscriptionData:
    """Subscription information from a source provider.

    Attributes:
        id: Unique subscription identifier.
        status: Current subscription status.
        plan_name: Name of the subscription plan.
        created_at: Subscription creation timestamp.
        trial_end_date: When the trial period ends.
        next_billing_date: Next scheduled billing date.
        amount: Subscription amount.
        currency: Currency code for the amount.
    """

    id: str
    status: str
    plan_name: str
    created_at: datetime
    trial_end_date: datetime | None = None
    next_billing_date: datetime | None = None
    amount: float | None = None
    currency: str | None = None


@dataclass(slots=True)
class PaymentEvent:
    """Payment event data from a source webhook.

    Attributes:
        id: Unique event identifier.
        event_type: Type of payment event.
        customer_id: Associated customer identifier.
        amount: Payment amount.
        currency: Currency code.
        status: Payment status.
        timestamp: When the event occurred.
        subscription_id: Associated subscription if any.
        error_message: Error details for failures.
        retry_count: Number of retry attempts.
    """

    id: str
    event_type: str
    customer_id: str
    amount: float
    currency: str
    status: str
    timestamp: datetime
    subscription_id: str | None = None
    error_message: str | None = None
    retry_count: int | None = None


class BaseSourcePlugin(BasePlugin):
    """Base class for source plugins (webhook providers).

    Source plugins handle incoming webhooks from payment providers,
    validating signatures and parsing event data into a standardized format.

    Unlike enrichment plugins which are configured globally, source plugins
    are typically instantiated per-workspace with workspace-specific
    credentials (webhook_secret).

    Subclasses must implement:
    - get_metadata(): Return plugin metadata with plugin_type=SOURCE
    - validate_webhook(): Validate webhook signature
    - parse_webhook(): Parse webhook data into standardized format

    Subclasses may override:
    - get_customer_data(): Retrieve customer information
    - get_payment_history(): Get payment history for a customer
    - get_usage_metrics(): Get usage metrics for a customer
    - get_related_events(): Get related events for a customer

    Example:
        class MySourcePlugin(BaseSourcePlugin):
            @classmethod
            def get_metadata(cls) -> PluginMetadata:
                return PluginMetadata(
                    name="my_source",
                    display_name="My Source",
                    version="1.0.0",
                    description="Webhook handler for My Source",
                    plugin_type=PluginType.SOURCE,
                    capabilities={PluginCapability.WEBHOOK_VALIDATION},
                )

            def validate_webhook(self, request: HttpRequest) -> bool:
                # Validate signature
                return True

            def parse_webhook(self, request: HttpRequest) -> dict[str, Any]:
                # Parse and return standardized event data
                return {"type": "payment_success", ...}
    """

    def __init__(self, webhook_secret: str = "") -> None:
        """Initialize the source plugin with webhook credentials.

        Args:
            webhook_secret: Secret key for webhook validation.
        """
        self.webhook_secret = webhook_secret

    @classmethod
    @abstractmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Must set plugin_type=PluginType.SOURCE.

        Returns:
            PluginMetadata describing this source plugin.
        """
        pass

    @abstractmethod
    def validate_webhook(self, request: HttpRequest) -> bool:
        """Validate webhook signature and authenticity.

        Args:
            request: The incoming HTTP request containing the webhook.

        Returns:
            True if the webhook is valid, False otherwise.
        """
        pass

    @abstractmethod
    def parse_webhook(
        self, request: HttpRequest, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Parse webhook data into a standardized format.

        Args:
            request: The incoming HTTP request containing the webhook.
            **kwargs: Additional provider-specific arguments.

        Returns:
            Parsed webhook data dictionary, or None for test webhooks.
            The dictionary should contain at minimum:
            - type: Event type string
            - customer_id: Customer identifier
            - status: Event status

        Raises:
            InvalidDataError: If the webhook data is invalid.
        """
        pass

    def get_payment_history(self, customer_id: str) -> list[dict[str, Any]]:
        """Get payment history for a customer.

        Args:
            customer_id: The customer's unique identifier.

        Returns:
            List of payment records.
        """
        return []

    def get_usage_metrics(self, customer_id: str) -> dict[str, Any]:
        """Get usage metrics for a customer.

        Args:
            customer_id: The customer's unique identifier.

        Returns:
            Dictionary of usage metrics.
        """
        return {}

    def get_customer_data(self, customer_id: str) -> dict[str, Any]:
        """Get customer data from the source provider.

        Args:
            customer_id: The customer's unique identifier.

        Returns:
            Dictionary of customer information including:
            - company_name: Company name
            - email: Customer email
            - first_name: Customer first name
            - last_name: Customer last name
            - customer_id: The customer identifier for fallback display
        """
        return {"customer_id": customer_id}

    def get_related_events(self, customer_id: str) -> list[dict[str, Any]]:
        """Get related events for a customer.

        Args:
            customer_id: The customer's unique identifier.

        Returns:
            List of related event records.
        """
        return []

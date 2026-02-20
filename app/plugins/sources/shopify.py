"""Shopify source plugin implementation.

This module implements the BaseSourcePlugin interface for Shopify,
handling webhook validation, parsing, and customer data retrieval
using HMAC-SHA256 signature verification.
"""

import base64
import hashlib
import hmac
import json
import logging
from typing import Any, ClassVar

from django.http import HttpRequest
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.sources.base import (
    BaseSourcePlugin,
    CustomerNotFoundError,
    InvalidDataError,
)

logger = logging.getLogger(__name__)


class ShopifySourcePlugin(BaseSourcePlugin):
    """Handle Shopify webhooks and customer data.

    This plugin validates webhook signatures using HMAC-SHA256
    verification as recommended by Shopify's documentation.

    Attributes:
        EVENT_TYPE_MAPPING: Maps Shopify topics to internal event types.
    """

    EVENT_TYPE_MAPPING: ClassVar[dict[str, str]] = {
        # Order events
        "orders/create": "order_created",
        "orders/paid": "payment_success",
        "orders/cancelled": "payment_cancelled",
        "orders/fulfilled": "order_fulfilled",
        # Fulfillment events
        "fulfillments/create": "fulfillment_created",
        "fulfillments/update": "fulfillment_updated",
        # Customer events
        "customers/update": "customer_updated",
        # Test
        "test": "test",
    }

    # Topics that are fulfillment-specific (need different parsing)
    FULFILLMENT_TOPICS: ClassVar[set[str]] = {
        "fulfillments/create",
        "fulfillments/update",
    }

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the Shopify source plugin.
        """
        return PluginMetadata(
            name="shopify",
            display_name="Shopify",
            version="1.0.0",
            description="Shopify webhook handler for orders and customers",
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

    def _validate_shopify_request(self, request: HttpRequest) -> str:
        """Validate Shopify webhook request and return topic.

        Args:
            request: The incoming HTTP request.

        Returns:
            The webhook topic string.

        Raises:
            InvalidDataError: If content type or topic is invalid.
        """
        if request.content_type != "application/json":
            raise InvalidDataError("Invalid content type")

        topic = request.headers.get("X-Shopify-Topic")
        if not topic:
            raise InvalidDataError("Missing webhook topic")

        return topic

    def _parse_shopify_json(self, request: HttpRequest) -> dict[str, Any]:
        """Parse and validate Shopify JSON data.

        Args:
            request: The incoming HTTP request.

        Returns:
            Parsed JSON data dictionary.

        Raises:
            InvalidDataError: If JSON is invalid or empty.
        """
        try:
            # Support both request.data (DRF/pre-parsed) and request.body (Django raw).
            # DRF views may pre-parse JSON into request.data as a dict, while Django
            # views provide raw bytes in request.body. This handles both cases.
            body = getattr(request, "data", None) or request.body
            data = json.loads(body) if isinstance(body, (str, bytes)) else body
        except (json.JSONDecodeError, AttributeError) as e:
            raise InvalidDataError("Invalid JSON data") from e

        if not isinstance(data, dict):
            raise InvalidDataError("Invalid JSON data")
        if not data:
            raise InvalidDataError("Missing required fields")

        return data

    def _is_test_webhook(self, topic: str, request: HttpRequest) -> bool:
        """Check if this is a test webhook.

        Args:
            topic: The webhook topic.
            request: The incoming HTTP request.

        Returns:
            True if this is a test webhook, False otherwise.
        """
        return (
            topic == "test"
            or request.headers.get("X-Shopify-Test", "").lower() == "true"
        )

    def _extract_shopify_customer_id(self, data: dict[str, Any]) -> str:
        """Extract customer ID from Shopify webhook data.

        Args:
            data: Parsed webhook data.

        Returns:
            Customer ID string.

        Raises:
            InvalidDataError: If customer ID cannot be extracted.
        """
        try:
            if "customer" in data:
                customer_id = str(data["customer"]["id"])
            elif "order" in data and "customer" in data["order"]:
                customer_id = str(data["order"]["customer"]["id"])
            else:
                raw_id = data.get("id")
                if not raw_id:
                    raise InvalidDataError("Missing required fields")
                customer_id = str(raw_id)

            if not customer_id:
                raise InvalidDataError("Missing required fields")

            return customer_id
        except (KeyError, ValueError) as e:
            raise InvalidDataError("Missing required fields") from e

    def _extract_line_items(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract line items from Shopify order data.

        Args:
            data: Raw webhook data.

        Returns:
            List of line item dictionaries.
        """
        line_items = []
        for item in data.get("line_items", []):
            line_items.append(
                {
                    "name": item.get("name", item.get("title", "Unknown Product")),
                    "sku": item.get("sku", ""),
                    "quantity": item.get("quantity", 1),
                    "price": float(item.get("price", 0)),
                    "variant_title": item.get("variant_title", ""),
                }
            )
        return line_items

    def _extract_payment_method(self, data: dict[str, Any]) -> dict[str, Any]:
        """Extract payment method info from Shopify order data.

        Args:
            data: Raw webhook data.

        Returns:
            Payment method info dictionary.
        """
        payment_info: dict[str, Any] = {}

        # Gateway names (e.g., "shopify_payments", "paypal")
        gateways = data.get("payment_gateway_names", [])
        if gateways:
            payment_info["payment_gateway"] = gateways[0]

        # Payment details for credit cards
        payment_details = data.get("payment_details", {})
        if payment_details:
            payment_info["credit_card_company"] = payment_details.get(
                "credit_card_company"
            )
            # Last 4 digits from masked number
            cc_num = payment_details.get("credit_card_number", "")
            if cc_num:
                payment_info["card_last4"] = cc_num[-4:]

        return payment_info

    def _build_shopify_event_data(
        self,
        event_type: str,
        customer_id: str,
        data: dict[str, Any],
        topic: str,
    ) -> dict[str, Any]:
        """Build Shopify event data structure.

        Args:
            event_type: The internal event type.
            customer_id: Customer identifier.
            data: Raw webhook data.
            topic: The Shopify webhook topic.

        Returns:
            Standardized event data dictionary.

        Raises:
            InvalidDataError: If required fields have invalid formats.
        """
        # Extract payment method info
        payment_info = self._extract_payment_method(data)

        # Check for subscription contract (recurring order)
        is_recurring = bool(data.get("subscription_contract_id"))

        event_data: dict[str, Any] = {
            "type": event_type,
            "customer_id": customer_id,
            "provider": "shopify",
            "external_id": str(data.get("id", "")),
            "created_at": data.get("created_at"),
            "status": "success",  # Default status
            "metadata": {
                "order_number": data.get("order_number"),
                "order_ref": (
                    str(data.get("order_number")) if data.get("order_number") else None
                ),
                "financial_status": data.get("financial_status"),
                "fulfillment_status": data.get("fulfillment_status"),
                "line_items": self._extract_line_items(data),
                "is_recurring": is_recurring,
                "subscription_contract_id": data.get("subscription_contract_id"),
                **payment_info,
            },
        }

        # Add amount if present
        if "total_price" in data:
            try:
                event_data["amount"] = float(data["total_price"])
            except (ValueError, TypeError) as e:
                raise InvalidDataError("Missing required fields") from e

        # Add currency if present
        if "currency" in data:
            event_data["currency"] = data["currency"]

        # For customer updates, include customer data
        if topic == "customers/update":
            event_data["customer_data"] = {
                "company": data.get("company", ""),
                "email": data.get("email", ""),
                "first_name": data.get("first_name", ""),
                "last_name": data.get("last_name", ""),
                "orders_count": data.get("orders_count", 0),
                "total_spent": data.get("total_spent", "0.00"),
            }

        return event_data

    def _build_fulfillment_event_data(
        self,
        event_type: str,
        customer_id: str,
        data: dict[str, Any],
        topic: str,
    ) -> dict[str, Any]:
        """Build fulfillment-specific event data structure.

        Fulfillment webhooks have a different structure than order webhooks.

        Args:
            event_type: The internal event type.
            customer_id: Customer identifier.
            data: Raw fulfillment webhook data.
            topic: The Shopify webhook topic.

        Returns:
            Standardized event data dictionary.
        """
        # Extract fulfillment-specific fields
        tracking_number = data.get("tracking_number")
        tracking_company = data.get("tracking_company")
        tracking_url = data.get("tracking_url")
        fulfillment_status = data.get("status", data.get("shipment_status"))

        # Get line items from fulfillment
        line_items = []
        for item in data.get("line_items", []):
            line_items.append(
                {
                    "name": item.get("name", item.get("title", "Unknown Product")),
                    "sku": item.get("sku", ""),
                    "quantity": item.get("quantity", 1),
                }
            )

        event_data: dict[str, Any] = {
            "type": event_type,
            "customer_id": customer_id,
            "provider": "shopify",
            "external_id": str(data.get("id", "")),
            "created_at": data.get("created_at"),
            "status": "success",
            "metadata": {
                "order_id": data.get("order_id"),
                "order_number": data.get("order_number"),
                "order_ref": (
                    str(data.get("order_number")) if data.get("order_number") else None
                ),
                "fulfillment_id": data.get("id"),
                "fulfillment_status": fulfillment_status,
                "shipment_status": fulfillment_status,
                "tracking_number": tracking_number,
                "tracking_company": tracking_company,
                "tracking_url": tracking_url,
                "line_items": line_items,
            },
        }

        return event_data

    def _extract_customer_id_from_fulfillment(self, data: dict[str, Any]) -> str:
        """Extract customer ID from fulfillment webhook data.

        Fulfillment webhooks may not include customer data directly.

        Args:
            data: Parsed fulfillment webhook data.

        Returns:
            Customer ID string or fallback value.
        """
        # Try direct customer field
        if "customer" in data and data["customer"]:
            customer_id = data["customer"].get("id")
            if customer_id:
                return str(customer_id)

        # Try destination email as fallback identifier
        destination = data.get("destination", {})
        if destination and destination.get("email"):
            return str(destination["email"])

        # Use order_id as fallback
        order_id = data.get("order_id")
        if order_id:
            return f"order_{order_id}"

        # Last resort: use fulfillment ID
        fulfillment_id = data.get("id")
        if fulfillment_id:
            return f"fulfillment_{fulfillment_id}"

        raise InvalidDataError("Cannot extract customer identifier from fulfillment")

    def parse_webhook(
        self, request: HttpRequest, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Parse Shopify webhook data.

        Args:
            request: The incoming HTTP request.
            **kwargs: Additional arguments (unused).

        Returns:
            Parsed event data dictionary, or None for test webhooks.

        Raises:
            InvalidDataError: If webhook data is invalid.
        """
        # Validate request
        topic = self._validate_shopify_request(request)

        # Parse JSON data
        data = self._parse_shopify_json(request)

        # Store webhook data for later use
        self._current_webhook_data = data

        # Check for test webhook
        if self._is_test_webhook(topic, request):
            return None

        # Map webhook topic to event type
        event_type = self.EVENT_TYPE_MAPPING.get(topic)
        if not event_type:
            raise InvalidDataError(f"Unsupported webhook topic: {topic}")

        # Handle fulfillment-specific topics differently
        if topic in self.FULFILLMENT_TOPICS:
            customer_id = self._extract_customer_id_from_fulfillment(data)
            return self._build_fulfillment_event_data(
                event_type, customer_id, data, topic
            )

        # Extract customer ID for order/customer events
        customer_id = self._extract_shopify_customer_id(data)

        # Build and return event data
        return self._build_shopify_event_data(event_type, customer_id, data, topic)

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

        data = self._current_webhook_data
        customer = data.get("customer", {})
        if not customer and "order" in data:
            customer = data["order"].get("customer", {})

        return {
            "company": customer.get("company", "Individual"),
            "email": customer.get("email", ""),
            "first_name": customer.get("first_name", ""),
            "last_name": customer.get("last_name", ""),
            "orders_count": customer.get("orders_count", 0),
            "total_spent": customer.get("total_spent", "0.00"),
            "customer_id": customer_id,
            "metadata": {
                "shop_domain": data.get("shop_domain", ""),
                "tags": customer.get("tags", []),
                "note": customer.get("note", ""),
            },
        }

    def validate_webhook(self, request: HttpRequest) -> bool:
        """Validate the webhook signature using HMAC verification.

        This method validates Shopify webhooks using the standard
        HMAC-SHA256 verification process as recommended in Shopify's
        documentation.

        Args:
            request: The incoming HTTP request.

        Returns:
            True if signature is valid, False otherwise.

        Raises:
            TypeError: If request body is not bytes.
        """
        hmac_header = request.headers.get("X-Shopify-Hmac-SHA256")
        if not hmac_header:
            return False

        if not isinstance(request.body, (bytes, bytearray)):
            raise TypeError("Expected bytes or bytearray for request body")

        # Use manual validation as the primary method
        return self._manual_validate_webhook(request)

    def _manual_validate_webhook(self, request: HttpRequest) -> bool:
        """Validate webhook using manual HMAC calculation.

        This is the primary validation method using HMAC-SHA256.

        Args:
            request: The incoming HTTP request.

        Returns:
            True if signature is valid, False otherwise.
        """
        hmac_header = request.headers.get("X-Shopify-Hmac-SHA256")
        if not hmac_header:
            return False

        message = request.body
        secret = (
            self.webhook_secret.encode("utf-8")
            if isinstance(self.webhook_secret, str)
            else self.webhook_secret
        )

        digest = hmac.new(secret, message, hashlib.sha256).digest()
        calculated_hmac = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(hmac_header, calculated_hmac)

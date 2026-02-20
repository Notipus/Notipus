"""Stripe API service for customer and account operations.

This module provides a client for Stripe operations using the
official Stripe SDK, including Checkout Sessions and Customer Portal.
"""

import logging
from typing import TYPE_CHECKING, Any

import stripe
from django.conf import settings

if TYPE_CHECKING:
    from core.models import Workspace

logger = logging.getLogger(__name__)


def _safe_getattr(obj: Any, attr: str, default: Any = None) -> Any:
    """Safely get an attribute from a Stripe object.

    The Stripe SDK's __getattr__ raises KeyError instead of AttributeError
    when an attribute doesn't exist on certain object types (e.g., canceled
    or incomplete subscriptions). Since Python's getattr() only catches
    AttributeError, this function handles both exceptions.

    Args:
        obj: The object to get the attribute from.
        attr: The attribute name.
        default: Default value if attribute doesn't exist.

    Returns:
        The attribute value, or default if not found.
    """
    try:
        return getattr(obj, attr, default)
    except KeyError:
        return default


class StripeAPI:
    """API client for Stripe operations using the official Stripe SDK.

    Provides methods for account verification, customer management,
    Checkout Sessions, Customer Portal, and subscription management.

    Attributes:
        api_key: The Stripe API key to use for requests.
    """

    def __init__(self, api_key: str | None = None) -> None:
        """Initialize the Stripe client with the secret key.

        Args:
            api_key: Optional Stripe API key. If not provided, uses
                     settings.STRIPE_SECRET_KEY (for Notipus billing).
        """
        self.api_key = api_key or settings.STRIPE_SECRET_KEY
        # Configure Stripe with the secret key and API version
        stripe.api_key = self.api_key
        stripe.api_version = settings.STRIPE_API_VERSION

    def get_account_info(self) -> dict[str, Any] | None:
        """Retrieve Stripe account information to verify API key validity.

        Returns:
            Dict with account info if successful, None if API key is invalid.
        """
        try:
            # Temporarily set the API key for this request
            stripe.api_key = self.api_key

            # Retrieve the connected account info
            account = stripe.Account.retrieve()
            return {
                "id": account.id,
                "business_profile": {
                    "name": getattr(account.business_profile, "name", None),
                    "url": getattr(account.business_profile, "url", None),
                }
                if account.business_profile
                else {},
                "email": account.email,
                "country": account.country,
                "default_currency": account.default_currency,
            }
        except stripe.AuthenticationError as e:
            logger.warning(f"Invalid Stripe API key: {e!s}")
            return None
        except stripe.StripeError as e:
            logger.error(f"Stripe error retrieving account: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error retrieving Stripe account: {e!s}")
            return None

    def create_stripe_customer(
        self,
        customer_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create Stripe customer using the Stripe SDK.

        Args:
            customer_data: Dictionary of customer attributes.

        Returns:
            Created customer data dictionary, or None on failure.
        """
        try:
            # Configure Stripe API key for this operation
            stripe.api_key = self.api_key

            # Use Stripe SDK to create customer
            customer = stripe.Customer.create(**customer_data)
            return customer.to_dict()
        except stripe.StripeError as e:
            logger.error(f"Stripe error creating customer: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating Stripe customer: {e!s}")
            return None

    @staticmethod
    def create_stripe_customer_static(
        customer_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create Stripe customer using the default API key (static method).

        Kept for backward compatibility. Prefer using instance method.

        Args:
            customer_data: Dictionary of customer attributes.

        Returns:
            Created customer data dictionary, or None on failure.
        """
        try:
            # Configure Stripe API key and version for this operation
            stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe.api_version = settings.STRIPE_API_VERSION

            # Use Stripe SDK to create customer
            customer = stripe.Customer.create(**customer_data)
            return customer.to_dict()
        except stripe.StripeError as e:
            logger.error(f"Stripe error creating customer: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating Stripe customer: {e!s}")
            return None

    def get_or_create_customer(self, workspace: "Workspace") -> dict[str, Any] | None:
        """Get existing Stripe customer or create a new one for the workspace.

        If the workspace already has a stripe_customer_id, retrieves
        that customer. Otherwise, creates a new customer and updates
        the workspace with the new customer ID.

        Args:
            workspace: The Workspace instance.

        Returns:
            Customer data dictionary, or None on failure.
        """
        try:
            stripe.api_key = self.api_key

            # If workspace already has a Stripe customer, retrieve it
            if workspace.stripe_customer_id:
                try:
                    customer = stripe.Customer.retrieve(workspace.stripe_customer_id)
                    # Check if customer was deleted
                    if not _safe_getattr(customer, "deleted", False):
                        return customer.to_dict()
                    logger.warning(
                        f"Stripe customer {workspace.stripe_customer_id} was deleted"
                    )
                except stripe.InvalidRequestError:
                    logger.warning(
                        f"Stripe customer {workspace.stripe_customer_id} not found"
                    )

            # Create new customer
            customer_data = {
                "name": workspace.name,
                "metadata": {
                    "workspace_id": str(workspace.id),
                    "workspace_uuid": str(workspace.uuid),
                },
            }

            # Add email if workspace has members
            if hasattr(workspace, "members") and workspace.members.exists():
                first_member = workspace.members.first()
                if first_member and first_member.user.email:
                    customer_data["email"] = first_member.user.email

            customer = stripe.Customer.create(**customer_data)

            # Update workspace with new customer ID
            workspace.stripe_customer_id = customer.id
            workspace.save(update_fields=["stripe_customer_id"])

            logger.info(
                f"Created Stripe customer {customer.id} for workspace {workspace.id}"
            )
            return customer.to_dict()

        except stripe.StripeError as e:
            logger.error(f"Stripe error in get_or_create_customer: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in get_or_create_customer: {e!s}")
            return None

    def create_checkout_session(
        self,
        customer_id: str,
        price_id: str,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        trial_period_days: int | None = None,
    ) -> dict[str, Any] | None:
        """Create a Stripe Checkout Session for subscription.

        Args:
            customer_id: Stripe customer ID.
            price_id: Stripe price ID for the subscription.
            success_url: URL to redirect on successful checkout.
            cancel_url: URL to redirect on cancelled checkout.
            metadata: Additional metadata to attach to the session.
            trial_period_days: Number of days for trial period. If set,
                the subscription will start with a trial period.

        Returns:
            Checkout session data with 'url' for redirect, or None on failure.
        """
        try:
            stripe.api_key = self.api_key

            # Append session_id to success URL for retrieval after redirect
            # This avoids session cookie issues with cross-site redirects
            from urllib.parse import urlparse

            base_success_url = success_url or settings.STRIPE_SUCCESS_URL
            parsed = urlparse(base_success_url)
            separator = "&" if parsed.query else "?"
            success_url_with_session = (
                f"{base_success_url}{separator}session_id={{CHECKOUT_SESSION_ID}}"
            )

            session_params: dict[str, Any] = {
                "customer": customer_id,
                "payment_method_types": ["card"],
                "line_items": [
                    {
                        "price": price_id,
                        "quantity": 1,
                    }
                ],
                "mode": "subscription",
                "success_url": success_url_with_session,
                "cancel_url": cancel_url or settings.STRIPE_CANCEL_URL,
                "allow_promotion_codes": True,
                "billing_address_collection": "auto",
            }

            # Build subscription_data with metadata and/or trial period
            subscription_data: dict[str, Any] = {}
            if metadata:
                session_params["metadata"] = metadata
                subscription_data["metadata"] = metadata
            if trial_period_days is not None:
                subscription_data["trial_period_days"] = trial_period_days
            if subscription_data:
                session_params["subscription_data"] = subscription_data

            session = stripe.checkout.Session.create(**session_params)

            logger.info(
                f"Created checkout session {session.id} for customer {customer_id}"
            )
            return {
                "id": session.id,
                "url": session.url,
                "customer": session.customer,
                "status": session.status,
            }

        except stripe.StripeError as e:
            logger.error(f"Stripe error creating checkout session: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating checkout session: {e!s}")
            return None

    def retrieve_checkout_session(self, session_id: str) -> dict[str, Any] | None:
        """Retrieve a Stripe Checkout Session by ID.

        Args:
            session_id: The Stripe Checkout Session ID.

        Returns:
            Session data including metadata, or None on failure.
        """
        # Validate session_id format (Stripe checkout sessions start with "cs_")
        if not session_id or not session_id.startswith("cs_"):
            logger.warning(f"Invalid checkout session_id format: {session_id!r}")
            return None

        try:
            stripe.api_key = self.api_key
            session = stripe.checkout.Session.retrieve(session_id)

            return {
                "id": session.id,
                "customer": session.customer,
                "status": session.status,
                "metadata": dict(session.metadata) if session.metadata else {},
                "subscription": session.subscription,
            }

        except stripe.StripeError as e:
            logger.error(f"Stripe error retrieving checkout session: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error retrieving checkout session: {e!s}")
            return None

    def create_portal_session(
        self,
        customer_id: str,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Create a Stripe Customer Portal session.

        Allows customers to manage their subscription, payment methods,
        and view invoices through Stripe's hosted portal.

        Args:
            customer_id: Stripe customer ID.
            return_url: URL to redirect when customer exits portal.

        Returns:
            Portal session data with 'url' for redirect, or None on failure.
        """
        try:
            stripe.api_key = self.api_key

            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url or settings.STRIPE_PORTAL_RETURN_URL,
            )

            logger.info(f"Created portal session for customer {customer_id}")
            return {
                "id": session.id,
                "url": session.url,
                "customer": session.customer,
            }

        except stripe.StripeError as e:
            logger.error(f"Stripe error creating portal session: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating portal session: {e!s}")
            return None

    def _extract_features_from_metadata(
        self, metadata: dict[str, Any] | None
    ) -> list[str]:
        """Extract features list from product metadata.

        Args:
            metadata: Product metadata dictionary.

        Returns:
            List of feature strings.
        """
        if not metadata or not metadata.get("features"):
            return []

        import json

        features_raw = metadata.get("features", "")
        try:
            result: list[str] = json.loads(features_raw)
            return result
        except (json.JSONDecodeError, TypeError):
            # Features might be comma-separated string
            return [f.strip() for f in str(features_raw).split(",") if f.strip()]

    def _build_price_data(self, price: Any, product: Any) -> dict[str, Any]:
        """Build price data dictionary from Stripe price and product.

        Args:
            price: Stripe Price object.
            product: Stripe Product object.

        Returns:
            Formatted price data dictionary.
        """
        price_data: dict[str, Any] = {
            "id": price.id,
            "product_id": product.id,
            "product_name": product.name,
            "product_description": product.description,
            "unit_amount": price.unit_amount,
            "currency": price.currency,
            "recurring": None,
            "metadata": dict(product.metadata) if product.metadata else {},
            "features": self._extract_features_from_metadata(product.metadata),
        }

        if price.recurring:
            price_data["recurring"] = {
                "interval": price.recurring.interval,
                "interval_count": price.recurring.interval_count,
            }

        return price_data

    def list_prices(
        self,
        active_only: bool = True,
        product_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List prices from Stripe (source of truth for pricing).

        Args:
            active_only: Only return active prices.
            product_ids: Filter by specific product IDs.
            limit: Maximum number of prices to return.

        Returns:
            List of price dictionaries with product info.
        """
        try:
            stripe.api_key = self.api_key

            params: dict[str, Any] = {
                "limit": limit,
                "expand": ["data.product"],
            }

            if active_only:
                params["active"] = True

            prices = stripe.Price.list(**params)

            result = []
            for price in prices.data:
                product = price.product
                if isinstance(product, str):
                    product = stripe.Product.retrieve(product)

                # Apply filters
                if product_ids and product.id not in product_ids:
                    continue
                if active_only and not product.active:
                    continue

                result.append(self._build_price_data(price, product))

            logger.info(f"Retrieved {len(result)} prices from Stripe")
            return result

        except stripe.StripeError as e:
            logger.error(f"Stripe error listing prices: {e!s}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error listing prices: {e!s}")
            return []

    def _get_nested_value(self, obj: Any, key: str, default: Any = None) -> Any:
        """Get a value from an object using dict-style or attribute access.

        Stripe SDK objects can behave as both dicts and objects, and the
        access pattern can vary. This helper tries dict-style first, then
        falls back to _safe_getattr.

        Args:
            obj: The object to get the value from.
            key: The key/attribute name.
            default: Default value if not found.

        Returns:
            The value, or default if not found.
        """
        if obj is None:
            return default
        if hasattr(obj, "get"):
            return obj.get(key, default)
        return _safe_getattr(obj, key, default)

    def _extract_subscription_items(self, sub: Any) -> list[dict[str, Any]]:
        """Extract items from a Stripe subscription object.

        Args:
            sub: Stripe Subscription object.

        Returns:
            List of item dictionaries with price and product info.
        """
        items = []

        # Use dict-style access for Stripe SDK objects. Attribute access
        # (sub.items.data) can fail with 'builtin_function_or_method' error
        # because .items is both an attribute and a method in some contexts.
        try:
            items_obj = sub["items"] if "items" in sub else None
            items_data = (
                list(items_obj["data"]) if items_obj and "data" in items_obj else []
            )
        except (AttributeError, TypeError, KeyError) as e:
            sub_id = sub.get("id", "unknown") if hasattr(sub, "get") else "unknown"
            logger.warning(f"Failed to extract subscription items for {sub_id}: {e}")
            return []

        for item in items_data:
            # Use dict-style access for nested Stripe objects
            price = self._get_nested_value(item, "price")
            if not price:
                continue

            product = self._get_nested_value(price, "product")
            product_name = None
            plan_name = None

            if isinstance(product, str):
                # Product is not expanded, fetch it from Stripe
                try:
                    fetched_product = stripe.Product.retrieve(product)
                    product_name = fetched_product.name
                    # Prefer metadata.plan_name if available (more reliable)
                    if hasattr(fetched_product, "metadata"):
                        plan_name = fetched_product.metadata.get("plan_name")
                except stripe.StripeError:
                    product_name = None
            elif product:
                # Product is expanded as a dict or object
                product_name = self._get_nested_value(product, "name")
                metadata = self._get_nested_value(product, "metadata")
                if metadata:
                    plan_name = self._get_nested_value(metadata, "plan_name")

            items.append(
                {
                    "price_id": self._get_nested_value(price, "id"),
                    "product_name": product_name,
                    "plan_name": plan_name,
                    "unit_amount": self._get_nested_value(price, "unit_amount"),
                    "currency": self._get_nested_value(price, "currency"),
                    "quantity": self._get_nested_value(item, "quantity", 1),
                }
            )

        return items

    def get_customer_subscriptions(
        self,
        customer_id: str,
        status: str = "all",
    ) -> list[dict[str, Any]]:
        """Get subscriptions for a customer.

        Args:
            customer_id: Stripe customer ID.
            status: Filter by status ('all', 'active', 'canceled', etc.).

        Returns:
            List of subscription dictionaries.
        """
        try:
            stripe.api_key = self.api_key

            params: dict[str, Any] = {
                "customer": customer_id,
                # Note: Can't expand data.items.data.price.product (5 levels > 4 max)
                # Expand only to price level, fetch product separately if needed
                "expand": ["data.items.data.price"],
            }

            if status != "all":
                params["status"] = status

            subscriptions = stripe.Subscription.list(**params)

            result = []
            for sub in subscriptions.data:
                # Use _safe_getattr for attributes that may be missing
                # on canceled/incomplete subscriptions. Stripe SDK's __getattr__
                # raises KeyError (not AttributeError) for missing attributes.
                sub_data = {
                    "id": sub.id,
                    "status": sub.status,
                    "current_period_start": _safe_getattr(sub, "current_period_start"),
                    "current_period_end": _safe_getattr(sub, "current_period_end"),
                    "cancel_at_period_end": _safe_getattr(
                        sub, "cancel_at_period_end", False
                    ),
                    "canceled_at": _safe_getattr(sub, "canceled_at"),
                    "items": self._extract_subscription_items(sub),
                }
                result.append(sub_data)

            return result

        except stripe.StripeError as e:
            logger.error(f"Stripe error getting subscriptions: {e!s}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error getting subscriptions: {e!s}")
            return []

    def get_invoices(
        self,
        customer_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get invoices for a customer.

        Args:
            customer_id: Stripe customer ID.
            limit: Maximum number of invoices to return.

        Returns:
            List of invoice dictionaries.
        """
        try:
            stripe.api_key = self.api_key

            invoices = stripe.Invoice.list(
                customer=customer_id,
                limit=limit,
            )

            result = []
            for inv in invoices.data:
                result.append(
                    {
                        "id": inv.id,
                        "number": inv.number,
                        "status": inv.status,
                        "amount_due": inv.amount_due,
                        "amount_paid": inv.amount_paid,
                        "currency": inv.currency,
                        "created": inv.created,
                        "period_start": inv.period_start,
                        "period_end": inv.period_end,
                        "hosted_invoice_url": inv.hosted_invoice_url,
                        "invoice_pdf": inv.invoice_pdf,
                    }
                )

            return result

        except stripe.StripeError as e:
            logger.error(f"Stripe error getting invoices: {e!s}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error getting invoices: {e!s}")
            return []

    def get_price_by_lookup_key(self, lookup_key: str) -> dict[str, Any] | None:
        """Get a price by its lookup key.

        Lookup keys are more stable than price IDs for referencing prices.

        Args:
            lookup_key: The lookup key assigned to the price.

        Returns:
            Price data dictionary, or None if not found.
        """
        try:
            stripe.api_key = self.api_key

            prices = stripe.Price.list(
                lookup_keys=[lookup_key],
                expand=["data.product"],
            )

            if not prices.data:
                logger.warning(f"No price found for lookup key: {lookup_key}")
                return None

            price = prices.data[0]
            product = price.product

            return {
                "id": price.id,
                "product_id": product.id if hasattr(product, "id") else product,
                "product_name": product.name if hasattr(product, "name") else None,
                "unit_amount": price.unit_amount,
                "currency": price.currency,
                "lookup_key": price.lookup_key,
            }

        except stripe.StripeError as e:
            logger.error(f"Stripe error getting price by lookup key: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting price by lookup key: {e!s}")
            return None

    def create_product(
        self,
        name: str,
        description: str = "",
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Create a Stripe Product.

        Args:
            name: Product name (displayed on invoices, checkout, etc.).
            description: Product description.
            metadata: Additional metadata (e.g., plan_name, features).

        Returns:
            Created product data dictionary, or None on failure.
        """
        try:
            stripe.api_key = self.api_key

            product_params: dict[str, Any] = {
                "name": name,
            }

            if description:
                product_params["description"] = description

            if metadata:
                product_params["metadata"] = metadata

            product = stripe.Product.create(**product_params)

            logger.info(f"Created Stripe product {product.id}: {name}")
            return {
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "metadata": dict(product.metadata) if product.metadata else {},
                "active": product.active,
            }

        except stripe.StripeError as e:
            logger.error(f"Stripe error creating product: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating product: {e!s}")
            return None

    def create_price(
        self,
        product_id: str,
        unit_amount: int,
        currency: str = "usd",
        interval: str = "month",
        lookup_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Create a recurring Stripe Price for a product.

        Args:
            product_id: The Stripe Product ID to attach the price to.
            unit_amount: Price amount in cents (e.g., 2900 for $29.00).
            currency: Three-letter ISO currency code (default: usd).
            interval: Billing interval ('month' or 'year').
            lookup_key: Optional lookup key for stable price references.

        Returns:
            Created price data dictionary, or None on failure.
        """
        try:
            stripe.api_key = self.api_key

            price_params: dict[str, Any] = {
                "product": product_id,
                "unit_amount": unit_amount,
                "currency": currency,
                "recurring": {"interval": interval},
            }

            if lookup_key:
                price_params["lookup_key"] = lookup_key
                # Transfer lookup key if it already exists on another price
                price_params["transfer_lookup_key"] = True

            price = stripe.Price.create(**price_params)

            logger.info(
                f"Created Stripe price {price.id} for product {product_id}: "
                f"{unit_amount} {currency}/{interval}"
            )
            return {
                "id": price.id,
                "product": price.product,
                "unit_amount": price.unit_amount,
                "currency": price.currency,
                "recurring": {
                    "interval": price.recurring.interval,
                    "interval_count": price.recurring.interval_count,
                }
                if price.recurring
                else None,
                "lookup_key": price.lookup_key,
                "active": price.active,
            }

        except stripe.StripeError as e:
            logger.error(f"Stripe error creating price: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating price: {e!s}")
            return None

    def list_products(
        self,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List Stripe Products.

        Args:
            active_only: Only return active products.
            limit: Maximum number of products to return.

        Returns:
            List of product dictionaries.
        """
        try:
            stripe.api_key = self.api_key

            params: dict[str, Any] = {"limit": limit}

            if active_only:
                params["active"] = True

            products = stripe.Product.list(**params)

            result = []
            for product in products.data:
                result.append(
                    {
                        "id": product.id,
                        "name": product.name,
                        "description": product.description,
                        "metadata": dict(product.metadata) if product.metadata else {},
                        "active": product.active,
                    }
                )

            logger.info(f"Retrieved {len(result)} products from Stripe")
            return result

        except stripe.StripeError as e:
            logger.error(f"Stripe error listing products: {e!s}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error listing products: {e!s}")
            return []

    def get_product_by_metadata(
        self,
        key: str,
        value: str,
    ) -> dict[str, Any] | None:
        """Find a product by metadata key-value pair.

        Args:
            key: Metadata key to search for.
            value: Metadata value to match.

        Returns:
            Product data dictionary if found, None otherwise.
        """
        try:
            stripe.api_key = self.api_key

            # Stripe doesn't support direct metadata filtering in list,
            # so we need to fetch all and filter
            products = stripe.Product.list(limit=100, active=True)

            for product in products.data:
                if product.metadata and product.metadata.get(key) == value:
                    logger.info(f"Found product {product.id} with {key}={value}")
                    return {
                        "id": product.id,
                        "name": product.name,
                        "description": product.description,
                        "metadata": dict(product.metadata),
                        "active": product.active,
                    }

            logger.info(f"No product found with metadata {key}={value}")
            return None

        except stripe.StripeError as e:
            logger.error(f"Stripe error searching products: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error searching products: {e!s}")
            return None

    def update_product(
        self,
        product_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Update an existing Stripe Product.

        Args:
            product_id: The Stripe Product ID to update.
            name: New product name (optional).
            description: New product description (optional).
            metadata: New metadata to set (optional, replaces existing).

        Returns:
            Updated product data dictionary, or None on failure.
        """
        try:
            stripe.api_key = self.api_key

            update_params: dict[str, Any] = {}

            if name is not None:
                update_params["name"] = name
            if description is not None:
                update_params["description"] = description
            if metadata is not None:
                update_params["metadata"] = metadata

            if not update_params:
                logger.warning("No update parameters provided for product")
                return None

            product = stripe.Product.modify(product_id, **update_params)

            logger.info(f"Updated Stripe product {product.id}")
            return {
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "metadata": dict(product.metadata) if product.metadata else {},
                "active": product.active,
            }

        except stripe.StripeError as e:
            logger.error(f"Stripe error updating product: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error updating product: {e!s}")
            return None

    def archive_product(self, product_id: str) -> bool:
        """Archive a Stripe product by setting active=False.

        Archived products cannot be used for new subscriptions but
        existing subscriptions remain active.

        Args:
            product_id: The Stripe Product ID to archive.

        Returns:
            True if successfully archived, False otherwise.
        """
        try:
            stripe.api_key = self.api_key
            stripe.Product.modify(product_id, active=False)
            logger.info(f"Archived Stripe product: {product_id}")
            return True
        except stripe.StripeError as e:
            logger.error(f"Failed to archive product {product_id}: {e!s}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error archiving product {product_id}: {e!s}")
            return False

    def archive_price(self, price_id: str) -> bool:
        """Archive a Stripe price by setting active=False.

        Archived prices cannot be used for new subscriptions but
        existing subscriptions with this price remain active.

        Args:
            price_id: The Stripe Price ID to archive.

        Returns:
            True if successfully archived, False otherwise.
        """
        try:
            stripe.api_key = self.api_key
            stripe.Price.modify(price_id, active=False)
            logger.info(f"Archived Stripe price: {price_id}")
            return True
        except stripe.StripeError as e:
            logger.error(f"Failed to archive price {price_id}: {e!s}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error archiving price {price_id}: {e!s}")
            return False

    def list_prices_for_product(
        self,
        product_id: str,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List all prices for a specific product.

        Args:
            product_id: The Stripe Product ID to list prices for.
            active_only: Only return active prices (default True).

        Returns:
            List of price dictionaries.
        """
        try:
            stripe.api_key = self.api_key

            params: dict[str, Any] = {
                "product": product_id,
                "limit": 100,
            }
            if active_only:
                params["active"] = True

            prices = stripe.Price.list(**params)

            result = []
            for price in prices.data:
                result.append(
                    {
                        "id": price.id,
                        "product": price.product,
                        "unit_amount": price.unit_amount,
                        "currency": price.currency,
                        "lookup_key": price.lookup_key,
                        "active": price.active,
                        "recurring": {
                            "interval": price.recurring.interval,
                            "interval_count": price.recurring.interval_count,
                        }
                        if price.recurring
                        else None,
                    }
                )

            logger.info(f"Retrieved {len(result)} prices for product {product_id}")
            return result

        except stripe.StripeError as e:
            logger.error(f"Failed to list prices for {product_id}: {e!s}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error listing prices for {product_id}: {e!s}")
            return []

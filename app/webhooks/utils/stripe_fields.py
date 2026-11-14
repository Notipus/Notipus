"""Version-tolerant field extraction for Stripe invoice and subscription data.

Stripe moved several fields between API versions. On the configured
``STRIPE_API_VERSION`` (2025-12-15.clover):

- an invoice's subscription lives at
  ``parent.subscription_details.subscription`` instead of the top-level
  ``subscription`` field;
- a subscription's ``current_period_end`` lives on each subscription
  item (``items.data[].current_period_end``) instead of the subscription.

These helpers check both locations, so billing code works on either
shape. They accept plain dicts and Stripe SDK
objects alike.
"""

from collections.abc import Mapping
from typing import Any, cast

import stripe


def _field(obj: Any, key: str) -> Any:
    """Read ``key`` from a mapping or Stripe object, or None when absent.

    Args:
        obj: A dict, a Stripe SDK object, or anything else.
        key: Field name.

    Returns:
        The field value, or None.
    """
    if isinstance(obj, Mapping):
        return obj.get(key)
    if isinstance(obj, stripe.StripeObject):
        # StripeObject is not a dict (no .get), but supports ``in``/[].
        return obj[key] if key in obj else None
    return None


def _object_id(value: Any) -> str | None:
    """Return an id string from an id or an expanded object.

    Args:
        value: An id string, an expanded object with an ``id``, or None.

    Returns:
        The id string, or None.
    """
    if isinstance(value, str):
        return value or None
    expanded_id = _field(value, "id")
    return expanded_id if isinstance(expanded_id, str) and expanded_id else None


def invoice_subscription_id(invoice: Any) -> str | None:
    """Return the id of the subscription an invoice belongs to.

    Args:
        invoice: Invoice data (webhook dict or Stripe object).

    Returns:
        Subscription id, or None for one-off invoices.
    """
    sub_id = _object_id(_field(invoice, "subscription"))
    if sub_id:
        return sub_id
    details = _field(_field(invoice, "parent"), "subscription_details")
    return _object_id(_field(details, "subscription"))


def _items_data(subscription: Any) -> list[Any]:
    """Return a subscription's item list for either payload shape.

    Webhook and SDK subscriptions nest items as ``{"data": [...]}``;
    StripeAPI.get_customer_subscriptions flattens them to a list.

    Args:
        subscription: Subscription data.

    Returns:
        List of item objects (possibly empty).
    """
    items = _field(subscription, "items")
    if isinstance(items, list):
        return items
    data = _field(items, "data")
    return list(data) if isinstance(data, list) else []


def subscription_period_end(subscription: Any) -> int | None:
    """Return a subscription's current period end (the next renewal).

    Reads the legacy top-level field first, then the first subscription
    item that carries one (clover API versions).

    Args:
        subscription: Subscription data (webhook dict, SDK object, or the
            flattened dict returned by StripeAPI.get_customer_subscriptions).

    Returns:
        Unix timestamp, or None when no period end is present.
    """
    period_end = _field(subscription, "current_period_end")
    if isinstance(period_end, int) and not isinstance(period_end, bool):
        return period_end
    for item in _items_data(subscription):
        item_end = _field(item, "current_period_end")
        if isinstance(item_end, int) and not isinstance(item_end, bool):
            return cast(int, item_end)
    return None

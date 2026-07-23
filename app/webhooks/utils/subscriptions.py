"""Amount and interval extraction for Stripe subscription payloads.

Shared by the Stripe source plugin (notification metadata) and
BillingService (trial-ending emails). Extraction never guesses: every
function returns None when the payload does not prove the value.
"""

from typing import Any


def extract_item_amount(item: dict[str, Any]) -> int | None:
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


def sum_item_amounts(items_data: Any) -> int | None:
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
        unit_amount = extract_item_amount(item)
        if unit_amount is None:
            continue
        quantity = item.get("quantity")
        if not isinstance(quantity, int) or quantity < 1:
            quantity = 1
        total += unit_amount * quantity
        found = True

    return total if found else None


def subscription_recurring_amount_cents(sub_data: dict[str, Any]) -> int | None:
    """Extract the total recurring amount in cents for a subscription.

    Modern multi-item subscriptions have a null top-level ``plan``, so
    the item amounts (``items[].plan.amount * quantity`` or
    ``items[].price.unit_amount * quantity``) are summed first, with
    the top-level plan as a legacy single-item fallback.

    Args:
        sub_data: Subscription payload dictionary.

    Returns:
        Total amount in cents, or None if not determinable.
    """
    items = sub_data.get("items")
    if isinstance(items, dict):
        items_total = sum_item_amounts(items.get("data"))
        if items_total is not None:
            return items_total

    plan = sub_data.get("plan")
    if isinstance(plan, dict) and plan.get("amount") is not None:
        return int(plan["amount"])

    return None


def _currency_field(obj: Any) -> str | None:
    """Return a dict's non-empty ``currency`` string, else None."""
    if isinstance(obj, dict):
        currency = obj.get("currency")
        if isinstance(currency, str) and currency:
            return currency
    return None


def subscription_currency(sub_data: dict[str, Any]) -> str | None:
    """Extract the ISO currency code for a subscription.

    Reads the top-level ``currency`` first, then the legacy top-level
    ``plan.currency``, then the first item's ``price.currency`` or
    ``plan.currency``.

    Args:
        sub_data: Subscription payload dictionary.

    Returns:
        Currency code string, or None if the payload does not carry one.
    """
    currency = _currency_field(sub_data) or _currency_field(sub_data.get("plan"))
    if currency:
        return currency

    items = sub_data.get("items")
    if not isinstance(items, dict):
        return None
    items_data = items.get("data")
    if not isinstance(items_data, list):
        return None

    for item in items_data:
        if not isinstance(item, dict):
            continue
        currency = _currency_field(item.get("price")) or _currency_field(
            item.get("plan")
        )
        if currency:
            return currency

    return None


def subscription_recurring_interval(sub_data: dict[str, Any]) -> str | None:
    """Extract the billing interval ("month", "year", ...) for a subscription.

    Reads the legacy top-level ``plan.interval`` first, then falls back
    to the first item's ``price.recurring.interval`` or
    ``plan.interval`` for prices-API payloads.

    Args:
        sub_data: Subscription payload dictionary.

    Returns:
        Billing interval string, or None if not determinable.
    """
    plan = sub_data.get("plan")
    if isinstance(plan, dict) and plan.get("interval"):
        return str(plan["interval"])

    items = sub_data.get("items")
    if not isinstance(items, dict):
        return None
    items_data = items.get("data")
    if not isinstance(items_data, list):
        return None

    for item in items_data:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        if isinstance(price, dict):
            recurring = price.get("recurring")
            if isinstance(recurring, dict) and recurring.get("interval"):
                return str(recurring["interval"])
        item_plan = item.get("plan")
        if isinstance(item_plan, dict) and item_plan.get("interval"):
            return str(item_plan["interval"])

    return None

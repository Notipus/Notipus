"""Shared utility functions for webhook services.

This module contains utility functions used across multiple webhook services
to avoid code duplication.
"""

from typing import Any, cast

# Billing period to headline interval suffix mapping. Unknown or missing
# periods fall back to "/mo" explicitly (most subscriptions are monthly).
INTERVAL_SUFFIX_MAP: dict[str, str] = {
    "monthly": "/mo",
    "month": "/mo",
    "annual": "/yr",
    "annually": "/yr",
    "yearly": "/yr",
    "year": "/yr",
    "quarterly": "/qtr",
    "quarter": "/qtr",
    "weekly": "/wk",
    "week": "/wk",
    "daily": "/day",
    "day": "/day",
}


def interval_suffix(billing_period: Any) -> str:
    """Map a billing period to a headline interval suffix.

    Args:
        billing_period: Billing period value from event metadata
            (e.g. "monthly", "annual"), or None.

    Returns:
        Interval suffix such as "/mo" or "/yr", defaulting to "/mo".
    """
    return INTERVAL_SUFFIX_MAP.get(str(billing_period or "").lower(), "/mo")


def get_display_name(customer_data: dict[str, Any]) -> str:
    """Get display name from customer data with smart fallbacks.

    Priority order:
    1. company_name or company field
    2. Customer's full name (first + last)
    3. Full email address
    4. Customer ID (formatted for readability)
    5. "Customer" as last resort

    Args:
        customer_data: Customer data dictionary.

    Returns:
        Display name string.
    """
    # Try company name first
    company_name = customer_data.get("company_name") or customer_data.get("company")
    if company_name and company_name != "Individual":
        return cast(str, company_name)

    # Try customer's full name
    first_name = customer_data.get("first_name", "")
    last_name = customer_data.get("last_name", "")
    if first_name or last_name:
        return f"{first_name} {last_name}".strip()

    # Use full email address as fallback
    email: str = customer_data.get("email", "")
    if email and "@" in email:
        return email

    # Use customer ID as fallback (e.g., "cus_TremsiHkK4YcSS")
    customer_id: str = customer_data.get("customer_id", "")
    if customer_id:
        return customer_id

    return "Customer"

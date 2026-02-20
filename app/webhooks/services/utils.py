"""Shared utility functions for webhook services.

This module contains utility functions used across multiple webhook services
to avoid code duplication.
"""

from typing import Any, cast


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

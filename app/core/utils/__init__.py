"""Core utility modules."""

from .cache_freshness import is_timestamp_fresh
from .email_domain import (
    extract_domain,
    is_disposable_email,
    is_enrichable_domain,
    is_free_email_provider,
    is_hosted_email_domain,
    is_payment_service_email,
)

__all__ = [
    "extract_domain",
    "is_disposable_email",
    "is_enrichable_domain",
    "is_free_email_provider",
    "is_hosted_email_domain",
    "is_payment_service_email",
    "is_timestamp_fresh",
]

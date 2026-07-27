"""Email domain utilities for filtering enrichable domains.

This module provides utilities for determining if an email domain
is worth enriching (i.e., belongs to a company vs free/disposable).
"""

import logging
import re
import unicodedata
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse

from disposable_email_domains import blocklist
from webhooks.utils.free_email_domains import FREE_EMAIL_DOMAINS

logger = logging.getLogger(__name__)

# Well-known free email providers - domains that should not be enriched.
# The curated database lives in webhooks.utils.free_email_domains (a
# dependency-light module) so the email classifier and this filter share
# one source of truth.
FREE_EMAIL_PROVIDERS: frozenset[str] = FREE_EMAIL_DOMAINS

# Hosted email domains - cloud providers where the subdomain represents
# the tenant/company. These domains should not be enriched since the tenant
# subdomain does not reliably map to a company website domain.
HOSTED_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "onmicrosoft.com",  # Azure AD / Microsoft 365
        "mail.onmicrosoft.com",  # Microsoft 365 mail subdomain
    }
)

# Maximum email length per RFC 5321
MAX_EMAIL_LENGTH = 254

# Simple email regex for basic validation
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def sanitize_email_input(value: Any) -> str | None:
    """Sanitize and validate email input.

    First line of defense against malicious input.

    Args:
        value: Raw input value.

    Returns:
        Sanitized email string or None if invalid.
    """
    # Must be a string
    if not isinstance(value, str):
        return None

    # Strip whitespace
    email = value.strip()

    # Check length
    if not email or len(email) > MAX_EMAIL_LENGTH:
        return None

    # Reject null bytes and control characters
    if "\x00" in email or any(ord(c) < 32 for c in email):
        return None

    # Normalize unicode (NFC) to prevent homograph attacks
    email = unicodedata.normalize("NFC", email)

    return email.lower()


def is_valid_email(email: str) -> bool:
    """Validate email format using stdlib.

    Args:
        email: Email address to validate.

    Returns:
        True if email format is valid.
    """
    if not email:
        return False

    # Use stdlib parseaddr for parsing
    _name, addr = parseaddr(email)
    if not addr or "@" not in addr:
        return False

    # Basic format check
    if not EMAIL_PATTERN.match(addr):
        return False

    # Extract and validate domain
    domain = addr.split("@")[1]

    # Reject IP address domains
    if domain.startswith("[") or domain[0].isdigit():
        return False

    # Reject localhost
    if domain in ("localhost", "localhost.localdomain"):
        return False

    # Must have at least one dot (TLD)
    if "." not in domain:
        return False

    # Validate domain structure via urlparse
    try:
        parsed = urlparse(f"http://{domain}")
        if not parsed.netloc or parsed.netloc != domain:
            return False
    except Exception:
        return False

    return True


def extract_domain(email: str) -> str | None:
    """Extract and normalize domain from email address.

    Args:
        email: Email address.

    Returns:
        Lowercase domain or None if invalid.
    """
    # Sanitize first
    sanitized = sanitize_email_input(email)
    if not sanitized:
        return None

    # Validate format
    if not is_valid_email(sanitized):
        return None

    # Extract domain
    try:
        _name, addr = parseaddr(sanitized)
        if not addr or "@" not in addr:
            return None

        domain = addr.split("@")[1].lower().strip()

        # Convert IDN to ASCII (punycode)
        try:
            domain = domain.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            pass  # Keep original if conversion fails

        return domain
    except Exception:
        return None


def is_free_email_provider(domain: str) -> bool:
    """Check if domain is a known free email provider.

    Args:
        domain: Domain to check.

    Returns:
        True if domain is a free email provider.
    """
    if not domain:
        return False
    return domain.lower() in FREE_EMAIL_PROVIDERS


def is_hosted_email_domain(domain: str) -> bool:
    """Check if domain is a hosted email provider with tenant subdomains.

    Hosted email domains are cloud providers (like Microsoft Azure AD) where
    the subdomain represents the tenant/company name, not the actual company
    website. For example, 'contoso.onmicrosoft.com' is a hosted domain where
    'contoso' is the tenant name.

    Args:
        domain: Domain to check (e.g., 'contoso.onmicrosoft.com').

    Returns:
        True if domain is a hosted email provider.
    """
    if not domain:
        return False

    domain_lower = domain.lower()

    # Check if domain ends with any hosted email domain suffix
    for hosted_domain in HOSTED_EMAIL_DOMAINS:
        if domain_lower.endswith(f".{hosted_domain}"):
            return True

    return False


def is_disposable_email(domain: str) -> bool:
    """Check if domain is a disposable/temporary email provider.

    Uses the disposable-email-domains package blocklist.

    Args:
        domain: Domain to check.

    Returns:
        True if domain is disposable.
    """
    if not domain:
        return False
    return domain.lower() in blocklist


def is_enrichable_domain(email: str) -> bool:
    """Determine if an email domain is worth enriching.

    Performs full validation and filtering:
    1. Sanitizes input
    2. Validates email format
    3. Extracts domain
    4. Checks not a free provider
    5. Checks not a disposable domain

    Args:
        email: Email address to check.

    Returns:
        True if domain should be enriched.
    """
    # Extract and validate domain
    domain = extract_domain(email)
    if not domain:
        logger.debug(f"Invalid email format, skipping enrichment: {email}")
        return False

    # Check if free provider
    if is_free_email_provider(domain):
        logger.debug(f"Free email provider, skipping enrichment: {domain}")
        return False

    # Check if disposable
    if is_disposable_email(domain):
        logger.debug(f"Disposable email domain, skipping enrichment: {domain}")
        return False

    # Check if hosted email domain (e.g., onmicrosoft.com)
    if is_hosted_email_domain(domain):
        logger.debug(f"Hosted email domain, skipping enrichment: {domain}")
        return False

    return True

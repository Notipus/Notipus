"""Helpers for keeping end-customer personal data out of log lines.

Log aggregation is retained longer and shared more widely than the database,
so end-customer email addresses must not be written to it verbatim. Use
:func:`mask_email` wherever an email would otherwise be interpolated into a
log message: the result keeps the domain (useful for debugging enrichment)
and a short keyed hash (so repeated events for one address can still be
correlated) without revealing the local part.
"""

import hashlib
import hmac

from django.conf import settings

_HASH_LENGTH = 10


def mask_email(email: object) -> str:
    """Return a log-safe stand-in for an email address.

    The local part is replaced by a short HMAC-SHA256 digest keyed with
    ``SECRET_KEY`` (so it cannot be reversed by hashing a list of candidate
    addresses); the domain is kept as-is.

    Args:
        email: The email address (any value is accepted defensively).

    Returns:
        A string like ``"<3f2a9c01be>@example.com"``, or ``"<none>"`` if
        ``email`` is empty or not a string.
    """
    if not isinstance(email, str) or not email.strip():
        return "<none>"
    normalized = email.strip().lower()
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:_HASH_LENGTH]
    _, sep, domain = normalized.rpartition("@")
    return f"<{digest}>@{domain}" if sep and domain else f"<{digest}>"

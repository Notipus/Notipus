"""Encrypt/decrypt helpers for PII values stored in the cache (Redis).

Slack Marketplace compliance requires customer data to be encrypted at
rest in every store, including Redis. These helpers reuse the
field-encryption cipher (ChaCha20-Poly1305, ``FIELD_ENCRYPTION_KEYS``,
see :mod:`core.encryption`) so PII-bearing cache values reach Redis as
ciphertext.

They operate on VALUES, not keys: call sites keep using their own
``cache.get``/``cache.set`` (preserving key prefixing, TTLs, and test
patch points) and wrap just the value::

    cache.set(key, encrypt_cache_value(record), timeout=ttl)
    record = decrypt_cache_value(cache.get(key))

Only PII-bearing values go through these helpers (pending webhook
payloads, cached customer emails, dashboard activity records).
Coordination keys stay plaintext on purpose: locks and attempt counters
rely on SET NX / numeric semantics that ciphertext cannot support, and
they carry no customer data.

Values are serialized as JSON (``default=str``) before encryption, so
they must be JSON-representable; non-JSON types (Decimal, datetime) are
stringified. Legacy plaintext values written before encryption was
enabled are passed through by :func:`decrypt_cache_value` unchanged, so
a deploy does not invalidate in-flight cache entries.
"""

import json
import logging
from typing import Any

from core.encryption import InvalidToken, decrypt, encrypt, looks_like_token

logger = logging.getLogger(__name__)


def encrypt_cache_value(value: Any) -> str:
    """Serialize a value to JSON and encrypt it for cache storage.

    Args:
        value: Any JSON-representable value (non-JSON types are
            stringified via ``default=str``).

    Returns:
        A ``pqc1:``-prefixed ciphertext token string.
    """
    token: str = encrypt(json.dumps(value, default=str))
    return token


def decrypt_cache_value(stored: Any, *, log_failures: bool = True) -> Any:
    """Decode a cache read that may hold an encrypted or legacy value.

    Args:
        stored: The raw value returned by ``cache.get``: None (miss), a
            ciphertext token written via :func:`encrypt_cache_value`, or
            a legacy plaintext value written before encryption.
        log_failures: Warn when a token cannot be decrypted. Callers
            that detect the failure themselves (raw value present but
            result None) and emit their own key-specific log pass False
            to avoid duplicate lines per poisoned entry.

    Returns:
        The decrypted, JSON-decoded value; the legacy value unchanged;
        or None for a miss. A token that no configured key can decrypt
        (key rotated away too early) is treated as a miss rather than
        surfacing garbage.
    """
    if stored is None:
        return None
    if isinstance(stored, str) and looks_like_token(stored):
        try:
            return json.loads(decrypt(stored))
        except InvalidToken:
            if log_failures:
                logger.warning(
                    "Cache value could not be decrypted with any configured "
                    "key; treating as a cache miss"
                )
            return None
    return stored

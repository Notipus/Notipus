"""Field-level encryption helpers using ChaCha20-Poly1305 (256-bit AEAD).

This module encrypts credential fields at rest. ChaCha20-Poly1305 with a
256-bit key is chosen deliberately:

* It is symmetric **at-rest** encryption (no asymmetric/lattice crypto), so a
  256-bit key preserves a ~128-bit post-quantum security margin against
  Grover's algorithm.
* ChaCha20-Poly1305 is fast and constant-time in pure software, with no
  dependency on AES-NI hardware acceleration.

Keys come from the ``FIELD_ENCRYPTION_KEYS`` Django setting: a list of
base64url-encoded 32-byte keys. The **first** key encrypts new values; **all**
keys are tried on decrypt, which enables zero-downtime key rotation:

    1. Generate a new key and prepend it to ``FIELD_ENCRYPTION_KEYS``.
    2. Deploy. New writes use the new key; old values still decrypt with the
       old (now secondary) key.
    3. Re-encrypt existing rows (re-save them) so everything uses the new key.
    4. Drop the old key from ``FIELD_ENCRYPTION_KEYS`` on the next deploy.

Stored token format (``text`` column)::

    "pqc1:" + base64url( nonce(12 bytes) || ciphertext_and_tag )

The ``pqc1:`` ASCII prefix marks real ciphertext so it can be distinguished
from legacy plaintext written before encryption was enabled. A value carrying
the prefix that fails to decrypt with every configured key raises
:class:`InvalidToken` (fail loud on key misconfiguration) rather than being
returned as if it were the plaintext secret.
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "InvalidToken",
    "TOKEN_PREFIX",
    "decrypt",
    "encrypt",
    "get_keys",
    "looks_like_token",
]

# ASCII version marker prefixing every ciphertext token.
TOKEN_PREFIX = "pqc1:"

# ChaCha20-Poly1305 parameters.
_KEY_SIZE = 32  # 256-bit key
_NONCE_SIZE = 12  # 96-bit nonce


class InvalidToken(Exception):
    """Raised when a value cannot be decrypted with any configured key."""


def looks_like_token(value: str) -> bool:
    """Return whether ``value`` is a ciphertext token produced by this module.

    Args:
        value: A stored string value.

    Returns:
        ``True`` if ``value`` carries the ``pqc1:`` prefix (real ciphertext),
        ``False`` otherwise (e.g. legacy plaintext).
    """
    return value.startswith(TOKEN_PREFIX)


def _b64url_encode(raw: bytes) -> str:
    """base64url-encode ``raw`` bytes to an ASCII string."""
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """base64url-decode ``text`` back to bytes."""
    return base64.urlsafe_b64decode(text.encode("ascii"))


def _derive_dev_key() -> str:
    """Derive a deterministic dev-only key from ``SECRET_KEY``.

    Returns:
        A base64url-encoded 32-byte key. NEVER used when the dev fallback is
        disallowed (i.e. production).
    """
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return _b64url_encode(digest)


@lru_cache(maxsize=1)
def get_keys() -> tuple[bytes, ...]:
    """Resolve (and cache) the raw 32-byte keys from configured settings.

    Resolution is lazy (first call) so build steps that never touch encrypted
    fields do not require keys. The dev-derived key is only used when the dev
    fallback is allowed; in production, missing keys fail loudly here so a
    throwaway key can never encrypt real credentials.

    Returns:
        A tuple of raw 32-byte keys; the first encrypts, all decrypt.

    Raises:
        ImproperlyConfigured: If no keys are configured (and the dev fallback
            is disallowed), or a configured key is not a base64url-encoded
            32-byte value.
    """
    configured = list(getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or [])
    if not configured:
        allow_dev_fallback = getattr(
            settings, "FIELD_ENCRYPTION_ALLOW_DEV_FALLBACK", settings.DEBUG
        )
        if not allow_dev_fallback:
            raise ImproperlyConfigured(
                "SECURITY ERROR: FIELD_ENCRYPTION_KEYS is not set. Set the "
                "FIELD_ENCRYPTION_KEYS environment variable to a "
                "comma-separated list of base64url-encoded 32-byte keys "
                "(first = primary). Generate one with: python -c "
                '"import os, base64; '
                'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
            )
        # Dev-only deterministic fallback so dev/CI/tests work without env.
        configured = [_derive_dev_key()]

    keys: list[bytes] = []
    for encoded in configured:
        try:
            raw = _b64url_decode(encoded)
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEYS contains a value that is not valid "
                "base64url. Generate keys with: python -c "
                '"import os, base64; '
                'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
            ) from exc
        if len(raw) != _KEY_SIZE:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEYS contains a key that is not 32 bytes "
                f"(got {len(raw)}). Each key must be a base64url-encoded "
                "32-byte (256-bit) value."
            )
        keys.append(raw)
    return tuple(keys)


def encrypt(value: str) -> str:
    """Encrypt a string with the primary key.

    Args:
        value: The plaintext to encrypt.

    Returns:
        A ``pqc1:``-prefixed base64url token as ``str``.
    """
    key = get_keys()[0]
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, value.encode("utf-8"), None)
    return TOKEN_PREFIX + _b64url_encode(nonce + ciphertext)


def decrypt(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt`, trying every key.

    Args:
        token: A ``pqc1:``-prefixed token.

    Returns:
        The decrypted plaintext ``str``.

    Raises:
        InvalidToken: If ``token`` is malformed or cannot be decrypted with
            any configured key.
    """
    if not looks_like_token(token):
        raise InvalidToken("Value is not a pqc1 ciphertext token.")
    try:
        blob = _b64url_decode(token[len(TOKEN_PREFIX) :])
    except (ValueError, TypeError) as exc:
        raise InvalidToken("Ciphertext token is not valid base64url.") from exc
    if len(blob) <= _NONCE_SIZE:
        raise InvalidToken("Ciphertext token is too short.")
    nonce, ciphertext = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:]
    for key in get_keys():
        try:
            plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, None)
        except InvalidTag:
            continue
        return plaintext.decode("utf-8")
    raise InvalidToken("No configured key could decrypt the ciphertext token.")

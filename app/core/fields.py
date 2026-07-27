"""Custom Django model fields that transparently encrypt data at rest.

These fields store their values as ChaCha20-Poly1305 ciphertext tokens (see
:mod:`core.encryption`) in ``text`` columns while preserving the exact Python
API of the plaintext fields they replace:

* :class:`EncryptedTextField` behaves like ``TextField`` / ``CharField`` and
  returns ``str``.
* :class:`EncryptedJSONField` behaves like ``JSONField`` and returns the
  decoded Python object (``dict`` by default).

Migration safety: a stored value is only treated as *legacy plaintext*
(written before encryption was enabled) when it does NOT carry the ``pqc1:``
ciphertext prefix. A value that DOES carry the prefix but fails to decrypt
indicates a wrong/rotated key, and the ``InvalidToken`` is re-raised so the
misconfiguration fails loud rather than silently returning ciphertext as if it
were the secret. A data migration re-saves legacy-plaintext rows so the
plaintext is replaced by ciphertext.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from core.encryption import (
    InvalidToken,
    decrypt,
    encrypt,
    looks_like_token,
)
from django.db import models

logger = logging.getLogger(__name__)

# Emit the "reading legacy plaintext" warning at most once per process to
# avoid flooding logs while data is being migrated.
_warned_legacy_plaintext = False


def _warn_legacy_plaintext(field_name: str) -> None:
    """Log a one-time warning that legacy plaintext was read.

    Args:
        field_name: The model field attribute name being read.
    """
    global _warned_legacy_plaintext
    if not _warned_legacy_plaintext:
        _warned_legacy_plaintext = True
        logger.warning(
            "Read legacy PLAINTEXT value from encrypted field %r. Run the "
            "credential encryption data migration to re-encrypt existing "
            "rows.",
            field_name,
        )


class _EncryptedFieldMixin:
    """Shared helpers for encrypted model fields."""

    def _label(self) -> str:
        """Return a human-readable field name for log messages."""
        return getattr(self, "attname", None) or type(self).__name__


class EncryptedTextField(_EncryptedFieldMixin, models.TextField):
    """A ``TextField`` whose value is encrypted at rest (ChaCha20-Poly1305).

    Encrypted tokens exceed 255 characters, so this is backed by ``text``
    rather than ``varchar``. Empty/``None`` values are stored verbatim.
    """

    def get_prep_value(self, value: Any) -> str | None:
        """Encrypt ``value`` on its way into the database.

        Args:
            value: The plaintext string (or ``None``/empty).

        Returns:
            The ciphertext token, or the value unchanged when empty/``None``.
        """
        if value is None or value == "":
            return cast("str | None", value)
        return cast(str, encrypt(str(value)))

    def from_db_value(
        self,
        value: str | None,
        expression: Any,
        connection: Any,
    ) -> str | None:
        """Decrypt ``value`` when loading it from the database.

        Falls back to the raw value only for genuine legacy plaintext (a value
        without the ``pqc1:`` prefix). A prefixed value that fails to decrypt
        means a wrong/rotated key and re-raises.

        Args:
            value: The raw column value (a ciphertext token, plaintext, None).
            expression: The originating query expression (unused).
            connection: The database connection (unused).

        Returns:
            The decrypted plaintext string, or ``None``/empty unchanged.

        Raises:
            InvalidToken: If ``value`` is a ciphertext token but cannot be
                decrypted with any configured key (key misconfiguration).
        """
        if value is None or value == "":
            return value
        if not looks_like_token(value):
            # Genuine pre-migration plaintext: no ciphertext prefix.
            _warn_legacy_plaintext(self._label())
            return value
        # Real ciphertext: a decrypt failure here (wrong/rotated key) must
        # propagate, never be returned as if it were the plaintext secret.
        return cast(str, decrypt(value))


class EncryptedJSONField(_EncryptedFieldMixin, models.TextField):
    """A ``JSONField``-like field whose JSON payload is encrypted at rest.

    The value is JSON-serialized, then encrypted, and stored in a ``text``
    column. On read it is decrypted and JSON-decoded, so callers keep working
    with native Python objects (a ``dict`` by default).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Force a JSON-compatible default (``dict``) unless overridden."""
        kwargs.setdefault("default", dict)
        super().__init__(*args, **kwargs)

    def get_prep_value(self, value: Any) -> str | None:
        """JSON-encode then encrypt ``value`` for storage.

        Args:
            value: A JSON-serializable Python object (or ``None``).

        Returns:
            The ciphertext token wrapping the JSON text, or ``None``.
        """
        if value is None:
            return None
        return cast(str, encrypt(json.dumps(value)))

    def from_db_value(
        self,
        value: str | None,
        expression: Any,
        connection: Any,
    ) -> Any:
        """Decrypt and JSON-decode ``value`` from the database.

        Falls back to JSON-decoding genuine legacy plaintext (the ``jsonb``
        column is cast to ``text`` by the schema migration, yielding JSON
        text). A ``pqc1:``-prefixed value that fails to decrypt, or a
        non-prefixed value that is not valid JSON, raises ``InvalidToken``
        rather than being silently mis-handled.

        Args:
            value: The raw column value.
            expression: The originating query expression (unused).
            connection: The database connection (unused).

        Returns:
            The decoded Python object, or ``None``.

        Raises:
            InvalidToken: If ``value`` is a ciphertext token but cannot be
                decrypted, or is neither valid ciphertext nor valid JSON.
        """
        if value is None:
            return None
        if not looks_like_token(value):
            # Genuine pre-migration plaintext must be valid JSON; anything
            # else is corruption / misconfiguration and must fail loud.
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                raise InvalidToken(
                    "Stored value is neither ciphertext nor valid JSON."
                ) from None
            _warn_legacy_plaintext(self._label())
            return decoded
        # Real ciphertext: a decrypt failure here must propagate.
        plaintext = decrypt(value)
        if plaintext == "":
            return None
        return json.loads(plaintext)

    def to_python(self, value: Any) -> Any:
        """Coerce ``value`` to a Python object for forms/deserialization.

        Args:
            value: A JSON string or an already-decoded object.

        Returns:
            The decoded Python object.
        """
        if value is None or not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value

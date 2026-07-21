"""Tests for the encrypted cache value helpers (issue #100).

Customer PII cached in Redis (pending webhook payloads, customer emails,
dashboard activity records) must be encrypted at rest. These tests cover
the value-level helpers plus their rollout compatibility guarantees.
"""

from decimal import Decimal
from typing import Any

import pytest
from core.encrypted_cache import decrypt_cache_value, encrypt_cache_value
from core.encryption import TOKEN_PREFIX


class TestEncryptCacheValue:
    """Values are serialized and leave for the cache as ciphertext."""

    @pytest.mark.parametrize(
        "value",
        [
            "billing@acme.com",
            {"email": "a@b.co", "amount": 29.99, "nested": {"names": ["Jo"]}},
            [{"event_data": {"type": "invoice_paid", "_queued_at": 1721.5}}],
        ],
    )
    def test_roundtrip(self, value: Any) -> None:
        """Encrypt then decrypt returns an equal value."""
        token = encrypt_cache_value(value)
        assert isinstance(token, str)
        assert token.startswith(TOKEN_PREFIX)
        assert decrypt_cache_value(token) == value

    def test_plaintext_never_visible_in_token(self) -> None:
        """The serialized plaintext must not appear in the token."""
        token = encrypt_cache_value({"email": "secret@example.com"})
        assert "secret@example.com" not in token

    def test_non_json_types_are_stringified(self) -> None:
        """Decimal and similar types survive via default=str."""
        token = encrypt_cache_value({"amount": Decimal("12.30")})
        assert decrypt_cache_value(token) == {"amount": "12.30"}


class TestDecryptCacheValue:
    """Reads tolerate misses, legacy plaintext, and stale tokens."""

    def test_none_is_a_miss(self) -> None:
        """A cache miss (None) stays None."""
        assert decrypt_cache_value(None) is None

    @pytest.mark.parametrize(
        "legacy",
        [
            "plain@example.com",
            '{"json": "string record"}',
            [{"event_data": {"type": "invoice_paid"}}],
            42,
        ],
    )
    def test_legacy_plaintext_passes_through(self, legacy: Any) -> None:
        """Values written before encryption are returned unchanged.

        A deploy that enables encryption must not invalidate in-flight
        pending events or cached records.
        """
        assert decrypt_cache_value(legacy) == legacy

    def test_undecryptable_token_is_a_miss(self) -> None:
        """A token no configured key can decrypt degrades to a miss.

        This is the key-rotated-away-too-early case: surfacing the raw
        token (or raising) would break webhook processing; a cache miss
        just costs a re-fetch.
        """
        bogus = TOKEN_PREFIX + "A" * 64
        assert decrypt_cache_value(bogus) is None

    def test_log_failures_false_suppresses_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Callers with their own failure logging can silence the warning.

        The pending queue logs a key-specific error for poisoned
        entries; a second generic warning per entry would be noise.
        """
        bogus = TOKEN_PREFIX + "A" * 64

        with caplog.at_level("WARNING", logger="core.encrypted_cache"):
            assert decrypt_cache_value(bogus, log_failures=False) is None
        assert caplog.records == []

        with caplog.at_level("WARNING", logger="core.encrypted_cache"):
            assert decrypt_cache_value(bogus) is None
        assert len(caplog.records) == 1

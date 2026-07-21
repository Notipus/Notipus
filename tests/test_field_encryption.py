"""Tests for at-rest credential encryption (issue #100).

Covers the low-level ChaCha20-Poly1305 helper (:mod:`core.encryption`), the
custom model fields (:mod:`core.fields`), key rotation, the legacy-plaintext
read fallback, wrong-key fail-loud behavior, and an end-to-end round trip
through the :class:`~core.models.Integration` model including a raw-column
check that the DB stores ``pqc1:`` ciphertext and never the plaintext.

All tests run offline under pytest-socket (no network access).
"""

import base64
import json
import os
from typing import Any

import pytest
from core import encryption
from core.encryption import (
    TOKEN_PREFIX,
    InvalidToken,
    decrypt,
    encrypt,
    get_keys,
    looks_like_token,
)
from core.fields import EncryptedJSONField, EncryptedTextField
from core.models import Integration, Workspace
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import override_settings


def _generate_key() -> str:
    """Return a fresh base64url-encoded 32-byte key."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def test_encrypt_decrypt_round_trip() -> None:
    """A value survives an encrypt/decrypt round trip unchanged."""
    plaintext = "whsec_super_secret_value"
    token = encrypt(plaintext)
    assert token != plaintext
    assert decrypt(token) == plaintext


def test_ciphertext_is_prefixed_and_not_plaintext() -> None:
    """Encrypted output is a pqc1 token, not the plaintext."""
    token = encrypt("hello")
    assert token.startswith(TOKEN_PREFIX)
    assert "hello" not in token


def test_encrypt_is_non_deterministic() -> None:
    """Two encryptions of the same value differ (random nonce)."""
    assert encrypt("same") != encrypt("same")


class TestRotation:
    """Key rotation: any configured key can decrypt; the first encrypts."""

    def test_secondary_key_decrypts_after_rotation(self) -> None:
        """A token from an old key still decrypts once a new key is primary."""
        old_key = _generate_key()
        new_key = _generate_key()

        # Encrypt with only the old key configured.
        with override_settings(FIELD_ENCRYPTION_KEYS=[old_key]):
            get_keys.cache_clear()
            token = encrypt("rotate-me")

        # Rotate: new key first (primary), old key retained for decryption.
        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key, old_key]):
            get_keys.cache_clear()
            assert decrypt(token) == "rotate-me"
            new_token = encrypt("fresh")

        # A token freshly written under the rotated config is NOT decryptable
        # by the old key alone.
        with override_settings(FIELD_ENCRYPTION_KEYS=[old_key]):
            get_keys.cache_clear()
            with pytest.raises(InvalidToken):
                decrypt(new_token)

        get_keys.cache_clear()

    def test_unknown_key_raises_invalid_token(self) -> None:
        """A token from a non-configured key fails to decrypt."""
        with override_settings(FIELD_ENCRYPTION_KEYS=[_generate_key()]):
            get_keys.cache_clear()
            token = encrypt("secret")

        with override_settings(FIELD_ENCRYPTION_KEYS=[_generate_key()]):
            get_keys.cache_clear()
            with pytest.raises(InvalidToken):
                decrypt(token)

        get_keys.cache_clear()

    def test_missing_keys_without_dev_fallback_fail_loud(self) -> None:
        """No keys + no dev fallback (i.e. production) raises lazily.

        Simulates DEBUG=False in production: resolution is lazy, so the error
        only surfaces on an actual encrypt/decrypt (e.g. during migrate),
        never merely on import or on non-encrypting build steps.
        """
        with override_settings(
            FIELD_ENCRYPTION_KEYS=[],
            FIELD_ENCRYPTION_ALLOW_DEV_FALLBACK=False,
        ):
            get_keys.cache_clear()
            with pytest.raises(ImproperlyConfigured):
                encrypt("anything")

        get_keys.cache_clear()

    def test_invalid_key_length_raises_improperly_configured(self) -> None:
        """A key that does not decode to 32 bytes is rejected."""
        short_key = base64.urlsafe_b64encode(os.urandom(16)).decode("ascii")
        with override_settings(FIELD_ENCRYPTION_KEYS=[short_key]):
            get_keys.cache_clear()
            with pytest.raises(ImproperlyConfigured):
                encrypt("anything")

        get_keys.cache_clear()

    def test_non_base64_key_raises_improperly_configured(self) -> None:
        """A key that is not valid base64url is rejected."""
        with override_settings(FIELD_ENCRYPTION_KEYS=["not valid base64 !!!"]):
            get_keys.cache_clear()
            with pytest.raises(ImproperlyConfigured):
                encrypt("anything")

        get_keys.cache_clear()


class TestEncryptedTextField:
    """Unit behavior of EncryptedTextField."""

    def test_prep_and_load_round_trip(self) -> None:
        """get_prep_value encrypts and from_db_value decrypts."""
        field = EncryptedTextField()
        stored = field.get_prep_value("token-123")
        assert stored != "token-123"
        assert stored.startswith(TOKEN_PREFIX)
        assert field.from_db_value(stored, None, None) == "token-123"

    def test_empty_values_stored_verbatim(self) -> None:
        """Empty/None values are not encrypted."""
        field = EncryptedTextField()
        assert field.get_prep_value("") == ""
        assert field.get_prep_value(None) is None
        assert field.from_db_value("", None, None) == ""
        assert field.from_db_value(None, None, None) is None

    def test_legacy_plaintext_read_fallback(self) -> None:
        """Undecryptable, non-prefixed values are returned as-is."""
        field = EncryptedTextField()
        assert field.from_db_value("legacy_plain_secret", None, None) == (
            "legacy_plain_secret"
        )

    def test_wrong_key_ciphertext_raises(self) -> None:
        """A pqc1 token under a wrong key fails loud (re-raises)."""
        with override_settings(FIELD_ENCRYPTION_KEYS=[_generate_key()]):
            get_keys.cache_clear()
            token = encrypt("top-secret")

        field = EncryptedTextField()
        with override_settings(FIELD_ENCRYPTION_KEYS=[_generate_key()]):
            get_keys.cache_clear()
            with pytest.raises(InvalidToken):
                field.from_db_value(token, None, None)

        get_keys.cache_clear()


class TestEncryptedJSONField:
    """Unit behavior of EncryptedJSONField."""

    def test_prep_and_load_round_trip(self) -> None:
        """A dict is JSON-encoded, encrypted, then decoded on load."""
        field = EncryptedJSONField()
        value = {"access_token": "xoxb-abc", "team": {"id": "T1"}}
        stored = field.get_prep_value(value)
        assert isinstance(stored, str)
        assert stored.startswith(TOKEN_PREFIX)
        assert "xoxb-abc" not in stored
        assert field.from_db_value(stored, None, None) == value

    def test_legacy_plaintext_json_fallback(self) -> None:
        """Legacy plaintext JSON (from jsonb->text cast) is JSON-decoded."""
        field = EncryptedJSONField()
        legacy = json.dumps({"access_token": "old"})
        assert field.from_db_value(legacy, None, None) == {"access_token": "old"}

    def test_none_round_trip(self) -> None:
        """None is preserved."""
        field = EncryptedJSONField()
        assert field.get_prep_value(None) is None
        assert field.from_db_value(None, None, None) is None

    def test_wrong_key_ciphertext_raises(self) -> None:
        """A pqc1 token under a wrong key fails loud (re-raises)."""
        with override_settings(FIELD_ENCRYPTION_KEYS=[_generate_key()]):
            get_keys.cache_clear()
            token = encrypt(json.dumps({"k": "v"}))

        field = EncryptedJSONField()
        with override_settings(FIELD_ENCRYPTION_KEYS=[_generate_key()]):
            get_keys.cache_clear()
            with pytest.raises(InvalidToken):
                field.from_db_value(token, None, None)

        get_keys.cache_clear()

    def test_non_json_non_token_raises(self) -> None:
        """A non-prefixed value that is not valid JSON re-raises."""
        field = EncryptedJSONField()
        with pytest.raises(InvalidToken):
            field.from_db_value("not-json-not-a-token", None, None)


class TestTokenDetection:
    """The pqc1 prefix heuristic used to separate ciphertext from plaintext."""

    def test_real_token_is_detected(self) -> None:
        """Output of encrypt() is recognized as a ciphertext token."""
        assert looks_like_token(encrypt("hello")) is True

    def test_plaintext_is_not_a_token(self) -> None:
        """Legacy plaintext values are not mistaken for ciphertext tokens."""
        assert looks_like_token("whsec_plain_secret") is False
        assert looks_like_token('{"access_token": "x"}') is False
        assert looks_like_token("") is False


@pytest.mark.django_db
class TestIntegrationModelEncryption:
    """End-to-end encryption through the Integration model."""

    @pytest.fixture
    def workspace(self) -> Workspace:
        """Create a workspace for integration rows."""
        return Workspace.objects.create(
            name="Enc Workspace",
            shop_domain="enc.myshopify.com",
        )

    def test_save_and_reload_preserves_api(self, workspace: Workspace) -> None:
        """Saved credentials reload identically (dict/str preserved)."""
        creds = {"access_token": "xoxb-secret", "team": {"id": "T42"}}
        integration = Integration.objects.create(
            workspace=workspace,
            integration_type="slack_notifications",
            oauth_credentials=creds,
            webhook_secret="whsec_reload_me",
        )
        reloaded = Integration.objects.get(pk=integration.pk)
        assert reloaded.oauth_credentials == creds
        assert reloaded.webhook_secret == "whsec_reload_me"
        # Property accessors that read oauth_credentials still work.
        assert reloaded.slack_bot_token == "xoxb-secret"
        assert reloaded.slack_team_id == "T42"

    def test_raw_db_column_is_encrypted(self, workspace: Workspace) -> None:
        """The raw stored column is a pqc1 token, never the plaintext secret."""
        integration = Integration.objects.create(
            workspace=workspace,
            integration_type="stripe_customer",
            oauth_credentials={"access_token": "PLAINTEXT_MARKER"},
            webhook_secret="whsec_PLAINTEXT_MARKER",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT oauth_credentials, webhook_secret "
                "FROM core_integration WHERE id = %s",
                [integration.pk],
            )
            raw_creds, raw_secret = cursor.fetchone()

        assert "PLAINTEXT_MARKER" not in raw_creds
        assert "PLAINTEXT_MARKER" not in raw_secret
        assert raw_creds.startswith(TOKEN_PREFIX)
        assert raw_secret.startswith(TOKEN_PREFIX)

    def test_reload_after_legacy_plaintext_write(
        self, workspace: Workspace, monkeypatch: Any
    ) -> None:
        """A row written as raw plaintext still reads back correctly."""
        integration = Integration.objects.create(
            workspace=workspace,
            integration_type="chargify",
            webhook_secret="placeholder",
        )
        # Simulate a pre-migration plaintext row by writing raw values.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core_integration "
                "SET webhook_secret = %s, oauth_credentials = %s WHERE id = %s",
                ["legacy_secret", json.dumps({"k": "v"}), integration.pk],
            )
        # Reset the one-time warning flag so the fallback path is exercised.
        monkeypatch.setattr(
            "core.fields._warned_legacy_plaintext", False, raising=False
        )
        reloaded = Integration.objects.get(pk=integration.pk)
        assert reloaded.webhook_secret == "legacy_secret"
        assert reloaded.oauth_credentials == {"k": "v"}

        # Re-saving encrypts it (mirrors the data migration behavior).
        reloaded.save()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT webhook_secret FROM core_integration WHERE id = %s",
                [integration.pk],
            )
            (raw_secret,) = cursor.fetchone()
        assert raw_secret.startswith(TOKEN_PREFIX)
        assert decrypt(raw_secret) == "legacy_secret"


def test_get_keys_returns_valid_key_material() -> None:
    """get_keys resolves to a non-empty tuple of 32-byte keys."""
    keys = get_keys()
    assert isinstance(keys, tuple)
    assert keys
    assert all(len(k) == 32 for k in keys)
    # Ensure the module exposes the custom InvalidToken used by fields.
    assert issubclass(encryption.InvalidToken, Exception)

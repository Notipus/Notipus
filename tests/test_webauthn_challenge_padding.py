"""Regression tests for WebAuthn challenge base64url padding.

Stored challenges are padded (``base64.urlsafe_b64encode``) while
``options_to_json`` serializes the challenge the client receives — and
echoes back — as *unpadded* base64url. The service must normalize the
client-supplied form before the database lookup, otherwise every real
passkey ceremony fails at the complete step even though unit tests that
round-trip padded strings pass.
"""

import base64

import pytest
from core.models import WebAuthnChallenge
from core.services.webauthn import WebAuthnService


def test_pad_challenge_restores_stripped_padding() -> None:
    """An unpadded base64url string is restored to its padded form."""
    raw = b"\x01\x02\x03\x04\x05" * 13  # 65 bytes -> padded encoding
    padded = base64.urlsafe_b64encode(raw).decode()
    unpadded = padded.rstrip("=")

    assert padded.endswith("=")
    assert WebAuthnService._pad_challenge(unpadded) == padded


def test_pad_challenge_is_noop_for_padded_input() -> None:
    """Already-padded challenges (legacy clients, tests) pass through."""
    padded = base64.urlsafe_b64encode(b"\xffthirty-two-bytes-of-challenge!").decode()

    assert WebAuthnService._pad_challenge(padded) == padded


@pytest.mark.django_db
def test_unpadded_client_challenge_matches_stored_signup_challenge() -> None:
    """The challenge a signup client echoes back must find the stored row.

    ``generate_signup_registration_options`` stores the padded form and
    hands the client the unpadded form via ``options_to_json``; after
    normalization the two must address the same database row.
    """
    service = WebAuthnService()
    options = service.generate_signup_registration_options(
        "peter", "peter.gibbons@initech.com"
    )

    client_challenge = options["challenge"]
    # py_webauthn emits unpadded base64url — the shape that regressed.
    assert "=" not in client_challenge

    normalized = service._pad_challenge(client_challenge)
    assert WebAuthnChallenge.objects.filter(
        challenge=normalized, challenge_type="signup_registration"
    ).exists()
    # Without normalization the lookup misses: this is the regression.
    assert not WebAuthnChallenge.objects.filter(challenge=client_challenge).exists()

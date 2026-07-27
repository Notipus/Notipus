"""Tests for WebAuthn challenge TTL enforcement and opportunistic cleanup.

These tests verify that stored WebAuthn challenges are time-boxed: a challenge
older than ``CHALLENGE_TTL`` is rejected at verification (using the same failure
path as a missing challenge), a fresh challenge is accepted, and stale
challenges are purged when a new challenge is created.
"""

import base64
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from core.models import WebAuthnChallenge, WebAuthnCredential
from core.services.webauthn import CHALLENGE_TTL, WebAuthnService
from django.contrib.auth.models import User
from django.utils import timezone


def _age_challenge(challenge: WebAuthnChallenge, age: timedelta) -> None:
    """Backdate a challenge's ``created_at`` past the auto_now_add default.

    Args:
        challenge: The challenge to backdate.
        age: How far in the past to set ``created_at``.
    """
    WebAuthnChallenge.objects.filter(pk=challenge.pk).update(
        created_at=timezone.now() - age
    )


@pytest.fixture
def user() -> User:
    """Create a persisted user for challenge ownership.

    Returns:
        A saved Django ``User`` instance.
    """
    return User.objects.create_user(username="alice", email="alice@example.com")


@pytest.fixture
def service() -> WebAuthnService:
    """Provide a WebAuthn service instance.

    Returns:
        A configured ``WebAuthnService``.
    """
    return WebAuthnService()


@pytest.mark.django_db
def test_registration_rejects_expired_challenge(
    service: WebAuthnService, user: User
) -> None:
    """An expired registration challenge is rejected without verifying crypto."""
    challenge = WebAuthnChallenge.objects.create(
        challenge="reg-challenge",
        user=user,
        challenge_type="registration",
    )
    _age_challenge(challenge, CHALLENGE_TTL + timedelta(minutes=1))

    with patch("core.services.webauthn.verify_registration_response") as mock_verify:
        result = service.verify_registration(
            user, {"challenge": "reg-challenge"}, "Passkey"
        )

    assert result is False
    mock_verify.assert_not_called()
    assert not WebAuthnCredential.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_registration_accepts_fresh_challenge(
    service: WebAuthnService, user: User
) -> None:
    """A challenge within the TTL is accepted and consumed."""
    challenge_str = base64.urlsafe_b64encode(b"registration-challenge").decode()
    WebAuthnChallenge.objects.create(
        challenge=challenge_str,
        user=user,
        challenge_type="registration",
    )

    verification = Mock()
    verification.credential_id = b"cred-id"
    verification.credential_public_key = b"public-key"
    verification.sign_count = 0

    with patch(
        "core.services.webauthn.verify_registration_response",
        return_value=verification,
    ):
        result = service.verify_registration(
            user, {"challenge": challenge_str}, "Passkey"
        )

    assert result is True
    assert WebAuthnCredential.objects.filter(user=user).exists()
    assert not WebAuthnChallenge.objects.filter(challenge=challenge_str).exists()


@pytest.mark.django_db
def test_authentication_rejects_expired_challenge(service: WebAuthnService) -> None:
    """An expired authentication challenge yields no authenticated user."""
    challenge = WebAuthnChallenge.objects.create(
        challenge="auth-challenge",
        user=None,
        challenge_type="authentication",
    )
    _age_challenge(challenge, CHALLENGE_TTL + timedelta(minutes=1))

    with patch("core.services.webauthn.verify_authentication_response") as mock_verify:
        result = service.verify_authentication(
            {"challenge": "auth-challenge", "id": "cred123"}
        )

    assert result is None
    mock_verify.assert_not_called()


@pytest.mark.django_db
def test_authentication_accepts_fresh_challenge(
    service: WebAuthnService, user: User
) -> None:
    """A fresh authentication challenge authenticates the credential owner."""
    challenge_str = base64.urlsafe_b64encode(b"authentication-challenge").decode()
    WebAuthnChallenge.objects.create(
        challenge=challenge_str,
        user=None,
        challenge_type="authentication",
    )
    WebAuthnCredential.objects.create(
        user=user,
        credential_id="cred123",
        public_key=base64.urlsafe_b64encode(b"public-key").decode(),
        sign_count=0,
        name="Passkey",
    )

    verification = Mock()
    verification.new_sign_count = 1

    with patch(
        "core.services.webauthn.verify_authentication_response",
        return_value=verification,
    ):
        result = service.verify_authentication(
            {"challenge": challenge_str, "id": "cred123"}
        )

    assert result == user
    assert not WebAuthnChallenge.objects.filter(challenge=challenge_str).exists()


@pytest.mark.django_db
def test_signup_rejects_expired_challenge(service: WebAuthnService) -> None:
    """An expired signup challenge does not create a user account."""
    challenge = WebAuthnChallenge.objects.create(
        challenge="signup-challenge",
        user=None,
        challenge_type="signup_registration",
    )
    _age_challenge(challenge, CHALLENGE_TTL + timedelta(minutes=1))

    with patch("core.services.webauthn.verify_registration_response") as mock_verify:
        result = service.complete_signup_registration(
            {"challenge": "signup-challenge"}, "bob", "bob@example.com"
        )

    assert result is None
    mock_verify.assert_not_called()
    assert not User.objects.filter(username="bob").exists()


@pytest.mark.django_db
def test_signup_accepts_fresh_challenge(service: WebAuthnService) -> None:
    """A fresh signup challenge creates the user and credential and is consumed."""
    challenge_str = base64.urlsafe_b64encode(b"signup-challenge").decode()
    WebAuthnChallenge.objects.create(
        challenge=challenge_str,
        user=None,
        challenge_type="signup_registration",
    )

    verification = Mock()
    verification.credential_id = b"cred-id"
    verification.credential_public_key = b"public-key"
    verification.sign_count = 0

    with patch(
        "core.services.webauthn.verify_registration_response",
        return_value=verification,
    ):
        result = service.complete_signup_registration(
            {"challenge": challenge_str}, "bob", "bob@example.com"
        )

    assert result is not None
    assert result.username == "bob"
    assert WebAuthnCredential.objects.filter(user=result).exists()
    assert not WebAuthnChallenge.objects.filter(challenge=challenge_str).exists()


@pytest.mark.django_db
def test_new_challenge_cleans_up_expired(service: WebAuthnService, user: User) -> None:
    """Creating a new challenge purges previously expired challenges."""
    stale = WebAuthnChallenge.objects.create(
        challenge="stale-challenge",
        user=None,
        challenge_type="authentication",
    )
    _age_challenge(stale, CHALLENGE_TTL + timedelta(minutes=1))

    fresh = WebAuthnChallenge.objects.create(
        challenge="fresh-challenge",
        user=None,
        challenge_type="authentication",
    )

    service.generate_registration_options(user)

    assert not WebAuthnChallenge.objects.filter(pk=stale.pk).exists()
    assert WebAuthnChallenge.objects.filter(pk=fresh.pk).exists()


@pytest.mark.django_db
def test_cleanup_expired_challenges_returns_count(service: WebAuthnService) -> None:
    """``cleanup_expired_challenges`` deletes and counts only expired rows."""
    expired = WebAuthnChallenge.objects.create(
        challenge="old",
        user=None,
        challenge_type="authentication",
    )
    _age_challenge(expired, CHALLENGE_TTL + timedelta(minutes=1))
    WebAuthnChallenge.objects.create(
        challenge="new",
        user=None,
        challenge_type="authentication",
    )

    count = service.cleanup_expired_challenges()

    assert count == 1
    assert WebAuthnChallenge.objects.filter(challenge="new").exists()
    assert not WebAuthnChallenge.objects.filter(challenge="old").exists()

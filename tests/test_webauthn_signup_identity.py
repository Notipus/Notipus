"""Tests that passkey signup creates exactly the account the begin step checked.

The complete step used to take ``username``/``email`` from its own request
body, so a client could pass the begin-step uniqueness check with one
address and create the account with another (for example a case variant of
an existing user's email). The identity is now bound to the stored
challenge and uniqueness is re-checked, ignoring case, before creation.
"""

import base64
import json
from unittest.mock import Mock, patch

import pytest
from allauth.account.models import EmailAddress
from core.models import WebAuthnChallenge
from core.services.webauthn import WebAuthnService, signup_identity_conflict
from django.contrib.auth.models import User
from django.test import Client

CHALLENGE = base64.urlsafe_b64encode(b"signup-identity-challenge").decode()


def _verification() -> Mock:
    """Build a stand-in for a successful registration verification.

    Returns:
        A mock with the attributes the service reads.
    """
    verification = Mock()
    verification.credential_id = b"cred-id"
    verification.credential_public_key = b"public-key"
    verification.sign_count = 0
    return verification


def _signup_challenge(username: str, email: str) -> WebAuthnChallenge:
    """Store a signup challenge bound to ``username``/``email``.

    Args:
        username: Username the begin step validated.
        email: Email the begin step validated.

    Returns:
        The stored challenge.
    """
    return WebAuthnChallenge.objects.create(
        challenge=CHALLENGE,
        user=None,
        challenge_type="signup_registration",
        signup_username=username,
        signup_email=email,
    )


@pytest.mark.django_db
def test_begin_binds_identity_to_challenge() -> None:
    """The begin step stores the validated username/email on the challenge."""
    response = Client().post(
        "/webauthn/signup/begin/",
        data=json.dumps({"username": "pat", "email": "pat@example.com"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    challenge = WebAuthnChallenge.objects.get(challenge_type="signup_registration")
    assert challenge.signup_username == "pat"
    assert challenge.signup_email == "pat@example.com"


@pytest.mark.django_db
def test_complete_ignores_body_identity() -> None:
    """Body username/email cannot replace the identity bound at begin."""
    User.objects.create_user(username="victim", email="victim@example.com")
    _signup_challenge("pat", "pat@example.com")

    with patch(
        "core.services.webauthn.verify_registration_response",
        return_value=_verification(),
    ):
        response = Client().post(
            "/webauthn/signup/complete/",
            data=json.dumps(
                {
                    "credential": {"challenge": CHALLENGE},
                    "username": "victim2",
                    "email": "VICTIM@example.com",
                }
            ),
            content_type="application/json",
        )

    assert response.status_code == 200
    assert User.objects.filter(email__iexact="victim@example.com").count() == 1
    assert User.objects.filter(username="pat", email="pat@example.com").exists()


@pytest.mark.django_db
def test_begin_rejects_case_variant_email() -> None:
    """An existing address in another letter case blocks signup at begin."""
    User.objects.create_user(username="owner", email="owner@example.com")

    response = Client().post(
        "/webauthn/signup/begin/",
        data=json.dumps({"username": "new", "email": "Owner@Example.COM"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Email already exists"


@pytest.mark.django_db
def test_begin_rejects_case_variant_username() -> None:
    """An existing username in another letter case blocks signup at begin."""
    User.objects.create_user(username="Owner", email="owner@example.com")

    response = Client().post(
        "/webauthn/signup/begin/",
        data=json.dumps({"username": "owner", "email": "other@example.com"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Username already exists"


@pytest.mark.django_db
def test_complete_rechecks_uniqueness() -> None:
    """An address claimed after begin (in any case) is not duplicated."""
    _signup_challenge("pat", "pat@example.com")
    User.objects.create_user(username="other", email="PAT@example.com")

    with patch(
        "core.services.webauthn.verify_registration_response",
        return_value=_verification(),
    ):
        result = WebAuthnService().complete_signup_registration(
            {"challenge": CHALLENGE}
        )

    assert result is None
    assert User.objects.filter(email__iexact="pat@example.com").count() == 1
    assert not WebAuthnChallenge.objects.filter(challenge=CHALLENGE).exists()


@pytest.mark.django_db
def test_complete_refuses_concurrent_signup_for_same_identity() -> None:
    """A signup whose identity lock is held by another request is refused."""
    _signup_challenge("pat", "pat@example.com")

    with (
        patch(
            "core.services.webauthn.verify_registration_response",
            return_value=_verification(),
        ),
        patch("core.services.webauthn.cache.add", return_value=False),
    ):
        result = WebAuthnService().complete_signup_registration(
            {"challenge": CHALLENGE}
        )

    assert result is None
    assert not User.objects.filter(username="pat").exists()


@pytest.mark.django_db
def test_conflict_checks_allauth_email_addresses() -> None:
    """A secondary allauth address counts as taken, ignoring case."""
    user = User.objects.create_user(username="owner", email="primary@example.com")
    EmailAddress.objects.create(user=user, email="second@example.com")

    assert signup_identity_conflict("x", "SECOND@example.com") == "email"
    assert signup_identity_conflict("x", "free@example.com") is None

"""Tests for email-verification scoping across auth methods.

Product rule: outgoing email is only needed for passkey signups. Slack
SSO users' emails are trusted as verified (Slack requires a verified
email for workspace accounts), so SSO must neither depend on nor
trigger verification email delivery.
"""

from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client


class TestSlackEmailTrust:
    """Test that Slack SSO emails are treated as verified."""

    def test_slack_provider_trusts_emails(self) -> None:
        """Test that the Slack provider declares VERIFIED_EMAIL.

        Without this, allauth records SSO emails unverified and sends a
        verification email that SSO users must never depend on - and
        SOCIALACCOUNT_EMAIL_AUTHENTICATION cannot match existing
        accounts by email.
        """
        assert settings.SOCIALACCOUNT_PROVIDERS["slack"]["VERIFIED_EMAIL"] is True

    def test_shopify_provider_does_not_trust_emails(self) -> None:
        """Test that email trust is scoped to Slack, not all providers."""
        assert "VERIFIED_EMAIL" not in settings.SOCIALACCOUNT_PROVIDERS["shopify"]


@pytest.mark.django_db
class TestPasskeySignupEmailVerification:
    """Test that passkey signups register and verify their email."""

    def _complete_signup(self, user: User) -> tuple[int, dict]:
        """POST a signup completion with the WebAuthn service mocked.

        Args:
            user: The user the mocked service returns as newly created.

        Returns:
            Tuple of (status_code, parsed JSON body).
        """
        client = Client()
        with patch(
            "core.views.webauthn.WebAuthnService.complete_signup_registration",
            return_value=user,
        ):
            response = client.post(
                "/webauthn/signup/complete/",
                data='{"credential": {"challenge": "x"}, '
                '"username": "pat", "email": "pat@example.com"}',
                content_type="application/json",
            )
        return response.status_code, response.json()

    def test_signup_sends_verification_email(self) -> None:
        """Test that completing a passkey signup emails a verification link."""
        user = User.objects.create_user(
            username="pat", email="pat@example.com", password=None
        )

        status, body = self._complete_signup(user)

        assert status == 200
        assert body["success"] is True
        assert len(mail.outbox) == 1
        assert "pat@example.com" in mail.outbox[0].to

    def test_signup_registers_unverified_email_address(self) -> None:
        """Test that the email lands in allauth as unverified.

        Unlike Slack SSO (provider-verified), a passkey user typed the
        address themselves - it must be recorded unverified until the
        confirmation link is clicked.
        """
        user = User.objects.create_user(
            username="pat", email="pat@example.com", password=None
        )

        self._complete_signup(user)

        email_address = EmailAddress.objects.get(user=user)
        assert email_address.email == "pat@example.com"
        assert email_address.verified is False

    def test_passkey_login_succeeds_with_multiple_auth_backends(self) -> None:
        """Test that passkey authentication logs in without a 500.

        With ModelBackend and allauth's backend both configured, a bare
        login() raises "must provide the backend argument" - which the
        broad except turned into a 500 for every passkey login/signup.
        """
        user = User.objects.create_user(
            username="pat", email="pat@example.com", password=None
        )
        client = Client()

        with patch(
            "core.views.webauthn.WebAuthnService.verify_authentication",
            return_value=user,
        ):
            response = client.post(
                "/webauthn/authenticate/complete/",
                data='{"credential": {"challenge": "x"}}',
                content_type="application/json",
            )

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_email_failure_does_not_fail_signup(self) -> None:
        """Test that a broken email backend never blocks account creation."""
        user = User.objects.create_user(
            username="pat", email="pat@example.com", password=None
        )

        with patch(
            "core.views.webauthn.setup_user_email",
            side_effect=RuntimeError("smtp down"),
        ):
            status, body = self._complete_signup(user)

        assert status == 200
        assert body["success"] is True

"""Tests that Slack sign-in never lands in an account with an unverified email.

A passkey signup accepts any typed address without proving ownership. With
email authentication on, allauth would otherwise sign the real owner's
first Slack login into that pre-created account and auto-connect it,
leaving the attacker's passkey working. These tests drive allauth's real
social-login completion against the project adapter.
"""

import pytest
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from core.models import WebAuthnCredential
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

EMAIL = "owner@example.com"


def _request() -> HttpRequest:
    """Build a GET request with a session and message storage attached.

    Returns:
        A request usable by allauth's login flow.
    """
    request = RequestFactory().get("/accounts/slack/login/callback/")
    SessionMiddleware(lambda r: HttpResponse()).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    request.user = AnonymousUser()
    return request


def _complete(
    email: str = EMAIL, uid: str = "U-OWNER"
) -> tuple[HttpRequest, HttpResponse]:
    """Run allauth's social login completion for a Slack callback.

    Args:
        email: Email Slack reports (Slack emails are trusted as verified).
        uid: Slack user id.

    Returns:
        The request (to inspect ``request.user``) and the response.
    """
    request = _request()
    with context.request_context(request):
        provider = get_adapter(request).get_provider(request, "slack")
        sociallogin = SocialLogin(
            user=User(email=email, username=""),
            account=SocialAccount(provider="slack", uid=uid, extra_data={}),
            email_addresses=[EmailAddress(email=email, verified=True, primary=True)],
            provider=provider,
        )
        response = complete_social_login(request, sociallogin)
    return request, response


def _passkey_account(email: str, *, verified: bool) -> User:
    """Create a passkey-only account like the passkey signup flow does.

    Args:
        email: Address typed at signup.
        verified: Whether the address was confirmed afterwards.

    Returns:
        The created user.
    """
    user = User.objects.create_user(username="squatter", email=email)
    WebAuthnCredential.objects.create(
        user=user,
        credential_id="attacker-cred",
        public_key="pk",
        sign_count=0,
        name="Signup Passkey",
    )
    EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    return user


@pytest.mark.django_db
def test_slack_login_does_not_hijack_unverified_passkey_account() -> None:
    """The owner's Slack login must not sign into an unverified squatted account."""
    squatter = _passkey_account(EMAIL, verified=False)

    request, _ = _complete()

    assert request.user != squatter
    assert not SocialAccount.objects.filter(user=squatter).exists()
    assert "_auth_user_id" not in request.session


@pytest.mark.django_db
def test_slack_login_unverified_match_case_insensitive() -> None:
    """A differently-cased squatted address is still refused."""
    squatter = _passkey_account("Owner@Example.com", verified=False)

    request, _ = _complete()

    assert request.user != squatter
    assert not SocialAccount.objects.filter(user=squatter).exists()


@pytest.mark.django_db
def test_slack_login_connects_verified_account() -> None:
    """A local account with a verified address is signed in and connected."""
    user = _passkey_account(EMAIL, verified=True)

    request, _ = _complete()

    assert request.user == user
    assert SocialAccount.objects.filter(user=user, provider="slack").exists()


@pytest.mark.django_db
def test_slack_login_existing_slack_account_still_works() -> None:
    """A user already connected to Slack signs in via the social account."""
    user = User.objects.create_user(username="slacker", email=EMAIL)
    EmailAddress.objects.create(user=user, email=EMAIL, verified=True, primary=True)
    SocialAccount.objects.create(user=user, provider="slack", uid="U-OWNER")

    request, _ = _complete()

    assert request.user == user


@pytest.mark.django_db
def test_slack_login_new_user_signs_up() -> None:
    """With no matching account, Slack login creates a verified account."""
    request, _ = _complete(email="new@example.com", uid="U-NEW")

    user = User.objects.get(email="new@example.com")
    assert request.user == user
    assert EmailAddress.objects.filter(
        user=user, email="new@example.com", verified=True
    ).exists()

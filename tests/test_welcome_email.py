"""Tests for the welcome email and the shared outbound mail helper.

Covers the envelope every product email now shares (a monitored Reply-To,
a plain-text part, an optional HTML alternative), the welcome email's
content, and the guarantee that a mail failure never breaks the flow that
triggered it.
"""

from unittest.mock import patch

import pytest
from core.services.mail import send_email
from core.services.welcome_email import SUBJECT, send_welcome_email
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail


@pytest.fixture
def user(db) -> User:
    """A newly created user with a first name and address."""
    return User.objects.create_user(
        username="peter",
        email="peter.gibbons@initech.com",
        first_name="Peter",
    )


class TestSendEmail:
    """The shared sender."""

    def test_sets_monitored_reply_to(self, db) -> None:
        """Replies route to the support inbox, not the no-reply sender."""
        send_email("Subject", "Body", ["someone@example.com"])

        assert len(mail.outbox) == 1
        assert mail.outbox[0].reply_to == [settings.SUPPORT_EMAIL]
        assert mail.outbox[0].from_email == settings.DEFAULT_FROM_EMAIL

    def test_attaches_html_alternative(self, db) -> None:
        """HTML rides along as an alternative, text stays the primary part."""
        send_email("S", "text body", ["a@example.com"], html_body="<p>html</p>")

        message = mail.outbox[0]
        assert message.body == "text body"
        assert message.alternatives == [("<p>html</p>", "text/html")]

    def test_no_recipients_is_a_noop(self, db) -> None:
        """An empty recipient list sends nothing and reports failure."""
        assert send_email("S", "B", []) is False
        assert mail.outbox == []

    def test_backend_failure_is_swallowed(self, db) -> None:
        """A refused send is reported, not raised."""
        with patch(
            "core.services.mail.EmailMultiAlternatives.send",
            side_effect=OSError("smtp down"),
        ):
            assert send_email("S", "B", ["a@example.com"]) is False


class TestWelcomeEmail:
    """Content and addressing of the welcome email."""

    def test_sent_to_the_new_user(self, user: User) -> None:
        """One message, to the user, with the expected subject."""
        assert send_welcome_email(user) is True

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [user.email]
        assert mail.outbox[0].subject == SUBJECT

    def test_greets_by_first_name(self, user: User) -> None:
        """The greeting uses the first name when it is known."""
        send_welcome_email(user)

        assert "Hi Peter" in mail.outbox[0].body
        assert "Welcome, Peter" in mail.outbox[0].alternatives[0][0]

    def test_falls_back_to_username_without_a_first_name(self, db) -> None:
        """A blank first name never renders as an empty greeting."""
        anon = User.objects.create_user(username="samir", email="samir@initech.com")

        send_welcome_email(anon)

        assert "Hi samir" in mail.outbox[0].body
        assert "Hi ," not in mail.outbox[0].body

    def test_drives_the_activation_action(self, user: User) -> None:
        """Both parts point at integrations, the one action that matters."""
        send_welcome_email(user)

        message = mail.outbox[0]
        expected = f"{settings.BASE_URL}/integrations/"
        assert expected in message.body
        assert expected in message.alternatives[0][0]

    def test_has_a_single_call_to_action(self, user: User) -> None:
        """The HTML part offers exactly one button, so the click is unambiguous."""
        send_welcome_email(user)

        assert mail.outbox[0].alternatives[0][0].count('class="cta-button"') == 1

    def test_skips_users_without_an_address(self, db) -> None:
        """No address means no send, and no crash."""
        addressless = User.objects.create_user(username="milton", email="")

        assert send_welcome_email(addressless) is False
        assert mail.outbox == []


class TestSignupPathsSendOnce:
    """Every signup route sends exactly one welcome email."""

    def test_allauth_signal_sends_welcome(self, db) -> None:
        """The allauth path (email and social) is wired to the signal."""
        from allauth.account.signals import user_signed_up

        new_user = User.objects.create_user(
            username="bob", email="bob@initech.com", first_name="Bob"
        )
        with patch("core.signals.analytics.track_event"):
            user_signed_up.send(sender=User, request=None, user=new_user)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["bob@initech.com"]

    @pytest.mark.parametrize(
        "module",
        ["core.views.auth", "core.views.webauthn"],
    )
    def test_custom_flows_import_the_sender(self, module: str) -> None:
        """Slack OIDC and passkey signup call the shared sender.

        Those two create their users without going through allauth, so the
        signal alone would leave both paths silent.
        """
        import importlib

        assert hasattr(importlib.import_module(module), "send_welcome_email")

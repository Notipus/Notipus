"""Custom adapters for django-allauth social authentication.

This module provides custom adapters to enhance the social authentication
flow, particularly for Slack OAuth integration.
"""

import logging

from allauth.account.models import EmailAddress
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from django.contrib import messages
from django.contrib.auth.models import AbstractUser

logger = logging.getLogger(__name__)

# Shown when a social login matches an account whose address was never
# verified. Deliberately does not reveal anything beyond what the signup
# form's "email already registered" error would.
UNVERIFIED_ACCOUNT_MESSAGE = (
    "An account with this email address already exists, but the address "
    "has not been verified, so it cannot be signed in to with Slack. If it "
    "is yours, sign in with your passkey and verify your email first."
)


def has_verified_email(user: AbstractUser, email: str) -> bool:
    """Return whether ``user`` has proven ownership of ``email``.

    Only an allauth ``EmailAddress`` row marked verified counts: the plain
    ``User.email`` field is whatever was typed at signup.

    Args:
        user: The local account.
        email: The address to check (compared case-insensitively).

    Returns:
        True if the address is verified for this user.
    """
    return bool(
        EmailAddress.objects.filter(
            user=user, email__iexact=email, verified=True
        ).exists()
    )


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom adapter for social account authentication.

    This adapter automatically generates usernames from email addresses
    when users sign up via social providers like Slack, enabling a
    seamless signup flow without requiring manual username input.
    """

    def is_auto_signup_allowed(self, request, sociallogin):
        """Determine if auto-signup is allowed for this social login.

        Always returns True to skip the intermediate signup form,
        since we auto-generate usernames from email addresses.

        Args:
            request: The HTTP request object.
            sociallogin: The social login object.

        Returns:
            True to allow auto-signup without showing the form.
        """
        return True

    def authenticate_by_email(
        self, sociallogin: SocialLogin
    ) -> tuple[AbstractUser, str] | None:
        """Match a social login to a local account only by a verified email.

        With ``SOCIALACCOUNT_EMAIL_AUTHENTICATION`` on, allauth signs the
        social user into whichever local account carries the same address,
        even when that account never verified it. Anyone could then pre-create
        an account (e.g. a passkey signup) with a victim's address and wait:
        the victim's first Slack sign-in would land in, and auto-connect to,
        the attacker's account while the attacker's passkey keeps working.

        Requiring the local address to be verified closes that. A verified
        account (including every Slack-created one, whose address allauth
        stores as verified) still signs in as before.

        Args:
            sociallogin: The in-progress social login.

        Returns:
            ``(user, email)`` for a verified match, otherwise None.
        """
        match: tuple[AbstractUser, str] | None = super().authenticate_by_email(
            sociallogin
        )
        if match is None:
            return None
        user, email = match
        if has_verified_email(user, email):
            return match
        logger.warning(
            "Refused social email authentication into account %s: "
            "email address not verified",
            user.pk,
        )
        if self.request is not None:
            try:
                messages.warning(self.request, UNVERIFIED_ACCOUNT_MESSAGE)
            except Exception:  # pragma: no cover - messages middleware absent
                logger.debug("Could not add unverified-account message")
        return None

    def populate_user(self, request, sociallogin, data):
        """Populate user instance with data from social provider.

        Args:
            request: The HTTP request object.
            sociallogin: The social login object containing provider data.
            data: Dictionary of user data from the social provider.

        Returns:
            User instance populated with social provider data.
        """
        user = super().populate_user(request, sociallogin, data)

        # Generate username from email if not already set
        if not user.username and user.email:
            base_username = user.email.split("@")[0]
            # Clean the username to remove invalid characters
            base_username = self._clean_username(base_username)
            user.username = self._generate_unique_username(base_username)

        return user

    def _clean_username(self, username: str) -> str:
        """Clean username to contain only valid characters.

        Args:
            username: The raw username string.

        Returns:
            Cleaned username with only alphanumeric characters and underscores.
        """
        import re

        # Keep only alphanumeric characters, underscores, and hyphens
        cleaned = re.sub(r"[^\w\-]", "", username)
        # Ensure it's not empty
        return cleaned or "user"

    def _generate_unique_username(self, base: str) -> str:
        """Generate a unique username based on a base string.

        Args:
            base: The base username to start from.

        Returns:
            A unique username that doesn't exist in the database.
        """
        from django.contrib.auth import get_user_model

        User = get_user_model()
        username = base
        counter = 1

        while User.objects.filter(username=username).exists():
            username = f"{base}{counter}"
            counter += 1

        return username

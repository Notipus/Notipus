"""WebAuthn service for handling passkey authentication.

This service provides secure passwordless authentication using WebAuthn/FIDO2
standards for user registration and login flows.
"""

import base64
import hashlib
import json
import logging
from datetime import timedelta
from typing import Any, cast

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..models import WebAuthnChallenge, WebAuthnCredential

logger = logging.getLogger(__name__)


class WebAuthnService:
    """Service for handling WebAuthn operations.

    Provides methods for generating registration and authentication
    options, and verifying credentials.

    Attributes:
        rp_id: Relying Party identifier (domain).
        rp_name: Human-readable Relying Party name.
        origin: Expected origin URL for verification.
    """

    def __init__(self) -> None:
        """Initialize WebAuthn service with configuration."""
        self.rp_id = self._get_rp_id()
        self.rp_name = "Notipus"
        self.origin = self._get_origin()

    def _get_rp_id(self) -> str:
        """Get the Relying Party ID from settings or environment.

        Returns:
            Relying Party ID string (domain name).
        """
        # In production, this should be your domain (e.g., "notipus.com")
        # In development, use localhost
        if hasattr(settings, "WEBAUTHN_RP_ID"):
            return cast(str, settings.WEBAUTHN_RP_ID)

        if settings.DEBUG:
            return "localhost"
        else:
            # Extract from ALLOWED_HOSTS in production
            allowed_hosts: list[str] = getattr(settings, "ALLOWED_HOSTS", [])
            for host in allowed_hosts:
                if host not in ["*", "localhost", "127.0.0.1"]:
                    return host
            return "notipus.com"  # Fallback

    def _get_origin(self) -> str:
        """Get the origin URL for WebAuthn operations.

        Returns:
            Origin URL string.
        """
        if hasattr(settings, "WEBAUTHN_ORIGIN"):
            return cast(str, settings.WEBAUTHN_ORIGIN)

        if settings.DEBUG:
            return "http://localhost:8000"
        else:
            return f"https://{self.rp_id}"

    def generate_registration_options(self, user: User) -> dict[str, Any]:
        """Generate registration options for a new WebAuthn credential.

        Args:
            user: Django User instance.

        Returns:
            Dictionary containing registration options for the client.

        Raises:
            Exception: If option generation fails.
        """
        try:
            # Get existing credentials to exclude them
            existing_credentials = self._get_user_credentials(user)

            # Generate registration options
            options = generate_registration_options(
                rp_id=self.rp_id,
                rp_name=self.rp_name,
                user_id=str(user.pk).encode(),
                user_name=user.username,
                user_display_name=user.get_full_name() or user.username,
                exclude_credentials=existing_credentials,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                    resident_key=ResidentKeyRequirement.PREFERRED,
                    user_verification=UserVerificationRequirement.PREFERRED,
                ),
                supported_pub_key_algs=[
                    COSEAlgorithmIdentifier.ECDSA_SHA_256,
                    COSEAlgorithmIdentifier.RSASSA_PSS_SHA_256,
                ],
            )

            # Store challenge for verification
            challenge_str = base64.urlsafe_b64encode(options.challenge).decode("utf-8")
            WebAuthnChallenge.objects.create(
                challenge=challenge_str, user=user, challenge_type="registration"
            )

            # Convert to JSON-serializable format
            return cast("dict[str, Any]", json.loads(options_to_json(options)))

        except Exception as e:
            logger.error(f"Error generating registration options: {e}")
            raise

    def verify_registration(
        self,
        user: User,
        credential_data: dict[str, Any],
        credential_name: str = "Passkey",
    ) -> bool:
        """Verify and store a new WebAuthn credential.

        Args:
            user: Django User instance.
            credential_data: Registration response from client.
            credential_name: User-friendly name for the credential.

        Returns:
            True if verification successful, False otherwise.
        """
        try:
            # Get and validate challenge
            challenge_str = credential_data.get("challenge")
            if not challenge_str:
                logger.error("No challenge in credential data")
                return False

            challenge = WebAuthnChallenge.objects.get(
                challenge=challenge_str, user=user, challenge_type="registration"
            )

            # Verify the registration response
            verification = verify_registration_response(
                credential=credential_data,
                expected_challenge=base64.urlsafe_b64decode(challenge.challenge),
                expected_origin=self.origin,
                expected_rp_id=self.rp_id,
            )

            # verify_registration_response raises on failure,
            # so reaching here means verification succeeded
            # Store the credential
            WebAuthnCredential.objects.create(
                user=user,
                credential_id=base64.urlsafe_b64encode(
                    verification.credential_id
                ).decode(),
                public_key=base64.urlsafe_b64encode(
                    verification.credential_public_key
                ).decode(),
                sign_count=verification.sign_count,
                name=credential_name,
            )

            # Clean up challenge
            challenge.delete()

            logger.info(f"WebAuthn credential registered for user {user.username}")
            return True

        except WebAuthnChallenge.DoesNotExist:
            logger.error("Invalid or expired challenge")
            return False
        except Exception as e:
            logger.error(f"Error verifying registration: {e}")
            return False

    def generate_authentication_options(
        self, username: str | None = None
    ) -> dict[str, Any]:
        """Generate authentication options for WebAuthn login.

        Args:
            username: Optional username to filter credentials.

        Returns:
            Dictionary containing authentication options for the client.

        Raises:
            Exception: If option generation fails.
        """
        try:
            # Get allowed credentials
            allowed_credentials: list[PublicKeyCredentialDescriptor] = []
            if username:
                try:
                    user = User.objects.get(username=username)
                    allowed_credentials = self._get_user_credentials(user)
                except User.DoesNotExist:
                    # Don't reveal if user exists - return empty credentials
                    pass

            # Generate authentication options
            options = generate_authentication_options(
                rp_id=self.rp_id,
                allow_credentials=allowed_credentials,
                user_verification=UserVerificationRequirement.PREFERRED,
            )

            # Store challenge for verification
            challenge_str = base64.urlsafe_b64encode(options.challenge).decode("utf-8")
            WebAuthnChallenge.objects.create(
                challenge=challenge_str,
                user=None,  # No specific user for authentication challenges
                challenge_type="authentication",
            )

            # Convert to JSON-serializable format
            return cast("dict[str, Any]", json.loads(options_to_json(options)))

        except Exception as e:
            logger.error(f"Error generating authentication options: {e}")
            raise

    def verify_authentication(self, credential_data: dict[str, Any]) -> User | None:
        """Verify WebAuthn authentication and return the authenticated user.

        Args:
            credential_data: Authentication response from client.

        Returns:
            User instance if authentication successful, None otherwise.
        """
        try:
            # Get and validate challenge
            challenge_str = credential_data.get("challenge")
            if not challenge_str:
                logger.error("No challenge in credential data")
                return None

            challenge = WebAuthnChallenge.objects.get(
                challenge=challenge_str, challenge_type="authentication"
            )

            # Find the credential
            credential_id = credential_data.get("id")
            if not credential_id:
                logger.error("No credential ID in authentication data")
                return None

            try:
                stored_credential = WebAuthnCredential.objects.get(
                    credential_id=credential_id
                )
            except WebAuthnCredential.DoesNotExist:
                logger.error("Unknown credential ID")
                return None

            # Verify the authentication response
            verification = verify_authentication_response(
                credential=credential_data,
                expected_challenge=base64.urlsafe_b64decode(challenge.challenge),
                expected_origin=self.origin,
                expected_rp_id=self.rp_id,
                credential_public_key=base64.urlsafe_b64decode(
                    stored_credential.public_key
                ),
                credential_current_sign_count=stored_credential.sign_count,
            )

            # verify_authentication_response raises on failure,
            # so reaching here means verification succeeded
            # Update sign count and last used
            stored_credential.sign_count = verification.new_sign_count
            stored_credential.last_used = timezone.now()
            stored_credential.save()

            # Clean up challenge
            challenge.delete()

            logger.info(
                f"WebAuthn authentication successful for user "
                f"{stored_credential.user.username}"
            )
            return cast(User, stored_credential.user)

        except WebAuthnChallenge.DoesNotExist:
            logger.error("Invalid or expired challenge")
            return None
        except Exception as e:
            logger.error(f"Error verifying authentication: {e}")
            return None

    def generate_signup_registration_options(
        self, username: str, email: str
    ) -> dict[str, Any]:
        """Generate registration options for WebAuthn signup flow.

        This method generates registration options for users who don't exist yet
        during the signup process.

        Args:
            username: The desired username for the new account.
            email: The email address for the new account.

        Returns:
            Dictionary containing registration options for the client.

        Raises:
            Exception: If option generation fails.
        """
        try:
            # Generate a temporary user ID for the registration
            # We'll use a hash of username + email to ensure uniqueness
            temp_user_id = hashlib.sha256(f"{username}:{email}".encode()).hexdigest()[
                :16
            ]

            # Generate registration options
            options = generate_registration_options(
                rp_id=self.rp_id,
                rp_name=self.rp_name,
                user_id=temp_user_id.encode(),
                user_name=username,
                user_display_name=username,  # Use username as display name for signup
                exclude_credentials=[],  # No existing credentials for new users
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                    resident_key=ResidentKeyRequirement.PREFERRED,
                    user_verification=UserVerificationRequirement.PREFERRED,
                ),
                supported_pub_key_algs=[
                    COSEAlgorithmIdentifier.ECDSA_SHA_256,
                    COSEAlgorithmIdentifier.RSASSA_PSS_SHA_256,
                ],
            )

            # Store challenge for verification with signup context
            challenge_str = base64.urlsafe_b64encode(options.challenge).decode("utf-8")
            WebAuthnChallenge.objects.create(
                challenge=challenge_str,
                user=None,  # No user yet, this is for signup
                challenge_type="signup_registration",
            )

            # Convert to JSON-serializable format
            return cast("dict[str, Any]", json.loads(options_to_json(options)))

        except Exception as e:
            logger.error(f"Error generating signup registration options: {e}")
            raise

    def complete_signup_registration(
        self, credential_data: dict[str, Any], username: str, email: str
    ) -> User | None:
        """Complete WebAuthn registration and create the user account.

        Args:
            credential_data: Registration response from client.
            username: The username for the new account.
            email: The email address for the new account.

        Returns:
            Created User instance if successful, None otherwise.
        """
        try:
            # Get and validate challenge
            challenge_str = credential_data.get("challenge")
            if not challenge_str:
                logger.error("No challenge in credential data")
                return None

            # Find challenge for signup registration
            challenge = WebAuthnChallenge.objects.get(
                challenge=challenge_str,
                user=None,  # Signup challenges have no user
                challenge_type="signup_registration",
            )

            # Verify the registration response
            verification = verify_registration_response(
                credential=credential_data,
                expected_challenge=base64.urlsafe_b64decode(challenge.challenge),
                expected_origin=self.origin,
                expected_rp_id=self.rp_id,
            )

            # verify_registration_response raises on failure,
            # so reaching here means verification succeeded
            # Create the user account atomically with the WebAuthn credential
            with transaction.atomic():
                # Create user without password (passwordless account)
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=None,  # No password for passkey-only accounts
                )

                # Store the WebAuthn credential
                WebAuthnCredential.objects.create(
                    user=user,
                    credential_id=base64.urlsafe_b64encode(
                        verification.credential_id
                    ).decode(),
                    public_key=base64.urlsafe_b64encode(
                        verification.credential_public_key
                    ).decode(),
                    sign_count=verification.sign_count,
                    name="Signup Passkey",
                )

                # Clean up challenge
                challenge.delete()

            logger.info(f"WebAuthn signup completed for user {username}")
            return user

        except WebAuthnChallenge.DoesNotExist:
            logger.error("Challenge not found for signup registration")
            return None
        except Exception as e:
            logger.error(f"Error during signup registration: {e}")
            return None

    def _get_user_credentials(self, user: User) -> list[PublicKeyCredentialDescriptor]:
        """Get existing credentials for a user as PublicKeyCredentialDescriptor objects.

        Args:
            user: Django User instance.

        Returns:
            List of credential descriptors.
        """
        credentials: list[PublicKeyCredentialDescriptor] = []
        for cred in WebAuthnCredential.objects.filter(user=user):
            credentials.append(
                PublicKeyCredentialDescriptor(
                    id=base64.urlsafe_b64decode(cred.credential_id)
                )
            )
        return credentials

    def cleanup_expired_challenges(self, hours: int = 1) -> int:
        """Clean up expired WebAuthn challenges.

        Args:
            hours: Hours after which challenges are considered expired.

        Returns:
            Number of challenges cleaned up.
        """
        cutoff_time = timezone.now() - timedelta(hours=hours)
        count, _ = WebAuthnChallenge.objects.filter(created_at__lt=cutoff_time).delete()

        if count > 0:
            logger.info(f"Cleaned up {count} expired WebAuthn challenges")

        return count

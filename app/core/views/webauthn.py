"""WebAuthn/Passkey authentication views.

This module handles passwordless authentication using WebAuthn/FIDO2.
"""

import json
import logging
from typing import Any, cast

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..models import WebAuthnCredential
from ..services.webauthn import WebAuthnService

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["POST"])
def webauthn_register_begin(request: HttpRequest) -> JsonResponse:
    """Start WebAuthn registration flow for adding a passkey.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with registration options or error.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)

    try:
        webauthn_service = WebAuthnService()
        options = webauthn_service.generate_registration_options(
            cast(User, request.user)
        )
        return JsonResponse({"success": True, "options": options})
    except Exception as e:
        logger.error(f"WebAuthn registration begin error: {e}")
        return JsonResponse({"error": "Failed to start registration"}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def webauthn_register_complete(request: HttpRequest) -> JsonResponse:
    """Complete WebAuthn registration and store the credential.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with success status or error.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)

    try:
        data: dict[str, Any] = json.loads(request.body)
        credential_data = data.get("credential")
        credential_name = data.get("name", "Passkey")

        if not credential_data:
            return JsonResponse({"error": "Missing credential data"}, status=400)

        webauthn_service = WebAuthnService()
        success = webauthn_service.verify_registration(
            cast(User, request.user), credential_data, credential_name
        )

        if success:
            return JsonResponse(
                {"success": True, "message": "Passkey registered successfully"}
            )
        else:
            return JsonResponse(
                {"error": "Registration verification failed"}, status=400
            )

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"WebAuthn registration complete error: {e}")
        return JsonResponse({"error": "Failed to complete registration"}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def webauthn_authenticate_begin(request: HttpRequest) -> JsonResponse:
    """Start WebAuthn authentication flow for passkey login.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with authentication options or error.
    """
    try:
        data: dict[str, Any] = json.loads(request.body)
        username = data.get("username")  # Optional for usernameless flow

        webauthn_service = WebAuthnService()
        options = webauthn_service.generate_authentication_options(username)
        return JsonResponse({"success": True, "options": options})

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"WebAuthn authentication begin error: {e}")
        return JsonResponse({"error": "Failed to start authentication"}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def webauthn_authenticate_complete(request: HttpRequest) -> JsonResponse:
    """Complete WebAuthn authentication and log the user in.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with success status and redirect URL, or error.
    """
    try:
        data: dict[str, Any] = json.loads(request.body)
        credential_data = data.get("credential")

        if not credential_data:
            return JsonResponse({"error": "Missing credential data"}, status=400)

        webauthn_service = WebAuthnService()
        user = webauthn_service.verify_authentication(credential_data)

        if user:
            # Log the user in
            login(request, user)
            return JsonResponse(
                {
                    "success": True,
                    "message": "Authentication successful",
                    "redirect_url": "/dashboard/",
                }
            )
        else:
            return JsonResponse(
                {"error": "Authentication verification failed"}, status=401
            )

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"WebAuthn authentication complete error: {e}")
        return JsonResponse({"error": "Failed to complete authentication"}, status=500)


@login_required
def webauthn_credentials(request: HttpRequest) -> JsonResponse:
    """View and manage user's WebAuthn credentials.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with credentials list or operation result.
    """
    if request.method == "GET":
        # Return user's existing credentials
        credentials = WebAuthnCredential.objects.filter(user=request.user).values(
            "id", "name", "created_at", "last_used"
        )
        return JsonResponse({"credentials": list(credentials)})

    elif request.method == "DELETE":
        # Delete a specific credential
        try:
            data: dict[str, Any] = json.loads(request.body)
            credential_id = data.get("credential_id")

            if not credential_id:
                return JsonResponse({"error": "Missing credential_id"}, status=400)

            WebAuthnCredential.objects.filter(
                id=credential_id, user=request.user
            ).delete()

            return JsonResponse({"success": True, "message": "Credential deleted"})

        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON data"}, status=400)
        except Exception as e:
            logger.error(f"WebAuthn credential deletion error: {e}")
            return JsonResponse({"error": "Failed to delete credential"}, status=500)

    return JsonResponse({"error": "Method not allowed"}, status=405)


@csrf_exempt
@require_http_methods(["POST"])
def webauthn_signup_begin(request: HttpRequest) -> JsonResponse:
    """Start WebAuthn registration flow for passwordless signup.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with registration options or error.
    """
    try:
        data: dict[str, Any] = json.loads(request.body)
        username = data.get("username", "").strip()
        email = data.get("email", "").strip()

        if not username or not email:
            return JsonResponse(
                {"error": "Username and email are required"}, status=400
            )

        # Check if username or email already exists
        if User.objects.filter(username=username).exists():
            return JsonResponse({"error": "Username already exists"}, status=400)

        if User.objects.filter(email=email).exists():
            return JsonResponse({"error": "Email already exists"}, status=400)

        webauthn_service = WebAuthnService()
        options = webauthn_service.generate_signup_registration_options(username, email)
        return JsonResponse({"success": True, "options": options})

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"WebAuthn signup begin error: {e}")
        return JsonResponse({"error": "Failed to start registration"}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def webauthn_signup_complete(request: HttpRequest) -> JsonResponse:
    """Complete WebAuthn registration and create user account.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with success status and redirect URL, or error.
    """
    try:
        data: dict[str, Any] = json.loads(request.body)
        credential_data = data.get("credential")
        username = data.get("username", "").strip()
        email = data.get("email", "").strip()

        if not credential_data or not username or not email:
            return JsonResponse({"error": "Missing required data"}, status=400)

        webauthn_service = WebAuthnService()
        user = webauthn_service.complete_signup_registration(
            credential_data, username, email
        )

        if user:
            # Log the user in
            login(request, user)

            return JsonResponse(
                {
                    "success": True,
                    "message": "Account created successfully with passkey",
                    "redirect_url": "/dashboard/",
                }
            )
        else:
            return JsonResponse(
                {"error": "Registration verification failed"}, status=400
            )

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"WebAuthn signup complete error: {e}")
        return JsonResponse({"error": "Failed to complete registration"}, status=500)

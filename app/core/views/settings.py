"""Notification settings views.

This module handles notification preference management.
"""

import json
import logging
from typing import Any, cast

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse

from ..models import NotificationSettings, UserProfile, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)

# The only fields the update API may touch. Anything else — including
# non-notify model fields like workspace_id — is rejected with a 400
# instead of silently ignored, so client typos surface immediately.
ALLOWED_NOTIFICATION_FIELDS: frozenset[str] = frozenset(
    {
        "notify_payment_success",
        "notify_payment_failure",
        "notify_subscription_created",
        "notify_subscription_updated",
        "notify_subscription_canceled",
        "notify_trial_ending",
        "notify_trial_expired",
        "notify_customer_updated",
        "notify_signups",
        "notify_shopify_order_created",
        "notify_shopify_order_updated",
        "notify_shopify_order_paid",
    }
)


def _get_user_workspace(request: HttpRequest) -> "Workspace":
    """Resolve the workspace for the requesting user.

    Tries WorkspaceMember first, falling back to the legacy UserProfile.

    Args:
        request: The HTTP request object.

    Returns:
        The user's workspace.

    Raises:
        UserProfile.DoesNotExist: If the user has neither membership nor
            profile.
    """
    member = WorkspaceMember.objects.filter(user=request.user, is_active=True).first()
    if member:
        return cast(Workspace, member.workspace)
    user_profile = UserProfile.objects.get(user=request.user)
    return cast(Workspace, user_profile.workspace)


@login_required
def get_notification_settings(request: HttpRequest) -> JsonResponse:
    """Get notification settings for the user's workspace.

    Settings are created with defaults if missing (normally the
    post_save signal creates them with the workspace).

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with notification settings or error.
    """
    try:
        workspace = _get_user_workspace(request)

        settings_obj, _created = NotificationSettings.objects.get_or_create(
            workspace=workspace
        )

        settings_data: dict[str, bool] = {
            field: getattr(settings_obj, field)
            for field in sorted(ALLOWED_NOTIFICATION_FIELDS)
        }

        return JsonResponse(settings_data)

    except UserProfile.DoesNotExist:
        return JsonResponse({"error": "User profile not found"}, status=404)
    except Exception:
        logger.error("Error retrieving notification settings", exc_info=True)
        return JsonResponse({"error": "Internal server error"}, status=500)


def _validate_settings_payload(data: Any) -> JsonResponse | None:
    """Validate an update payload before anything is applied.

    Args:
        data: Decoded JSON body of the update request.

    Returns:
        A 400 JsonResponse describing the first problem, or None when the
        payload is a well-formed object of known boolean fields.
    """
    # Valid JSON that isn't an object (list/number/string) must be a
    # 400, not an AttributeError-turned-500 at data.items().
    if not isinstance(data, dict):
        return JsonResponse({"error": "Request body must be a JSON object"}, status=400)

    for field, value in data.items():
        if field not in ALLOWED_NOTIFICATION_FIELDS:
            return JsonResponse(
                {"error": f"Field '{field}' is not allowed to be updated"},
                status=400,
            )
        if not isinstance(value, bool):
            return JsonResponse(
                {"error": f"Field '{field}' must be a boolean value"},
                status=400,
            )

    return None


@login_required
def update_notification_settings(request: HttpRequest) -> JsonResponse:
    """Update notification settings for the user's workspace.

    Validates the full payload before applying anything: unknown fields
    and non-boolean values are 400s, so a partially-misspelled payload
    never half-applies.

    Args:
        request: The HTTP request object.

    Returns:
        JSON response with the list of updated fields or error.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        workspace = _get_user_workspace(request)

        data: Any = json.loads(request.body)
        error_response = _validate_settings_payload(data)
        if error_response is not None:
            return error_response

        settings_obj, _created = NotificationSettings.objects.get_or_create(
            workspace=workspace
        )

        updated_fields: list[str] = []
        for field, value in data.items():
            setattr(settings_obj, field, value)
            updated_fields.append(field)

        if updated_fields:
            settings_obj.save(update_fields=updated_fields)

        return JsonResponse({"status": "success", "updated_fields": updated_fields})

    except UserProfile.DoesNotExist:
        return JsonResponse({"error": "User profile not found"}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON data"}, status=400)
    except Exception:
        logger.error("Error updating notification settings", exc_info=True)
        return JsonResponse({"error": "Internal server error"}, status=500)

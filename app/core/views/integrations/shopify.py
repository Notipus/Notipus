"""Shopify OAuth integration views.

Handles Shopify OAuth 2.0 flow for receiving order and customer webhooks.
Similar to Stripe Connect, this automatically creates webhook subscriptions
after successful OAuth authorization.
"""

import hashlib
import hmac
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render

from ...models import Integration
from .base import (
    DEFAULT_API_TIMEOUT,
    require_admin_role,
    require_post_method,
    require_workspace,
)

logger = logging.getLogger(__name__)

# Integration metadata
INTEGRATION_TYPE = "shopify"
DISPLAY_NAME = "Shopify"

# Shopify event categories with their webhook topics
# Used for configurable webhook subscriptions
SHOPIFY_EVENT_CATEGORIES: dict[str, dict[str, str | list[str] | bool]] = {
    "orders": {
        "label": "Orders",
        "description": "New orders and payment events",
        "topics": ["orders/create", "orders/paid", "orders/cancelled"],
        "default": True,
    },
    "refunds": {
        "label": "Refunds",
        "description": "Full and partial refunds",
        # refunds/create is the only refund topic Shopify has, and it
        # covers partial refunds as well as full ones.
        "topics": ["refunds/create"],
        "default": True,
    },
    # Hidden, because it cannot deliver. These topics need
    # read_own_subscription_contracts, which Shopify gates behind an
    # access request and which only covers contracts belonging to the
    # requesting app. Notipus creates none - merchants run subscriptions
    # through Recharge, Shopify Subscriptions and similar - so the scope
    # would grant nothing, and Shopify documents no scope for reading
    # another app's contracts. The topics are not declared in
    # shopify.app.toml for the same reason.
    #
    # Offering it would advertise something that returns nothing: a
    # merchant ticks the box and never hears about a subscription again.
    #
    # The entry stays because it is still the mapping from these topics to
    # a category - the parsing is written and tested, so making them
    # available again means deleting one flag, not a rewrite.
    "subscriptions": {
        "label": "Subscriptions",
        "description": "Subscription contracts and recurring billing",
        "topics": [
            "subscription_contracts/create",
            "subscription_contracts/update",
            "subscription_billing_attempts/success",
            "subscription_billing_attempts/failure",
            # Payment needs customer authentication (SCA) to complete.
            "subscription_billing_attempts/challenged",
        ],
        "default": False,
        "hidden": True,
    },
    "fulfillment": {
        "label": "Fulfillment",
        "description": "Shipping and delivery updates",
        "topics": ["orders/fulfilled", "fulfillments/create", "fulfillments/update"],
        "default": True,
    },
    "customers": {
        "label": "Customers",
        "description": "Customer profile updates",
        "topics": ["customers/update"],
        "default": True,
    },
}

# All available webhook topics (for backward compatibility)
SHOPIFY_WEBHOOK_TOPICS: list[str] = [
    topic
    for category in SHOPIFY_EVENT_CATEGORIES.values()
    if isinstance(category["topics"], list)
    for topic in category["topics"]
]


def _get_topics_for_categories(enabled_categories: list[str]) -> list[str]:
    """Get webhook topics for the given enabled categories.

    Args:
        enabled_categories: List of category keys to enable.

    Returns:
        List of webhook topic strings.
    """
    topics: list[str] = []
    for category_key in enabled_categories:
        if category_key in SHOPIFY_EVENT_CATEGORIES:
            category_topics = SHOPIFY_EVENT_CATEGORIES[category_key]["topics"]
            if isinstance(category_topics, list):
                topics.extend(category_topics)
    return topics


def _selectable_categories() -> dict[str, dict[str, str | list[str] | bool]]:
    """Return the categories a merchant may actually choose.

    Hidden categories still map their topics, but are neither offered nor
    accepted: a category that can never deliver is a promise the app does
    not keep.

    Returns:
        The category config minus anything marked hidden.
    """
    return {
        key: config
        for key, config in SHOPIFY_EVENT_CATEGORIES.items()
        if not config.get("hidden")
    }


def _get_default_categories() -> list[str]:
    """Get list of category keys that are enabled by default.

    Returns:
        List of default category keys.
    """
    return [
        key for key, config in SHOPIFY_EVENT_CATEGORIES.items() if config.get("default")
    ]


@login_required
def integrate_shopify(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Shopify integration setup page.

    Shows a form for users to enter their shop URL and initiate OAuth flow.
    If already connected, shows the connected status.

    Args:
        request: The HTTP request object.

    Returns:
        Shopify integration page or redirect to workspace creation.
    """
    workspace, redirect_response = require_workspace(request)
    if redirect_response:
        return redirect_response

    # Check for existing integration
    existing_integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    # Get enabled categories for connected integrations
    enabled_categories = []
    if existing_integration:
        enabled_categories = existing_integration.integration_settings.get(
            "enabled_categories", _get_default_categories()
        )

    context = {
        "workspace": workspace,
        "integration": existing_integration,
        "shopify_configured": bool(settings.SHOPIFY_CLIENT_ID),
        "event_categories": _selectable_categories(),
        "enabled_categories": enabled_categories,
    }
    return render(request, "core/integrate_shopify.html.j2", context)


@login_required
def shopify_connect(request: HttpRequest) -> HttpResponseRedirect:
    """Start Shopify OAuth flow.

    Accepts shop URL via POST, validates it, stores state in session,
    and redirects to Shopify OAuth authorization page.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to Shopify OAuth or integrations page on error.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    # Require admin role for integration modifications
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    if not settings.SHOPIFY_CLIENT_ID:
        logger.error("SHOPIFY_CLIENT_ID not configured")
        messages.error(
            request, "Shopify integration is not configured. Please contact support."
        )
        return redirect("core:integrations")

    # Get and validate shop URL from POST data
    shop_url = request.POST.get("shop_url", "").strip()
    if not shop_url:
        messages.error(request, "Please enter your Shopify store URL")
        return redirect("core:integrate_shopify")

    # Normalize shop URL to myshopify.com domain
    shop_domain, error_message = _normalize_shop_domain(shop_url)
    if error_message:
        messages.error(request, error_message)
        return redirect("core:integrate_shopify")

    # Validate the shop domain format (security check)
    if not shop_domain or not _is_valid_shop_domain(shop_domain):
        messages.error(request, "Invalid Shopify store URL format")
        return redirect("core:integrate_shopify")

    # Get selected event categories and validate against known categories
    raw_categories = request.POST.getlist("event_categories")
    valid_category_keys = set(_selectable_categories())
    selected_categories = [c for c in raw_categories if c in valid_category_keys]

    # Default to all categories if none selected or all were invalid
    if not selected_categories:
        selected_categories = _get_default_categories()

    # Generate state parameter for CSRF protection
    state = secrets.token_urlsafe(32)

    # Store state, shop, and categories in session for callback verification
    request.session["shopify_oauth_state"] = state
    request.session["shopify_shop_domain"] = shop_domain
    request.session["shopify_event_categories"] = selected_categories

    # Build OAuth authorization URL
    # https://shopify.dev/docs/apps/auth/oauth/getting-started
    auth_params = {
        "client_id": settings.SHOPIFY_CLIENT_ID,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": settings.SHOPIFY_REDIRECT_URI,
        "state": state,
    }

    # Defense-in-depth: re-validate domain before constructing redirect URL
    if not _is_valid_shop_domain(shop_domain):
        messages.error(request, "Invalid Shopify store URL format")
        return redirect("core:integrate_shopify")

    auth_url = f"https://{shop_domain}/admin/oauth/authorize?{urlencode(auth_params)}"

    logger.info(f"Redirecting to Shopify OAuth for shop: {shop_domain}")
    return redirect(auth_url)


def shopify_connect_callback(
    request: HttpRequest,
) -> HttpResponse | HttpResponseRedirect:
    """Handle Shopify OAuth callback.

    Validates state parameter, exchanges authorization code for access token,
    creates webhook subscriptions, and stores the integration.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to integrations page on success, error response on failure.
    """
    # Get callback parameters
    code = request.GET.get("code")
    state = request.GET.get("state")
    shop = request.GET.get("shop")
    error = request.GET.get("error")
    error_description = request.GET.get("error_description")

    # Handle OAuth errors
    if error:
        logger.error(f"Shopify OAuth error: {error} - {error_description}")
        error_msg = error_description or error
        messages.error(request, f"Shopify connection failed: {error_msg}")
        return redirect("core:integrations")

    # Validate required parameters
    if not code or not state or not shop:
        messages.error(request, "Invalid OAuth callback: missing parameters")
        return redirect("core:integrations")

    # Validate state parameter (CSRF protection)
    stored_state = request.session.get("shopify_oauth_state")
    if not stored_state or not secrets.compare_digest(state, stored_state):
        logger.error("Shopify OAuth state mismatch - possible CSRF attack")
        messages.error(request, "Invalid OAuth state. Please try again.")
        return redirect("core:integrations")

    # Validate shop matches what we stored
    stored_shop = request.session.get("shopify_shop_domain")
    if not stored_shop or shop != stored_shop:
        logger.error(f"Shopify shop mismatch: expected {stored_shop}, got {shop}")
        messages.error(request, "Shop domain mismatch. Please try again.")
        return redirect("core:integrations")

    # Get enabled categories from session
    enabled_categories = request.session.get(
        "shopify_event_categories", _get_default_categories()
    )

    # Clean up session
    request.session.pop("shopify_oauth_state", None)
    request.session.pop("shopify_shop_domain", None)
    request.session.pop("shopify_event_categories", None)

    # Get user's workspace (require admin role for modifications)
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Validate HMAC signature if present (Shopify may include this)
    hmac_param = request.GET.get("hmac")
    if hmac_param and not _verify_oauth_hmac(request, hmac_param):
        logger.error("Shopify OAuth HMAC verification failed")
        messages.error(request, "OAuth verification failed. Please try again.")
        return redirect("core:integrations")

    # Exchange authorization code for access token. This fails early
    # (before the integration is stored) when SHOPIFY_CLIENT_SECRET is
    # not configured, so an existing integration's webhook_secret is
    # never overwritten with an empty value.
    token_data = _exchange_code_for_token(request, shop, code)
    if token_data is None:
        return redirect("core:integrations")

    if not token_data.get("access_token"):
        logger.error("Missing access_token in Shopify response")
        messages.error(request, "Shopify connection failed: Invalid response")
        return redirect("core:integrations")

    # The token is deliberately discarded here rather than stored.
    #
    # Webhook subscriptions are declared in the app configuration, so
    # Shopify delivers events without Notipus ever calling the Admin
    # API. A stored token would be a standing read capability over the
    # merchant's entire order and customer history - far more access
    # than delivering notifications needs, and the most damaging thing
    # in the database if it ever leaked. The exchange still happens
    # because completing it is what proves the merchant authorised the
    # install; only the resulting credential is thrown away.
    assert workspace is not None
    integration, created = Integration.objects.update_or_create(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        defaults={
            # Events arrive on the app-level endpoint signed with the
            # app's client secret, so no per-shop secret is kept either.
            "webhook_secret": "",
            "oauth_credentials": {},
            "integration_settings": {
                "shop_domain": shop,
                "enabled_categories": enabled_categories,
            },
            "is_active": True,
            "webhook_verified_at": None,
        },
    )

    action = "connected" if created else "reconnected"
    logger.info(f"Shopify {action} for workspace {workspace.name} (shop: {shop})")
    messages.success(
        request,
        f"Shopify {action} successfully! You will now receive order notifications.",
    )
    return redirect("core:integrations")


@login_required
def disconnect_shopify(request: HttpRequest) -> HttpResponseRedirect:
    """Disconnect the Shopify integration.

    Nothing is deleted at Shopify's end because nothing was created
    there: subscriptions belong to the app, not to this shop. Events for
    a disconnected store keep arriving at the app-level endpoint and are
    acknowledged and dropped, and stop entirely once the merchant
    uninstalls the app.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to integrations page.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    # Require admin role for disconnection
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Find the active Shopify integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        messages.warning(request, "No active Shopify integration found")
        return redirect("core:integrations")

    # Deactivate the integration
    integration.is_active = False
    integration.save()

    messages.success(request, "Shopify disconnected successfully!")
    return redirect("core:integrations")


@login_required
def update_shopify_events(request: HttpRequest) -> HttpResponseRedirect:
    """Update Shopify webhook event subscriptions.

    Allows users to change which event categories they want to receive.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to Shopify integration page.
    """
    error_redirect = require_post_method(request)
    if error_redirect:
        return error_redirect

    # Require admin role for modifications
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response

    # Find the active Shopify integration
    integration = Integration.objects.filter(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        is_active=True,
    ).first()

    if not integration:
        messages.error(request, "No active Shopify integration found")
        return redirect("core:integrate_shopify")

    # Get new selected categories and validate against known categories
    raw_categories = request.POST.getlist("event_categories")
    valid_category_keys = set(_selectable_categories())
    new_categories = [c for c in raw_categories if c in valid_category_keys]

    if not new_categories:
        messages.error(request, "Please select at least one valid event category")
        return redirect("core:integrate_shopify")

    old_categories = integration.integration_settings.get(
        "enabled_categories", _get_default_categories()
    )

    assert workspace is not None
    if set(new_categories) == set(old_categories):
        messages.info(request, "No changes to event subscriptions")
        return redirect("core:integrate_shopify")

    # Purely a local preference now. Shopify delivers every topic the app
    # declares to every installed store, and the receiving endpoint drops
    # what this workspace hasn't asked for - so changing categories needs
    # no API call, and cannot half-fail partway through.
    integration.integration_settings["enabled_categories"] = new_categories
    integration.save()

    logger.info(
        f"Updated Shopify event categories for workspace {workspace.name}: "
        f"{old_categories} -> {new_categories}"
    )
    messages.success(request, "Event subscriptions updated successfully!")
    return redirect("core:integrate_shopify")


def _normalize_shop_domain(shop_url: str) -> tuple[str | None, str | None]:
    """Normalize shop URL to myshopify.com domain.

    Accepts various formats:
    - mystore (just the store name)
    - mystore.myshopify.com
    - https://mystore.myshopify.com
    - mystore.myshopify.com/admin

    Rejects custom domains (e.g., shop.mybusiness.com) with an appropriate error.

    Args:
        shop_url: User-provided shop URL.

    Returns:
        Tuple of (normalized_domain, error_message).
        If successful: (domain, None)
        If failed: (None, error_message)
    """
    if not shop_url:
        return None, "Please enter your Shopify store URL"

    # Remove protocol and path, lowercase
    shop = shop_url.lower().strip()
    shop = shop.replace("https://", "").replace("http://", "")
    shop = shop.split("/")[0]  # Remove any path

    # Check if it's a myshopify.com domain
    if shop.endswith(".myshopify.com"):
        # Extract and validate the shop name
        shop_name = shop.replace(".myshopify.com", "")
        if not shop_name or not shop_name.replace("-", "").replace("_", "").isalnum():
            return None, "Invalid store name in URL"
        return shop, None

    # Check if it looks like a custom domain (contains a dot)
    if "." in shop:
        # This is a custom domain like "shop.mybusiness.com"
        return None, (
            "Custom domains are not supported for OAuth. "
            "Please enter your myshopify.com domain instead. "
            "You can find it in Shopify Admin > Settings > Domains."
        )

    # It's just a store name (e.g., "mystore")
    # Validate the shop name
    if not shop or not shop.replace("-", "").replace("_", "").isalnum():
        return None, (
            "Invalid store name. Use only letters, numbers, hyphens, and underscores."
        )

    return f"{shop}.myshopify.com", None


def _is_valid_shop_domain(shop_domain: str) -> bool:
    """Validate shop domain format for security.

    Prevents injection attacks by ensuring the domain follows
    Shopify's shop domain format.

    Args:
        shop_domain: The shop domain to validate.

    Returns:
        True if valid, False otherwise.
    """
    import re

    # Shopify shop domains must match this pattern
    # Only alphanumeric, hyphens, and underscores allowed in shop name
    pattern = r"^[a-zA-Z0-9][a-zA-Z0-9\-_]*\.myshopify\.com$"
    return bool(re.match(pattern, shop_domain))


def _verify_oauth_hmac(request: HttpRequest, hmac_param: str) -> bool:
    """Verify HMAC signature from Shopify OAuth callback.

    Args:
        request: The HTTP request object.
        hmac_param: The HMAC parameter from the callback.

    Returns:
        True if valid or no secret configured, False if invalid.
    """
    if not settings.SHOPIFY_CLIENT_SECRET:
        return True  # Can't verify without secret

    # Build the message from query parameters (excluding hmac)
    params = dict(request.GET.items())
    params.pop("hmac", None)

    # Sort parameters and create message string
    sorted_params = sorted(params.items())
    message = "&".join(f"{k}={v}" for k, v in sorted_params)

    # Calculate HMAC
    calculated_hmac = hmac.new(
        settings.SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated_hmac, hmac_param)


def _exchange_code_for_token(request: HttpRequest, shop: str, code: str) -> dict | None:
    """Exchange authorization code for access token.

    Args:
        request: The HTTP request object.
        shop: The shop domain.
        code: The authorization code from Shopify.

    Returns:
        Token data dict or None if exchange failed (including when
        SHOPIFY_CLIENT_SECRET is not configured).
    """
    # The client secret is required both for the token exchange and as
    # the stored webhook HMAC secret. Treat a missing secret as a failed
    # connect instead of proceeding and persisting an empty
    # webhook_secret (which would break validation of every webhook).
    if not settings.SHOPIFY_CLIENT_SECRET:
        logger.error("SHOPIFY_CLIENT_SECRET not configured")
        messages.error(
            request, "Shopify integration is not configured. Please contact support."
        )
        return None

    if not _is_valid_shop_domain(shop):
        logger.error(f"Invalid shop domain in token exchange: {shop}")
        messages.error(request, "Invalid shop domain. Please try again.")
        return None

    token_url = f"https://{shop}/admin/oauth/access_token"

    try:
        response = requests.post(
            token_url,
            data={
                "client_id": settings.SHOPIFY_CLIENT_ID,
                "client_secret": settings.SHOPIFY_CLIENT_SECRET,
                "code": code,
            },
            timeout=DEFAULT_API_TIMEOUT,
        )
        response.raise_for_status()
        token_data = response.json()
    except requests.exceptions.Timeout:
        logger.error("Shopify OAuth token exchange timed out")
        messages.error(request, "Shopify connection timed out. Please try again.")
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"Shopify OAuth token exchange HTTP error: {e}")
        messages.error(request, "Shopify connection failed. Please try again.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Shopify OAuth request failed: {e!s}")
        messages.error(request, "Shopify connection failed. Please try again.")
        return None

    if "error" in token_data:
        logger.error(f"Shopify token exchange error: {token_data}")
        error_detail = token_data.get("error_description", token_data.get("error"))
        messages.error(request, f"Shopify connection failed: {error_detail}")
        return None

    result: dict[str, Any] = token_data
    return result

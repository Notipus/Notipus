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

from ...models import Integration, Workspace
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
        # refunds/create covers partial refunds too; orders/refunded only
        # fires once a refund settles the entire order.
        "topics": ["refunds/create", "orders/refunded"],
        "default": True,
    },
    # Off by default: these topics need read_own_subscription_contracts,
    # which most stores won't have granted. Enabling them for everyone
    # would fail registration on every non-subscription store.
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
        "event_categories": SHOPIFY_EVENT_CATEGORIES,
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
    valid_category_keys = set(SHOPIFY_EVENT_CATEGORIES.keys())
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

    access_token = token_data.get("access_token")
    scope = token_data.get("scope", "")

    if not access_token:
        logger.error(f"Missing access_token in Shopify response: {token_data}")
        messages.error(request, "Shopify connection failed: Invalid response")
        return redirect("core:integrations")

    # Create webhook subscriptions for enabled categories
    assert workspace is not None
    webhook_result = _create_webhook_subscriptions(
        request, workspace, shop, access_token, enabled_categories
    )
    if webhook_result is None:
        # Still save the integration but warn about webhook issues
        logger.warning(
            f"Webhook creation failed for shop {shop}, saving integration anyway"
        )
        webhook_ids = []
    else:
        webhook_ids = webhook_result

    # Store or update Shopify integration.
    # Webhooks created via the Admin API are signed with the app's client
    # secret, so store it as the webhook secret for HMAC validation.
    # SHOPIFY_CLIENT_SECRET is guaranteed non-empty by the check above.
    integration, created = Integration.objects.update_or_create(
        workspace=workspace,
        integration_type=INTEGRATION_TYPE,
        defaults={
            "webhook_secret": settings.SHOPIFY_CLIENT_SECRET,
            "oauth_credentials": {
                "access_token": access_token,
                "scope": scope,
            },
            "integration_settings": {
                "shop_domain": shop,
                "webhook_ids": webhook_ids,
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
    """Disconnect Shopify integration and delete webhook subscriptions.

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

    # Try to delete webhook subscriptions from Shopify
    _delete_webhook_subscriptions(integration)

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
    valid_category_keys = set(SHOPIFY_EVENT_CATEGORIES.keys())
    new_categories = [c for c in raw_categories if c in valid_category_keys]

    if not new_categories:
        messages.error(request, "Please select at least one valid event category")
        return redirect("core:integrate_shopify")

    # Get current settings
    shop = integration.integration_settings.get("shop_domain")
    access_token = integration.oauth_credentials.get("access_token")
    old_categories = integration.integration_settings.get(
        "enabled_categories", _get_default_categories()
    )

    if not shop or not access_token:
        messages.error(request, "Integration is missing required credentials")
        return redirect("core:integrate_shopify")

    # Update webhooks if categories changed
    assert workspace is not None
    if set(new_categories) != set(old_categories):
        # Create new webhooks first (before deleting old ones) to minimize downtime
        # and avoid losing webhooks if creation fails
        new_webhook_ids = _create_webhook_subscriptions(
            request, workspace, shop, access_token, new_categories
        )

        # Only delete old webhooks after new ones are created successfully
        if new_webhook_ids is not None:
            _delete_webhook_subscriptions(integration)

            # Update integration settings
            integration.integration_settings["enabled_categories"] = new_categories
            integration.integration_settings["webhook_ids"] = new_webhook_ids
            integration.save()

            logger.info(
                f"Updated Shopify event categories for workspace {workspace.name}: "
                f"{old_categories} -> {new_categories}"
            )
            messages.success(request, "Event subscriptions updated successfully!")
        else:
            # Creation failed - keep existing webhooks
            logger.error(
                f"Failed to create new webhooks for workspace {workspace.name}, "
                "keeping existing configuration"
            )
            messages.error(
                request,
                "Failed to update event subscriptions. Please try again.",
            )
    else:
        messages.info(request, "No changes to event subscriptions")

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


def _graphql_topic(rest_topic: str) -> str:
    """Convert a REST webhook topic to its GraphQL enum name.

    Shopify names ``WebhookSubscriptionTopic`` enum values after the REST
    topics with separators replaced by underscores and the whole thing
    uppercased, e.g. ``orders/create`` -> ``ORDERS_CREATE``.

    Args:
        rest_topic: REST-style topic string (e.g. "orders/create").

    Returns:
        The GraphQL enum name for the topic.
    """
    return rest_topic.replace("/", "_").replace("-", "_").upper()


def _webhook_gid(webhook_id: str | int) -> str:
    """Return a WebhookSubscription GID for a stored webhook id.

    Integrations connected before the GraphQL migration stored numeric
    REST webhook ids. Those refer to the same subscriptions, so promote
    them to GIDs rather than leaving them undeletable.

    Args:
        webhook_id: A GID string or a numeric REST webhook id.

    Returns:
        A ``gid://shopify/WebhookSubscription/...`` identifier.
    """
    value = str(webhook_id)
    if value.startswith("gid://"):
        return value
    return f"gid://shopify/WebhookSubscription/{value}"


def _shopify_graphql(
    shop: str, access_token: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Execute a GraphQL Admin API request against a shop.

    The REST Admin API is legacy and unavailable to apps created after
    April 1, 2025, so all webhook management goes through GraphQL.

    Args:
        shop: The shop domain (already validated by the caller).
        access_token: The Shopify access token.
        query: The GraphQL document to execute.
        variables: Variables for the document.

    Returns:
        The ``data`` object from the response, or None on transport
        failure or a top-level GraphQL error.
    """
    api_version = settings.SHOPIFY_API_VERSION
    try:
        response = requests.post(
            f"https://{shop}/admin/api/{api_version}/graphql.json",
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
            timeout=DEFAULT_API_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Shopify GraphQL request failed for {shop}: {e!s}")
        return None
    except ValueError as e:
        logger.error(f"Shopify GraphQL returned non-JSON for {shop}: {e!s}")
        return None

    if payload.get("errors"):
        logger.error(f"Shopify GraphQL errors for {shop}: {payload['errors']}")
        return None

    data: dict[str, Any] | None = payload.get("data")
    return data


WEBHOOK_SUBSCRIPTIONS_QUERY = """
query webhookSubscriptions($first: Int!) {
  webhookSubscriptions(first: $first) {
    edges {
      node {
        id
        topic
        uri
      }
    }
  }
}
"""

WEBHOOK_SUBSCRIPTION_CREATE_MUTATION = """
mutation webhookSubscriptionCreate(
  $topic: WebhookSubscriptionTopic!
  $webhookSubscription: WebhookSubscriptionInput!
) {
  webhookSubscriptionCreate(
    topic: $topic
    webhookSubscription: $webhookSubscription
  ) {
    webhookSubscription {
      id
      topic
      uri
    }
    userErrors {
      field
      message
    }
  }
}
"""

WEBHOOK_SUBSCRIPTION_DELETE_MUTATION = """
mutation webhookSubscriptionDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors {
      field
      message
    }
  }
}
"""


def _existing_webhook_ids_by_topic(
    shop: str, access_token: str, webhook_url: str
) -> dict[str, str]:
    """Map GraphQL topic -> subscription id for subscriptions we own.

    Only subscriptions already pointing at ``webhook_url`` are returned,
    so reconnecting a shop reuses its existing subscriptions instead of
    failing on Shopify's "address already taken" error.

    Args:
        shop: The shop domain.
        access_token: The Shopify access token.
        webhook_url: The callback URL this workspace expects.

    Returns:
        Mapping of topic enum name to subscription GID. Empty when the
        lookup fails - callers then simply attempt creation.
    """
    data = _shopify_graphql(
        shop, access_token, WEBHOOK_SUBSCRIPTIONS_QUERY, {"first": 250}
    )
    if not data:
        return {}

    existing: dict[str, str] = {}
    edges = data.get("webhookSubscriptions", {}).get("edges", [])
    for edge in edges:
        node = edge.get("node") or {}
        if node.get("uri") == webhook_url and node.get("topic") and node.get("id"):
            existing[node["topic"]] = node["id"]
    return existing


def _create_one_subscription(
    shop: str, access_token: str, topic: str, webhook_url: str
) -> str | None:
    """Create a single webhook subscription.

    Args:
        shop: The shop domain.
        access_token: The Shopify access token.
        topic: REST-style topic name (e.g. "orders/create").
        webhook_url: The HTTPS callback URL.

    Returns:
        The subscription GID, or None if the create failed. A failure on
        one topic is logged and does not abort the remaining topics.
    """
    data = _shopify_graphql(
        shop,
        access_token,
        WEBHOOK_SUBSCRIPTION_CREATE_MUTATION,
        {
            "topic": _graphql_topic(topic),
            "webhookSubscription": {"uri": webhook_url, "format": "JSON"},
        },
    )
    if not data:
        return None

    result = data.get("webhookSubscriptionCreate") or {}
    user_errors = result.get("userErrors") or []
    if user_errors:
        logger.warning(f"Failed to create Shopify webhook for {topic}: {user_errors}")
        return None

    webhook_id: str | None = (result.get("webhookSubscription") or {}).get("id")
    if webhook_id:
        logger.info(f"Created Shopify webhook for {topic}: {webhook_id}")
    return webhook_id


def _create_webhook_subscriptions(
    request: HttpRequest,
    workspace: Workspace,
    shop: str,
    access_token: str,
    enabled_categories: list[str] | None = None,
) -> list[str] | None:
    """Create webhook subscriptions on the Shopify store.

    Args:
        request: The HTTP request object.
        workspace: The user's workspace.
        shop: The shop domain.
        access_token: The Shopify access token.
        enabled_categories: List of category keys to create webhooks for.
            If None, uses all default categories.

    Returns:
        List of webhook subscription GIDs, or None if none could be
        created.
    """
    if not _is_valid_shop_domain(shop):
        logger.error(f"Invalid shop domain in webhook creation: {shop}")
        messages.error(request, "Invalid shop domain. Please try again.")
        return None

    if enabled_categories is None:
        enabled_categories = _get_default_categories()

    # Get topics for enabled categories
    topics = _get_topics_for_categories(enabled_categories)
    if not topics:
        logger.warning("No webhook topics to create - no categories enabled")
        return []

    webhook_url = f"{settings.BASE_URL}/webhook/customer/{workspace.uuid}/shopify/"

    # Shopify only delivers to HTTPS endpoints, so a plain-HTTP BASE_URL
    # (the local-development default) would make every create fail with
    # an opaque userError. Say so explicitly instead.
    if not webhook_url.startswith("https://"):
        logger.error(f"BASE_URL must be HTTPS for Shopify webhooks: {webhook_url}")
        messages.error(
            request,
            "Shopify only delivers webhooks to HTTPS URLs. "
            "Configure BASE_URL with an HTTPS address and reconnect.",
        )
        return None

    existing = _existing_webhook_ids_by_topic(shop, access_token, webhook_url)
    webhook_ids: list[str] = []

    for topic in topics:
        # Reuse a subscription that already points at this workspace.
        webhook_id = existing.get(_graphql_topic(topic)) or _create_one_subscription(
            shop, access_token, topic, webhook_url
        )
        if webhook_id:
            webhook_ids.append(webhook_id)

    if not webhook_ids:
        logger.warning(f"No webhooks created for shop {shop}")
        messages.warning(
            request,
            "Connected to Shopify but could not create webhooks. "
            "You may need to configure webhooks manually in Shopify admin.",
        )
        return None

    logger.info(f"Created {len(webhook_ids)} webhooks for shop {shop}")
    return webhook_ids


def _delete_webhook_subscriptions(integration: Integration) -> None:
    """Delete webhook subscriptions from the Shopify store.

    Args:
        integration: The Shopify integration.
    """
    shop = integration.integration_settings.get("shop_domain")
    access_token = integration.oauth_credentials.get("access_token")
    webhook_ids = integration.integration_settings.get("webhook_ids", [])

    if not shop or not access_token or not _is_valid_shop_domain(shop):
        return

    for webhook_id in webhook_ids:
        gid = _webhook_gid(webhook_id)
        data = _shopify_graphql(
            shop, access_token, WEBHOOK_SUBSCRIPTION_DELETE_MUTATION, {"id": gid}
        )
        if not data:
            # Log but don't fail - the webhook might already be deleted.
            continue

        result = data.get("webhookSubscriptionDelete") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            logger.warning(f"Failed to delete Shopify webhook {gid}: {user_errors}")
        else:
            logger.info(f"Deleted Shopify webhook {gid}")

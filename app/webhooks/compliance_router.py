"""Shopify's mandatory privacy compliance webhooks.

Every public Shopify app must answer ``customers/data_request``,
``customers/redact`` and ``shop/redact``, or it is rejected at review.

These differ from the order webhooks in two ways that shape this module:

* They are declared on the app, not per shop, so one endpoint serves
  every merchant and the shop is resolved from the payload rather than
  from a workspace id in the URL.
* Shopify requires ``401`` for a bad HMAC, where the order endpoint
  answers ``400``. They are also signed with the app's client secret
  rather than a per-integration secret.
"""

import base64
import hashlib
import hmac
import json
import logging
from typing import Any

from core.models import Integration, Workspace
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .services.shopify_compliance import (
    fulfil_data_request,
    redact_customer,
    redact_shop,
)

logger = logging.getLogger(__name__)

CUSTOMER_DATA_REQUEST = "customers/data_request"
CUSTOMER_REDACT = "customers/redact"
SHOP_REDACT = "shop/redact"
APP_UNINSTALLED = "app/uninstalled"

COMPLIANCE_TOPICS = frozenset(
    {CUSTOMER_DATA_REQUEST, CUSTOMER_REDACT, SHOP_REDACT, APP_UNINSTALLED}
)


def _verify_hmac(request: HttpRequest) -> bool:
    """Verify the request against the app's client secret.

    Args:
        request: The incoming request.

    Returns:
        True when the signature is valid.
    """
    secret = settings.SHOPIFY_CLIENT_SECRET
    if not secret:
        # Never accept unverifiable compliance requests: they delete data.
        logger.error(
            "SECURITY: SHOPIFY_CLIENT_SECRET not configured; "
            "rejecting compliance webhook"
        )
        return False

    header = request.headers.get("X-Shopify-Hmac-SHA256")
    if not header:
        return False

    body = request.body
    if not isinstance(body, (bytes, bytearray)):
        return False

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(header, base64.b64encode(digest).decode("utf-8"))


def _find_workspace(shop_domain: str | None) -> Workspace | None:
    """Resolve the workspace for a shop domain.

    Compliance webhooks arrive on a single app-level endpoint, so the
    tenant has to come from the payload. Inactive integrations are
    included on purpose: shop/redact arrives 48 hours after uninstall,
    by which point the integration is no longer active but its data
    still needs erasing.

    Args:
        shop_domain: The myshopify.com domain from the payload.

    Returns:
        The workspace, or None when the shop is unknown.
    """
    if not shop_domain:
        return None
    # Lowercased to match: connect stores the normalised domain, so a
    # mixed-case value would find nothing and an erasure request would be
    # acknowledged as having no data while the data still exists.
    integration = Integration.objects.filter(
        integration_type="shopify",
        integration_settings__shop_domain=shop_domain.lower(),
    ).first()
    return integration.workspace if integration else None


def _payload(request: HttpRequest) -> dict[str, Any]:
    """Parse the JSON body.

    Args:
        request: The incoming request.

    Returns:
        The decoded body, or an empty dict when it isn't a JSON object.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@csrf_exempt
@require_http_methods(["POST"])
def shopify_compliance_webhook(request: HttpRequest) -> JsonResponse:
    """Handle all three mandatory Shopify privacy webhooks.

    Args:
        request: The incoming request.

    Returns:
        401 for an invalid signature, otherwise 200. Shopify retries any
        non-2xx, so an unknown shop or an unrecognised topic is still
        acknowledged - there is simply nothing to erase.
    """
    if not _verify_hmac(request):
        logger.warning("Rejected Shopify compliance webhook: invalid HMAC")
        return JsonResponse({"error": "Invalid webhook signature"}, status=401)

    topic = request.headers.get("X-Shopify-Topic", "")
    if topic not in COMPLIANCE_TOPICS:
        logger.warning("Ignoring unknown compliance topic: %s", topic)
        return JsonResponse({"status": "ignored"}, status=200)

    data = _payload(request)
    shop_domain = data.get("shop_domain") or request.headers.get(
        "X-Shopify-Shop-Domain"
    )
    workspace = _find_workspace(shop_domain)

    if workspace is None:
        # Nothing stored for a shop that never connected. Acknowledge so
        # Shopify stops retrying.
        logger.info(
            "Compliance webhook %s for unknown shop %s: nothing to do",
            topic,
            shop_domain,
        )
        return JsonResponse({"status": "no_data"}, status=200)

    customer = data.get("customer") or {}
    customer_id = customer.get("id")
    email = customer.get("email")

    if topic == APP_UNINSTALLED:
        # The access token dies with the uninstall, so keeping the
        # integration active would leave Notipus calling a store it can
        # no longer reach. Data is kept: shop/redact arrives 48 hours
        # later to erase it, and the merchant may reinstall before then.
        deactivated = workspace.integrations.filter(
            integration_type="shopify", is_active=True
        ).update(is_active=False)
        logger.info(
            "Shopify app/uninstalled for workspace %s: %d integrations deactivated",
            workspace.uuid,
            deactivated,
        )
        result = {"integrations": deactivated}
    elif topic == SHOP_REDACT:
        result = redact_shop(workspace)
    elif topic == CUSTOMER_REDACT:
        result = redact_customer(
            workspace, str(customer_id) if customer_id else None, email
        )
    else:
        # Emailed to the merchant, who is the controller and the only
        # party able to verify who asked. Failure is logged loudly rather
        # than returned as an error: a non-2xx would make Shopify retry a
        # webhook whose problem is on our side, not theirs.
        result = fulfil_data_request(
            workspace,
            str(customer_id) if customer_id else None,
            email,
            (data.get("data_request") or {}).get("id"),
        )

    return JsonResponse({"status": "ok", **result}, status=200)

"""Tests for binding a Shopify event to the right tenant.

Shopify's HMAC covers the request body and nothing else. It proves the
body came from Shopify; it says nothing about which store sent it, and
does not cover the headers at all. So neither the shop domain header nor
the address on its own is a trustworthy tenant selector, and events are
checked against both wherever both are known.
"""

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from core.models import Integration, Workspace
from django.test import Client
from django.urls import reverse

SECRET = "shpss_app_secret"
SHOP_A = "tenant-a.myshopify.com"
SHOP_B = "tenant-b.myshopify.com"

ORDER: dict[str, Any] = {
    "id": 5500000000001,
    "order_number": 1041,
    "email": "buyer@example.com",
    "created_at": "2026-08-15T10:22:31-04:00",
    "currency": "USD",
    "total_price": "149.00",
    "financial_status": "paid",
    "customer": {"id": 7001234567890, "email": "buyer@example.com"},
    "line_items": [{"name": "Widget", "sku": "W-1", "quantity": 1, "price": "149.00"}],
}


@pytest.fixture(autouse=True)
def app_secret(settings: Any) -> None:
    """Configure the app secret deliveries are signed with.

    Args:
        settings: pytest-django settings fixture.
    """
    settings.SHOPIFY_CLIENT_SECRET = SECRET


def make_workspace(name: str, shop: str, secret: str = "") -> Workspace:
    """Create a workspace connected to a given shop.

    Args:
        name: Workspace name.
        shop: The myshopify domain it connected.
        secret: Per-integration webhook secret, if the merchant supplied one.

    Returns:
        The workspace.
    """
    ws = Workspace.objects.create(name=name)
    Integration.objects.create(
        workspace=ws,
        integration_type="shopify",
        oauth_credentials={},
        webhook_secret=secret,
        integration_settings={
            "shop_domain": shop,
            "enabled_categories": ["orders"],
        },
        is_active=True,
    )
    return ws


def deliver_to_workspace(
    client: Client,
    workspace: Workspace,
    shop: str | None,
    secret: str = SECRET,
) -> Any:
    """POST a signed order to a workspace's own address.

    Args:
        client: Django test client.
        workspace: Target workspace.
        shop: Shop domain header, or None to omit.
        secret: Secret to sign with.

    Returns:
        The HTTP response.
    """
    raw = json.dumps(ORDER).encode()
    digest = base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    ).decode()
    headers = {"x-shopify-topic": "orders/create", "x-shopify-hmac-sha256": digest}
    if shop is not None:
        headers["x-shopify-shop-domain"] = shop
    return client.post(
        reverse(
            "webhooks:customer_shopify_webhook",
            kwargs={"organization_uuid": str(workspace.uuid)},
        ),
        data=raw,
        content_type="application/json",
        headers=headers,
    )


@pytest.mark.django_db
class TestPerTenantAddressStillWorks:
    """The workspace address is a supported delivery target."""

    def test_app_signed_event_is_accepted(self, client: Client) -> None:
        """An app-installed store keeps no secret of its own.

        Its deliveries are signed with the app's client secret, so the
        endpoint must fall back to that rather than reject everything.
        """
        workspace = make_workspace("A", SHOP_A)
        response = deliver_to_workspace(client, workspace, SHOP_A)
        assert response.status_code == 200

    def test_merchant_supplied_secret_is_used_when_present(
        self, client: Client
    ) -> None:
        """A self-registered webhook signs with the merchant's own secret.

        That path needs no app install and no credential at all, so it
        must keep working independently of the app secret.
        """
        workspace = make_workspace("A", SHOP_A, secret="merchant-own-secret")
        response = deliver_to_workspace(
            client, workspace, SHOP_A, secret="merchant-own-secret"
        )
        assert response.status_code == 200

    def test_wrong_secret_is_still_rejected(self, client: Client) -> None:
        """Falling back to the app secret must not accept anything."""
        workspace = make_workspace("A", SHOP_A)
        response = deliver_to_workspace(client, workspace, SHOP_A, secret="nope")
        assert response.status_code == 400


@pytest.mark.django_db
class TestCrossTenantMisattribution:
    """The address and the shop must agree."""

    def test_another_shops_event_cannot_be_posted_to_this_workspace(
        self, client: Client
    ) -> None:
        """The core safety net.

        Every Shopify integration validates against the same app client
        secret, so a body captured from one store carries a signature
        that verifies at any workspace's address. Only the shop check
        stops tenant B being shown tenant A's orders.
        """
        make_workspace("A", SHOP_A)
        workspace_b = make_workspace("B", SHOP_B)

        response = deliver_to_workspace(client, workspace_b, SHOP_A)

        assert response.status_code == 403
        assert response.json()["error"] == "ShopMismatch"

    def test_matching_shop_passes(self, client: Client) -> None:
        """The legitimate case is unaffected."""
        workspace_b = make_workspace("B", SHOP_B)
        response = deliver_to_workspace(client, workspace_b, SHOP_B)
        assert response.status_code == 200

    def test_shop_comparison_ignores_case(self, client: Client) -> None:
        """Domains are case-insensitive; a mismatch here would be a bug."""
        workspace = make_workspace("A", SHOP_A)
        response = deliver_to_workspace(client, workspace, SHOP_A.upper())
        assert response.status_code == 200

    def test_absent_header_does_not_block_delivery(self, client: Client) -> None:
        """Not every sender sets the header.

        With nothing to compare, the address alone decides - which is the
        behaviour that existed before the check was added.
        """
        workspace = make_workspace("A", SHOP_A)
        response = deliver_to_workspace(client, workspace, None)
        assert response.status_code == 200

    def test_unconfigured_shop_does_not_block_delivery(self, client: Client) -> None:
        """A merchant-registered webhook may have no shop recorded."""
        workspace = Workspace.objects.create(name="No shop recorded")
        Integration.objects.create(
            workspace=workspace,
            integration_type="shopify",
            oauth_credentials={},
            webhook_secret="",
            integration_settings={"enabled_categories": ["orders"]},
            is_active=True,
        )
        response = deliver_to_workspace(client, workspace, SHOP_A)
        assert response.status_code == 200

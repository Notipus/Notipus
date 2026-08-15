"""Tests for the app-level Shopify events endpoint.

Subscriptions are declared in the app configuration rather than created
per shop, so every installed store delivers to one endpoint and Notipus
holds no Admin API credential. That makes two things load-bearing: the
shop domain header, which selects the tenant, and the app client secret,
which is the only thing proving the request came from Shopify.
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
SHOP = "events-test.myshopify.com"

ORDER_BODY: dict[str, Any] = {
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
    """Configure the app secret every delivery is signed with.

    Args:
        settings: pytest-django settings fixture.
    """
    settings.SHOPIFY_CLIENT_SECRET = SECRET


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create a workspace with a connected store and no credentials.

    Args:
        db: pytest-django database fixture.

    Returns:
        The workspace.
    """
    ws = Workspace.objects.create(name="Events Test")
    Integration.objects.create(
        workspace=ws,
        integration_type="shopify",
        oauth_credentials={},
        webhook_secret="",
        integration_settings={
            "shop_domain": SHOP,
            "enabled_categories": ["orders", "fulfillment"],
        },
        is_active=True,
    )
    return ws


def deliver(
    client: Client,
    topic: str,
    body: dict[str, Any] | None = None,
    shop: str | None = SHOP,
    secret: str = SECRET,
) -> Any:
    """Deliver a signed event the way Shopify would.

    Args:
        client: Django test client.
        topic: Shopify topic header.
        body: Payload, defaulting to an order.
        shop: Shop domain header, or None to omit it.
        secret: Secret to sign with.

    Returns:
        The HTTP response.
    """
    raw = json.dumps(ORDER_BODY if body is None else body).encode()
    digest = base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    ).decode()
    headers = {"x-shopify-topic": topic, "x-shopify-hmac-sha256": digest}
    if shop is not None:
        headers["x-shopify-shop-domain"] = shop
    return client.post(
        reverse("webhooks:shopify_events_webhook"),
        data=raw,
        content_type="application/json",
        headers=headers,
    )


@pytest.mark.django_db
class TestSignature:
    """The app client secret is the only proof of origin."""

    def test_valid_signature_is_processed(
        self, client: Client, workspace: Workspace
    ) -> None:
        """A properly signed order is accepted."""
        response = deliver(client, "orders/create")
        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_forged_signature_is_rejected(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Anyone could POST here; only Shopify can sign."""
        response = deliver(client, "orders/create", secret="wrong-secret")
        assert response.status_code == 401

    def test_signature_is_checked_before_the_shop_is_resolved(
        self, client: Client
    ) -> None:
        """An unsigned request must not probe which shops exist.

        Answering "unknown shop" before verifying would let anyone
        enumerate connected stores.
        """
        response = deliver(
            client, "orders/create", shop="probe.myshopify.com", secret="wrong-secret"
        )
        assert response.status_code == 401


@pytest.mark.django_db
class TestTenantRouting:
    """One endpoint serves every store, so routing must be exact."""

    def test_events_reach_the_matching_workspace(
        self, client: Client, workspace: Workspace
    ) -> None:
        """The shop domain header selects the tenant."""
        response = deliver(client, "orders/create")
        assert response.status_code == 200

    def test_unknown_shop_is_acknowledged_not_retried(self, client: Client) -> None:
        """A store with no workspace has nowhere to deliver.

        Answering non-2xx would make Shopify retry forever for a store
        that will never have a destination.
        """
        response = deliver(client, "orders/create", shop="nobody.myshopify.com")
        assert response.status_code == 200

    def test_missing_shop_header_is_acknowledged(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Without the header there is no tenant to pick."""
        response = deliver(client, "orders/create", shop=None)
        assert response.status_code == 200

    def test_disconnected_store_stops_receiving(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Disconnecting cannot unsubscribe at Shopify's end.

        Subscriptions belong to the app, so events keep arriving and the
        endpoint has to drop them.
        """
        Integration.objects.filter(workspace=workspace).update(is_active=False)
        response = deliver(client, "orders/create")
        assert response.status_code == 200
        assert "no workspace" in response.json()["message"]


@pytest.mark.django_db
class TestCategoryFiltering:
    """Merchants still choose what they receive, without an API call."""

    def test_enabled_category_is_processed(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Orders are enabled for this workspace."""
        response = deliver(client, "orders/create")
        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_disabled_category_is_dropped(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Customers are not enabled, so the event is discarded.

        Shopify sends every declared topic to every installed store, so
        this filter is what preserves the merchant's choice.
        """
        response = deliver(
            client,
            "customers/update",
            body={"id": 7001234567890, "email": "buyer@example.com"},
        )
        assert response.status_code == 200
        assert "not enabled" in response.json()["message"]

    def test_unknown_topic_is_not_filtered_out(
        self, client: Client, workspace: Workspace
    ) -> None:
        """A topic in no category is left for the parser to judge."""
        response = deliver(client, "checkouts/create")
        assert response.status_code == 200
        assert "not enabled" not in response.json()["message"]


@pytest.mark.django_db
class TestSignedShopBinding:
    """The unsigned header is checked against the signed body."""

    def test_header_cannot_redirect_an_order_to_another_tenant(
        self, client: Client, workspace: Workspace
    ) -> None:
        """The strongest guarantee this endpoint can offer.

        Order payloads name their store inside the signed body, so a
        captured body cannot be re-pointed at a different workspace by
        rewriting the header.
        """
        body = dict(
            ORDER_BODY,
            order_status_url="https://someone-else.myshopify.com/1/orders/x",
        )
        response = deliver(client, "orders/create", body=body)

        assert response.status_code == 403
        assert response.json()["error"] == "ShopMismatch"

    def test_matching_signed_shop_passes(
        self, client: Client, workspace: Workspace
    ) -> None:
        """The legitimate case is unaffected."""
        body = dict(ORDER_BODY, order_status_url=f"https://{SHOP}/1/orders/x")
        response = deliver(client, "orders/create", body=body)
        assert response.status_code == 200

    def test_custom_primary_domain_is_not_rejected(
        self, client: Client, workspace: Workspace
    ) -> None:
        """A store may use its own domain in order_status_url.

        That cannot be compared against the myshopify domain in the
        header, so it goes unchecked rather than being wrongly dropped.
        """
        body = dict(ORDER_BODY, order_status_url="https://shop.brand.com/1/orders/x")
        response = deliver(client, "orders/create", body=body)
        assert response.status_code == 200

    def test_payload_without_shop_identity_still_delivers(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Fulfillments and customer events carry no store name.

        Those fall back to the header alone, which is why the per-tenant
        address remains the stronger target.
        """
        response = deliver(client, "orders/create")
        assert response.status_code == 200

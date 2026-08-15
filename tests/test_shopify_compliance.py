"""Tests for Shopify's mandatory privacy compliance webhooks.

Shopify rejects any public app that does not implement these three
topics, or that does not answer them as specified - notably 401 for an
invalid HMAC, where the order webhooks answer 400.
"""

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from core.encrypted_cache import encrypt_cache_value
from core.models import Integration, Workspace
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

SECRET = "shpss_test_secret"
SHOP = "compliance-test.myshopify.com"


@pytest.fixture(autouse=True)
def app_secret(settings: Any) -> None:
    """Configure the app secret and a cache that actually stores.

    The default test cache is a DummyCache, which silently drops every
    write - so assertions about records being deleted would pass whether
    or not the code deleted anything.

    Args:
        settings: pytest-django settings fixture.
    """
    settings.SHOPIFY_CLIENT_SECRET = SECRET
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "shopify-compliance-tests",
        }
    }
    cache.clear()


def store_record(workspace_uuid: str, key: str, record: dict[str, Any]) -> None:
    """Store a webhook record the way production does.

    Args:
        workspace_uuid: Owning workspace UUID.
        key: Redis key for the record.
        record: The record contents.
    """
    from django.utils import timezone
    from webhooks.services.shopify_compliance import _activity_key

    cache.set(key, encrypt_cache_value(record), timeout=600)
    date_str = timezone.now().strftime("%Y-%m-%d")
    activity_key = _activity_key(workspace_uuid, date_str)
    existing = cache.get(activity_key)
    keys = json.loads(existing) if existing else []
    keys.append(key)
    cache.set(activity_key, json.dumps(keys), timeout=600)


def sign(body: bytes, secret: str = SECRET) -> str:
    """Sign a body the way Shopify does.

    Args:
        body: Raw request body.
        secret: App client secret.

    Returns:
        Base64 HMAC-SHA256 digest.
    """
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


def post(
    client: Client,
    url_name: str,
    topic: str,
    payload: dict[str, Any],
    secret: str = SECRET,
) -> Any:
    """POST a signed compliance webhook.

    Args:
        client: Django test client.
        url_name: Named URL to hit.
        topic: Shopify topic header.
        payload: Body to send.
        secret: Secret to sign with.

    Returns:
        The HTTP response.
    """
    body = json.dumps(payload).encode()
    return client.post(
        reverse(f"webhooks:{url_name}"),
        data=body,
        content_type="application/json",
        headers={
            "x-shopify-topic": topic,
            "x-shopify-hmac-sha256": sign(body, secret),
            "x-shopify-shop-domain": SHOP,
        },
    )


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create a workspace with a connected Shopify store.

    Args:
        db: pytest-django database fixture.

    Returns:
        The workspace.
    """
    ws = Workspace.objects.create(name="Compliance Test")
    Integration.objects.create(
        workspace=ws,
        integration_type="shopify",
        oauth_credentials={"access_token": "tok"},
        integration_settings={"shop_domain": SHOP},
        is_active=True,
    )
    return ws


@pytest.mark.django_db
class TestSignatureHandling:
    """Signature rules, which Shopify checks explicitly at review."""

    def test_invalid_hmac_returns_401_not_400(self, client: Client) -> None:
        """Shopify mandates 401 here, unlike the order endpoint's 400."""
        body = json.dumps({"shop_domain": SHOP}).encode()
        response = client.post(
            reverse("webhooks:shopify_customer_redact"),
            data=body,
            content_type="application/json",
            headers={
                "x-shopify-topic": "customers/redact",
                "x-shopify-hmac-sha256": "not-a-valid-signature",
            },
        )
        assert response.status_code == 401

    def test_missing_hmac_returns_401(self, client: Client) -> None:
        """An unsigned request must never reach the erasure code."""
        response = client.post(
            reverse("webhooks:shopify_shop_redact"),
            data=json.dumps({"shop_domain": SHOP}),
            content_type="application/json",
            headers={"x-shopify-topic": "shop/redact"},
        )
        assert response.status_code == 401

    def test_wrong_secret_returns_401(
        self, client: Client, workspace: Workspace
    ) -> None:
        """A signature from another app's secret is not acceptable."""
        response = post(
            client,
            "shopify_customer_redact",
            "customers/redact",
            {"shop_domain": SHOP},
            secret="a-different-secret",
        )
        assert response.status_code == 401

    def test_valid_hmac_is_accepted(self, client: Client, workspace: Workspace) -> None:
        """A correctly signed request is processed."""
        response = post(
            client,
            "shopify_customer_redact",
            "customers/redact",
            {"shop_domain": SHOP, "customer": {"id": 1, "email": "a@b.com"}},
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestUnconfiguredSecret:
    """A missing secret must fail closed - these requests delete data."""

    def test_unconfigured_secret_rejects_everything(
        self, client: Client, settings: Any
    ) -> None:
        """Without a secret nothing can be verified, so nothing is trusted."""
        settings.SHOPIFY_CLIENT_SECRET = ""
        response = post(
            client, "shopify_shop_redact", "shop/redact", {"shop_domain": SHOP}
        )
        assert response.status_code == 401


@pytest.mark.django_db
class TestRedaction:
    """The erasure topics must actually erase."""

    def test_shop_redact_deletes_records_and_deactivates(
        self, client: Client, workspace: Workspace
    ) -> None:
        """shop/redact clears stored data and stops future delivery."""
        webhook_key = f"webhook:{workspace.uuid}:order:123"
        store_record(str(workspace.uuid), webhook_key, {"customer_id": "42"})

        response = post(
            client, "shopify_shop_redact", "shop/redact", {"shop_domain": SHOP}
        )

        assert response.status_code == 200
        assert cache.get(webhook_key) is None
        integration = Integration.objects.get(workspace=workspace)
        assert integration.is_active is False

    def test_customer_redact_removes_only_that_customer(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Erasing one person must not erase everyone else's records."""
        target = f"webhook:{workspace.uuid}:order:target"
        other = f"webhook:{workspace.uuid}:order:other"
        store_record(str(workspace.uuid), target, {"customer_id": "42"})
        store_record(str(workspace.uuid), other, {"customer_id": "99"})

        response = post(
            client,
            "shopify_customer_redact",
            "customers/redact",
            {"shop_domain": SHOP, "customer": {"id": 42}},
        )

        assert response.status_code == 200
        assert cache.get(target) is None
        assert cache.get(other) is not None

    def test_guest_checkout_is_matched_by_email(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Guest orders are keyed by email, not by customer id.

        Matching only on id would leave guest records behind after an
        erasure request.
        """
        guest = f"webhook:{workspace.uuid}:order:guest"
        store_record(str(workspace.uuid), guest, {"customer_id": "walkin@example.com"})

        response = post(
            client,
            "shopify_customer_redact",
            "customers/redact",
            {"shop_domain": SHOP, "customer": {"email": "walkin@example.com"}},
        )

        assert response.status_code == 200
        assert cache.get(guest) is None


@pytest.mark.django_db
class TestAcknowledgement:
    """Shopify retries anything non-2xx, so these must still return 200."""

    def test_unknown_shop_is_acknowledged(self, client: Client) -> None:
        """A shop that never connected has nothing to erase."""
        response = post(
            client,
            "shopify_shop_redact",
            "shop/redact",
            {"shop_domain": "never-connected.myshopify.com"},
        )
        assert response.status_code == 200

    def test_data_request_is_answered(
        self, client: Client, workspace: Workspace
    ) -> None:
        """customers/data_request is acknowledged and reported."""
        response = post(
            client,
            "shopify_customer_data_request",
            "customers/data_request",
            {
                "shop_domain": SHOP,
                "customer": {"id": 7, "email": "who@example.com"},
                "data_request": {"id": 999},
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_shop_redact_works_after_uninstall(
        self, client: Client, workspace: Workspace
    ) -> None:
        """shop/redact arrives 48h after uninstall.

        By then the integration is inactive, so looking only at active
        integrations would silently skip the erasure.
        """
        Integration.objects.filter(workspace=workspace).update(is_active=False)
        response = post(
            client, "shopify_shop_redact", "shop/redact", {"shop_domain": SHOP}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.django_db
class TestAppUninstalled:
    """Uninstall handling, so dead tokens stop being used."""

    def test_uninstall_deactivates_but_keeps_data(
        self, client: Client, workspace: Workspace
    ) -> None:
        """Data survives the uninstall; shop/redact erases it later.

        Shopify sends shop/redact 48 hours afterwards, and the merchant
        may reinstall before then, so erasing here would lose history
        for a reconnect.
        """
        webhook_key = f"webhook:{workspace.uuid}:order:keepme"
        store_record(str(workspace.uuid), webhook_key, {"customer_id": "42"})

        response = post(
            client,
            "shopify_app_uninstalled",
            "app/uninstalled",
            {"shop_domain": SHOP},
        )

        assert response.status_code == 200
        assert Integration.objects.get(workspace=workspace).is_active is False
        assert cache.get(webhook_key) is not None

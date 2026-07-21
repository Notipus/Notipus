"""Tests for signed-content webhook deduplication (issue #118).

Chargify and Shopify HMAC-sign only the raw request body; the webhook-id
and timestamp headers are attacker-mutable. Replay protection therefore
keys the router dedup on the SHA-256 of the signed body (surfaced by the
source plugins as ``content_hash``) instead of the unsigned
``X-*-Webhook-Id`` headers. Verifies that:

- a legitimate provider retry (identical raw body) is suppressed,
- a replay of a captured body with a freshly minted webhook-id header is
  still suppressed (it cannot mint a new dedup key),
- distinct events (different signed bodies) produce distinct dedup keys
  and both process.
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pytest
from core.models import Integration, Workspace
from django.http import HttpResponse
from django.test import Client
from webhooks.webhook_router import _get_dedup_key

CHARGIFY_SECRET = "test-chargify-secret"
SHOPIFY_SECRET = "test-shopify-secret"


@pytest.fixture
def mock_consolidation_cache() -> Generator[dict, None, None]:
    """Back the event consolidation cache with a real dict.

    Tests run with DummyCache, which would make every dedup check a
    no-op. This fixture provides working get/set semantics.

    Yields:
        The dict backing the cache, for inspection.
    """
    cache_data: dict = {}

    def mock_get(key: str, default: Any = None) -> Any:
        return cache_data.get(key, default)

    def mock_set(key: str, value: Any, timeout: Any = None) -> None:
        cache_data[key] = value

    with patch("webhooks.services.event_consolidation.cache") as mock:
        mock.get = mock_get
        mock.set = mock_set
        yield cache_data


def _chargify_body(**overrides: str) -> bytes:
    """Build a signed-content Chargify form body.

    Mirrors real Chargify webhook bodies, which embed the webhook's own
    id (top-level ``id`` field) and the transaction id, making each
    distinct event's body unique while retries resend it byte-identical.

    Args:
        overrides: Form fields to override or add.

    Returns:
        URL-encoded form body bytes.
    """
    fields = {
        "event": "payment_success",
        "id": "wh_1001",
        "payload[subscription][id]": "sub_789",
        "payload[subscription][customer][id]": "cust_123",
        "payload[subscription][customer][email]": "test@example.com",
        "payload[subscription][product][name]": "Premium Plan",
        "payload[transaction][id]": "txn_1",
        "payload[transaction][amount_in_cents]": "2999",
        "created_at": "2024-03-15T10:00:00Z",
    }
    fields.update(overrides)
    return urlencode(fields).encode()


def _post_chargify(
    client: Client, workspace: Workspace, body: bytes, webhook_id: str
) -> HttpResponse:
    """POST a Chargify webhook with a genuine HMAC over the body.

    Args:
        client: Django test client.
        workspace: Target workspace.
        body: Raw form-encoded body to send (and sign).
        webhook_id: Value for the unsigned X-Chargify-Webhook-Id header.

    Returns:
        The webhook endpoint response.
    """
    signature = hmac.new(CHARGIFY_SECRET.encode(), body, hashlib.sha256).hexdigest()
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return client.post(
        f"/webhook/customer/{workspace.uuid}/chargify/",
        data=body,
        content_type="application/x-www-form-urlencoded",
        HTTP_X_CHARGIFY_WEBHOOK_ID=webhook_id,
        HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256=signature,
        HTTP_X_CHARGIFY_WEBHOOK_TIMESTAMP=timestamp,
    )


def _shopify_body(
    order_id: int = 555,
    financial_status: str = "paid",
    updated_at: str = "2024-03-15T10:00:00-05:00",
) -> bytes:
    """Build a signed-content Shopify order JSON body.

    Mirrors real Shopify order payloads, which embed the order id and
    ``updated_at``/``financial_status``, making distinct events' bodies
    unique while redeliveries resend the body byte-identical.

    Args:
        order_id: Shopify order id embedded in the body.
        financial_status: Order financial status embedded in the body.
        updated_at: Order updated_at timestamp embedded in the body.

    Returns:
        JSON body bytes.
    """
    payload = {
        "id": order_id,
        "order_number": 1020,
        "customer": {"id": 456, "email": "buyer@example.com"},
        "total_price": "10.00",
        "currency": "USD",
        "financial_status": financial_status,
        "created_at": "2024-03-15T10:00:00-05:00",
        "updated_at": updated_at,
    }
    return json.dumps(payload).encode()


def _post_shopify(
    client: Client,
    workspace: Workspace,
    body: bytes,
    webhook_id: str,
    topic: str = "orders/create",
) -> HttpResponse:
    """POST a Shopify webhook with a genuine HMAC over the body.

    Args:
        client: Django test client.
        workspace: Target workspace.
        body: Raw JSON body to send (and sign).
        webhook_id: Value for the unsigned X-Shopify-Webhook-Id header.
        topic: Shopify webhook topic header.

    Returns:
        The webhook endpoint response.
    """
    digest = hmac.new(SHOPIFY_SECRET.encode(), body, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    return client.post(
        f"/webhook/customer/{workspace.uuid}/shopify/",
        data=body,
        content_type="application/json",
        HTTP_X_SHOPIFY_TOPIC=topic,
        HTTP_X_SHOPIFY_HMAC_SHA256=signature,
        HTTP_X_SHOPIFY_WEBHOOK_ID=webhook_id,
    )


class TestDedupKeyPrefersSignedContent:
    """Unit tests for the router dedup key derivation."""

    def test_content_hash_preferred_over_event_id(self) -> None:
        """The signed body hash wins over any surfaced event id."""
        event_data = {
            "provider": "chargify",
            "content_hash": "abc123",
            "event_id": "unsigned_header_id",
            "external_id": "sub_1",
            "type": "payment_success",
        }
        assert _get_dedup_key(event_data) == "chargify:sha256:abc123"

    def test_content_hash_is_provider_namespaced(self) -> None:
        """Equal body hashes from different providers cannot collide."""
        chargify = {"provider": "chargify", "content_hash": "same"}
        shopify = {"provider": "shopify", "content_hash": "same"}
        assert _get_dedup_key(chargify) != _get_dedup_key(shopify)

    def test_event_id_still_used_without_content_hash(self) -> None:
        """Stripe's signed evt_... id keeps working as the dedup key."""
        event_data = {
            "provider": "stripe",
            "event_id": "evt_123",
            "external_id": "sub_ABC",
        }
        assert _get_dedup_key(event_data) == "evt_123"


class TestParsersSurfaceContentHash:
    """Parser-level tests: plugins surface the signed body hash."""

    def test_chargify_sets_content_hash_not_event_id(self) -> None:
        """Chargify surfaces sha256(body); the unsigned id is not a key."""
        from plugins.sources.chargify import ChargifySourcePlugin

        body = _chargify_body()
        fields = {
            "event": "payment_success",
            "id": "wh_1001",
            "payload[subscription][id]": "sub_789",
            "payload[subscription][customer][id]": "cust_123",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[transaction][id]": "txn_1",
            "payload[transaction][amount_in_cents]": "2999",
            "created_at": "2024-03-15T10:00:00Z",
        }
        mock_request = Mock()
        mock_request.content_type = "application/x-www-form-urlencoded"
        mock_request.headers = {"X-Chargify-Webhook-Id": "header_id_1"}
        mock_request.body = body
        mock_request.POST = Mock()
        mock_request.POST.dict.return_value = fields
        mock_request.POST.get.return_value = "payment_success"

        plugin = ChargifySourcePlugin(webhook_secret=CHARGIFY_SECRET)
        event = plugin.parse_webhook(mock_request)

        assert event is not None
        assert event["content_hash"] == hashlib.sha256(body).hexdigest()
        assert "event_id" not in event

    def test_shopify_sets_content_hash_not_event_id(self) -> None:
        """Shopify surfaces sha256(body); the unsigned id is not a key."""
        from plugins.sources.shopify import ShopifySourcePlugin

        body = _shopify_body()
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {
            "X-Shopify-Topic": "orders/create",
            "X-Shopify-Webhook-Id": "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043",
        }
        mock_request.data = body
        mock_request.body = body

        plugin = ShopifySourcePlugin(webhook_secret=SHOPIFY_SECRET)
        event = plugin.parse_webhook(mock_request)

        assert event is not None
        assert event["content_hash"] == hashlib.sha256(body).hexdigest()
        assert "event_id" not in event


@pytest.mark.django_db
class TestChargifySignedContentDedup:
    """End-to-end Chargify dedup keyed on the signed body."""

    @pytest.fixture
    def workspace(self) -> Workspace:
        """Create a workspace with an active Chargify integration."""
        workspace = Workspace.objects.create(
            name="Test Workspace", shop_domain="test.myshopify.com"
        )
        Integration.objects.create(
            workspace=workspace,
            integration_type="chargify",
            webhook_secret=CHARGIFY_SECRET,
            is_active=True,
        )
        return workspace

    @patch("django.conf.settings.EVENT_PROCESSOR")
    def test_legitimate_retry_same_body_is_deduped(
        self,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        client: Client,
        workspace: Workspace,
    ) -> None:
        """A provider retry resends the identical body and is suppressed."""
        mock_processor.process_event_rich.return_value = {"blocks": []}
        body = _chargify_body()

        first = _post_chargify(client, workspace, body, "wh_1001")
        second = _post_chargify(client, workspace, body, "wh_1001")

        assert first.status_code == 200
        assert "successfully" in first.json()["message"]
        assert second.status_code == 200
        assert "duplicate suppressed" in second.json()["message"]

    @patch("django.conf.settings.EVENT_PROCESSOR")
    def test_replay_with_fresh_webhook_id_header_is_suppressed(
        self,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        client: Client,
        workspace: Workspace,
    ) -> None:
        """A replayed body with a minted webhook-id header is suppressed.

        The X-Chargify-Webhook-Id header is not covered by the HMAC, so
        an attacker replaying a captured (body, signature) pair can set
        any value; it must not mint a fresh dedup key.
        """
        mock_processor.process_event_rich.return_value = {"blocks": []}
        body = _chargify_body()

        first = _post_chargify(client, workspace, body, "wh_1001")
        replay = _post_chargify(client, workspace, body, "attacker_minted_id")

        assert first.status_code == 200
        assert "successfully" in first.json()["message"]
        assert replay.status_code == 200
        assert "duplicate suppressed" in replay.json()["message"]
        # Exactly one notification was built
        assert mock_processor.process_event_rich.call_count == 1

    @patch("django.conf.settings.EVENT_PROCESSOR")
    def test_distinct_events_different_bodies_both_process(
        self,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        client: Client,
        workspace: Workspace,
    ) -> None:
        """Two distinct events (different signed bodies) both process."""
        mock_processor.process_event_rich.return_value = {"blocks": []}
        first_body = _chargify_body(
            **{"id": "wh_1001", "payload[transaction][id]": "txn_1"}
        )
        second_body = _chargify_body(
            **{"id": "wh_1002", "payload[transaction][id]": "txn_2"}
        )

        first = _post_chargify(client, workspace, first_body, "wh_1001")
        second = _post_chargify(client, workspace, second_body, "wh_1002")

        assert first.status_code == 200
        assert "successfully" in first.json()["message"]
        assert second.status_code == 200
        assert "successfully" in second.json()["message"]
        assert mock_processor.process_event_rich.call_count == 2


@pytest.mark.django_db
class TestShopifySignedContentDedup:
    """End-to-end Shopify dedup keyed on the signed body."""

    @pytest.fixture
    def workspace(self) -> Workspace:
        """Create a workspace with an active Shopify integration."""
        workspace = Workspace.objects.create(
            name="Test Workspace", shop_domain="test.myshopify.com"
        )
        Integration.objects.create(
            workspace=workspace,
            integration_type="shopify",
            webhook_secret=SHOPIFY_SECRET,
            is_active=True,
        )
        return workspace

    @patch("django.conf.settings.EVENT_PROCESSOR")
    def test_legitimate_retry_same_body_is_deduped(
        self,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        client: Client,
        workspace: Workspace,
    ) -> None:
        """A Shopify redelivery resends the identical body; suppressed."""
        mock_processor.process_event_rich.return_value = {"blocks": []}
        body = _shopify_body()

        first = _post_shopify(client, workspace, body, "delivery-1")
        second = _post_shopify(client, workspace, body, "delivery-1")

        assert first.status_code == 200
        assert "successfully" in first.json()["message"]
        assert second.status_code == 200
        assert "duplicate suppressed" in second.json()["message"]

    @patch("django.conf.settings.EVENT_PROCESSOR")
    def test_replay_with_fresh_webhook_id_header_is_suppressed(
        self,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        client: Client,
        workspace: Workspace,
    ) -> None:
        """A replayed body with a minted webhook-id header is suppressed.

        The X-Shopify-Webhook-Id header is not covered by the HMAC, so
        an attacker replaying a captured (body, signature) pair can set
        any value; it must not mint a fresh dedup key.
        """
        mock_processor.process_event_rich.return_value = {"blocks": []}
        body = _shopify_body()

        first = _post_shopify(client, workspace, body, "delivery-1")
        replay = _post_shopify(client, workspace, body, "attacker-minted-id")

        assert first.status_code == 200
        assert "successfully" in first.json()["message"]
        assert replay.status_code == 200
        assert "duplicate suppressed" in replay.json()["message"]
        assert mock_processor.process_event_rich.call_count == 1

    @patch("django.conf.settings.EVENT_PROCESSOR")
    def test_distinct_events_different_bodies_both_process(
        self,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        client: Client,
        workspace: Workspace,
    ) -> None:
        """Two distinct orders (different signed bodies) both process."""
        mock_processor.process_event_rich.return_value = {"blocks": []}
        first_body = _shopify_body(order_id=555)
        second_body = _shopify_body(order_id=556)

        first = _post_shopify(client, workspace, first_body, "delivery-1")
        second = _post_shopify(client, workspace, second_body, "delivery-2")

        assert first.status_code == 200
        assert "successfully" in first.json()["message"]
        assert second.status_code == 200
        assert "successfully" in second.json()["message"]
        assert mock_processor.process_event_rich.call_count == 2

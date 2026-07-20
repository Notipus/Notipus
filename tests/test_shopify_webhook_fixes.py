"""Tests for the Shopify integration end-to-end fixes.

Covers webhook secret storage/backfill, event type mappings
(orders/cancelled, orders/refunded, customers/create, checkouts/create),
guest checkout handling, null line-item prices, consolidation bucket
integrity, and Decimal monetary precision.
"""

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any
from unittest.mock import Mock, patch

import pytest
from core.models import Integration, Workspace
from django.apps import apps as django_apps
from django.http import JsonResponse
from django.test import Client, override_settings
from plugins.sources.shopify import ShopifySourcePlugin
from webhooks.models.rich_notification import NotificationSeverity, NotificationType
from webhooks.services.event_consolidation import EventConsolidationService
from webhooks.services.event_processor import EventProcessor

WEBHOOK_SECRET = "test-shopify-secret"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Compute a valid Shopify HMAC-SHA256 signature for a body.

    Args:
        body: The raw request body bytes.
        secret: The webhook secret to sign with.

    Returns:
        Base64-encoded HMAC signature string.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture
def workspace(db: Any) -> Workspace:
    """Create a test workspace.

    Args:
        db: pytest-django database fixture.

    Returns:
        A Workspace instance.
    """
    return Workspace.objects.create(
        name="Test Workspace",
        shop_domain="test.myshopify.com",
    )


@pytest.fixture
def shopify_integration(workspace: Workspace) -> Integration:
    """Create an active Shopify integration with a webhook secret.

    Args:
        workspace: The workspace fixture.

    Returns:
        An Integration instance.
    """
    return Integration.objects.create(
        workspace=workspace,
        integration_type="shopify",
        webhook_secret=WEBHOOK_SECRET,
        is_active=True,
    )


def _post_shopify_webhook(
    client: Client,
    workspace: Workspace,
    topic: str,
    payload: dict[str, Any],
) -> JsonResponse:
    """POST a signed Shopify webhook to the workspace endpoint.

    Args:
        client: Django test client.
        workspace: Target workspace.
        topic: Shopify webhook topic header value.
        payload: JSON payload to send.

    Returns:
        The HTTP response.
    """
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        f"/webhook/customer/{workspace.uuid}/shopify/",
        data=body,
        content_type="application/json",
        HTTP_X_SHOPIFY_TOPIC=topic,
        HTTP_X_SHOPIFY_HMAC_SHA256=_sign(body),
        HTTP_X_SHOPIFY_SHOP_DOMAIN="test.myshopify.com",
    )


@pytest.mark.django_db
class TestShopifyWebhookEndToEnd:
    """End-to-end webhook tests through the router with real HMAC validation."""

    def test_orders_cancelled_returns_200_and_produces_notification(
        self,
        client: Client,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """orders/cancelled must return 200 and produce a notification."""
        payload = {
            "id": 820982911946154508,
            "order_number": 1001,
            "customer": {"id": 456, "email": "buyer@gmail.com"},
            "total_price": "29.99",
            "currency": "USD",
            "created_at": "2024-03-15T10:00:00Z",
        }

        with patch("django.conf.settings.EVENT_PROCESSOR") as mock_processor:
            mock_processor.process_event_rich.return_value = {"blocks": []}
            response = _post_shopify_webhook(
                client, workspace, "orders/cancelled", payload
            )

        assert response.status_code == 200
        mock_processor.process_event_rich.assert_called_once()
        event_data = mock_processor.process_event_rich.call_args[0][0]
        assert event_data["type"] == "order_cancelled"
        assert event_data["customer_id"] == "456"

    def test_orders_refunded_is_accepted(
        self,
        client: Client,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """orders/refunded must be accepted and map to refund_issued."""
        payload = {
            "id": 820982911946154509,
            "order_number": 1002,
            "customer": {"id": 456, "email": "buyer@gmail.com"},
            "total_price": "19.99",
            "currency": "USD",
        }

        with patch("django.conf.settings.EVENT_PROCESSOR") as mock_processor:
            mock_processor.process_event_rich.return_value = {"blocks": []}
            response = _post_shopify_webhook(
                client, workspace, "orders/refunded", payload
            )

        assert response.status_code == 200
        event_data = mock_processor.process_event_rich.call_args[0][0]
        assert event_data["type"] == "refund_issued"

    def test_guest_checkout_returns_200(
        self,
        client: Client,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """Guest checkout (customer explicitly null) must not 500."""
        payload = {
            "id": 820982911946154510,
            "order_number": 1003,
            "customer": None,
            "email": "guest@gmail.com",
            "total_price": "10.00",
            "currency": "USD",
        }

        with patch("django.conf.settings.EVENT_PROCESSOR") as mock_processor:
            mock_processor.process_event_rich.return_value = {"blocks": []}
            response = _post_shopify_webhook(
                client, workspace, "orders/create", payload
            )

        assert response.status_code == 200
        event_data = mock_processor.process_event_rich.call_args[0][0]
        # Guest checkouts are identified by email, never by the order id
        assert event_data["customer_id"] == "guest@gmail.com"

    def test_guest_checkout_without_email_returns_200(
        self,
        client: Client,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """Guest checkout without any email still returns 200."""
        payload = {
            "id": 820982911946154511,
            "order_number": 1004,
            "customer": None,
            "total_price": "10.00",
            "currency": "USD",
        }

        with patch("django.conf.settings.EVENT_PROCESSOR") as mock_processor:
            mock_processor.process_event_rich.return_value = {"blocks": []}
            response = _post_shopify_webhook(
                client, workspace, "orders/create", payload
            )

        assert response.status_code == 200
        event_data = mock_processor.process_event_rich.call_args[0][0]
        assert event_data["customer_id"] is None

    def test_unknown_topic_returns_200(
        self,
        client: Client,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """Unknown topics must be acknowledged with 200, not rejected."""
        response = _post_shopify_webhook(
            client, workspace, "products/create", {"id": 1}
        )
        assert response.status_code == 200

    def test_null_price_line_item_does_not_crash(
        self,
        client: Client,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """A line item with an explicit null price must not cause a 500."""
        payload = {
            "id": 820982911946154512,
            "order_number": 1005,
            "customer": {"id": 456, "email": "buyer@gmail.com"},
            "total_price": "5.00",
            "currency": "USD",
            "line_items": [
                {"name": "Gift Card", "price": None, "quantity": 1},
                {"name": "Widget", "price": "5.00", "quantity": 1},
            ],
        }

        with patch("django.conf.settings.EVENT_PROCESSOR") as mock_processor:
            mock_processor.process_event_rich.return_value = {"blocks": []}
            response = _post_shopify_webhook(
                client, workspace, "orders/create", payload
            )

        assert response.status_code == 200
        event_data = mock_processor.process_event_rich.call_args[0][0]
        prices = [item["price"] for item in event_data["metadata"]["line_items"]]
        assert prices == [Decimal("0"), Decimal("5.00")]


class TestShopifyEventTypeMapping:
    """Tests for the extended EVENT_TYPE_MAPPING."""

    @pytest.fixture
    def provider(self) -> ShopifySourcePlugin:
        """Create a ShopifySourcePlugin instance for testing."""
        return ShopifySourcePlugin(webhook_secret=WEBHOOK_SECRET)

    def test_orders_cancelled_maps_to_valid_event_type(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """orders/cancelled must map to a type the EventProcessor accepts."""
        event_type = provider.EVENT_TYPE_MAPPING["orders/cancelled"]
        assert event_type == "order_cancelled"
        assert event_type in EventProcessor.VALID_EVENT_TYPES

    def test_new_topics_map_to_valid_event_types(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """New topic mappings must all be valid EventProcessor types."""
        expected = {
            "orders/refunded": "refund_issued",
            "customers/create": "customer_created",
            "checkouts/create": "checkout_started",
        }
        for topic, event_type in expected.items():
            assert provider.EVENT_TYPE_MAPPING[topic] == event_type
            assert event_type in EventProcessor.VALID_EVENT_TYPES

    def test_all_mapped_event_types_are_valid(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """Every mapped event type (except test) must be processable."""
        for topic, event_type in provider.EVENT_TYPE_MAPPING.items():
            if topic == "test":
                continue
            assert event_type in EventProcessor.VALID_EVENT_TYPES, (
                f"{topic} maps to invalid event type {event_type}"
            )


class TestShopifyEventIdDedup:
    """Tests for surfacing X-Shopify-Webhook-Id as the router dedup key."""

    @pytest.fixture
    def provider(self) -> ShopifySourcePlugin:
        """Create a ShopifySourcePlugin instance for testing."""
        return ShopifySourcePlugin(webhook_secret=WEBHOOK_SECRET)

    def _make_request(self, headers: dict[str, str]) -> Mock:
        """Build a mock orders/create request with the given headers."""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {"X-Shopify-Topic": "orders/create", **headers}
        payload = {
            "id": 555,
            "order_number": 1020,
            "customer": {"id": 456},
            "total_price": "10.00",
        }
        mock_request.data = json.dumps(payload).encode()
        return mock_request

    def test_webhook_id_header_surfaced_as_event_id(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """X-Shopify-Webhook-Id is surfaced as event_id for router dedup."""
        request = self._make_request(
            {"X-Shopify-Webhook-Id": "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043"}
        )
        event = provider.parse_webhook(request)
        assert event is not None
        assert event["event_id"] == "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043"

    def test_missing_webhook_id_header_omits_event_id(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """Without the header, event_id is absent (router falls back)."""
        event = provider.parse_webhook(self._make_request({}))
        assert event is not None
        assert "event_id" not in event

    def test_fulfillment_webhook_also_surfaces_event_id(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """Fulfillment topic parsing also surfaces the delivery id."""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {
            "X-Shopify-Topic": "fulfillments/create",
            "X-Shopify-Webhook-Id": "delivery-123",
        }
        payload = {
            "id": 123456,
            "order_id": 789,
            "status": "success",
            "customer": {"id": 456},
        }
        mock_request.data = json.dumps(payload).encode()

        event = provider.parse_webhook(mock_request)
        assert event is not None
        assert event["event_id"] == "delivery-123"


class TestOrderCancelledNotification:
    """Tests that order_cancelled events build a real notification."""

    def test_build_rich_notification_for_order_cancelled(self) -> None:
        """EventProcessor must build a notification for order_cancelled."""
        processor = EventProcessor()
        event_data: dict[str, Any] = {
            "type": "order_cancelled",
            "customer_id": "456",
            "provider": "shopify",
            "external_id": "820982911946154508",
            "amount": Decimal("29.99"),
            "currency": "USD",
            "metadata": {"order_number": 1001},
        }
        customer_data: dict[str, Any] = {
            "email": "buyer@gmail.com",
            "first_name": "Test",
            "last_name": "User",
        }

        notification = processor.build_rich_notification(event_data, customer_data)

        assert notification.type == NotificationType.ORDER_CANCELLED
        assert notification.severity == NotificationSeverity.WARNING
        assert "1001" in notification.headline
        assert "canceled" in notification.headline.lower()


class TestGuestCheckoutCustomerId:
    """Tests for customer id extraction on order-scoped topics."""

    @pytest.fixture
    def provider(self) -> ShopifySourcePlugin:
        """Create a ShopifySourcePlugin instance for testing."""
        return ShopifySourcePlugin(webhook_secret=WEBHOOK_SECRET)

    def test_guest_checkout_null_customer_uses_email(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """Null customer falls back to the order email."""
        data = {"id": 789, "customer": None, "email": "guest@example.com"}
        result = provider._extract_shopify_customer_id(data, "orders/create")
        assert result == "guest@example.com"

    def test_guest_checkout_contact_email_fallback(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """contact_email is used when email is absent."""
        data = {"id": 789, "customer": None, "contact_email": "c@example.com"}
        result = provider._extract_shopify_customer_id(data, "orders/create")
        assert result == "c@example.com"

    def test_order_without_customer_never_returns_order_id(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """The order id must never be used as a customer identifier."""
        data = {"id": 789, "customer": None}
        result = provider._extract_shopify_customer_id(data, "orders/create")
        assert result is None
        assert result != "789"

    def test_customer_without_id_falls_back_for_order_topics(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """A customer dict without an id falls through to email."""
        data = {"id": 789, "customer": {"email": "x@example.com"}, "email": "x@e.com"}
        result = provider._extract_shopify_customer_id(data, "orders/paid")
        assert result == "x@e.com"


class TestConsolidationBucketIntegrity:
    """Tests that orders without customers do not poison consolidation."""

    def test_no_consolidation_bucket_without_customer_id(self) -> None:
        """No suppression/pending bucket may be created without a customer."""
        service = EventConsolidationService()

        with patch("webhooks.services.event_consolidation.cache") as mock_cache:
            mock_cache.get.return_value = None
            # order_created is a primary event that would normally mark
            # secondary events for suppression under the customer id
            result = service.should_send_notification(
                event_type="order_created",
                customer_id=None,  # type: ignore[arg-type]
                workspace_id="ws-1",
                amount=Decimal("10.00"),
            )

            assert result is True
            # No cache buckets were created for the missing customer
            mock_cache.set.assert_not_called()

    def test_order_id_never_used_as_bucket_key(self) -> None:
        """Parsing an order without customer yields no order-id bucket key."""
        provider = ShopifySourcePlugin(webhook_secret=WEBHOOK_SECRET)
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {"X-Shopify-Topic": "orders/create"}
        payload = {
            "id": 987654321,
            "order_number": 1010,
            "customer": None,
            "total_price": "10.00",
        }
        mock_request.data = json.dumps(payload).encode()

        event = provider.parse_webhook(mock_request)

        assert event is not None
        assert event["customer_id"] is None
        assert event["customer_id"] != str(payload["id"])


class TestDecimalPrecision:
    """Tests for Decimal-based monetary amounts in the Shopify parser."""

    @pytest.fixture
    def provider(self) -> ShopifySourcePlugin:
        """Create a ShopifySourcePlugin instance for testing."""
        return ShopifySourcePlugin(webhook_secret=WEBHOOK_SECRET)

    def test_line_item_sum_has_no_float_drift(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """0.10 + 0.20 must sum to exactly 0.30 (not 0.30000000000000004)."""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {"X-Shopify-Topic": "orders/create"}
        payload = {
            "id": 111,
            "order_number": 1011,
            "customer": {"id": 456},
            "total_price": "0.30",
            "currency": "USD",
            "line_items": [
                {"name": "Item A", "price": "0.10", "quantity": 1},
                {"name": "Item B", "price": "0.20", "quantity": 1},
            ],
        }
        mock_request.data = json.dumps(payload).encode()

        event = provider.parse_webhook(mock_request)

        assert event is not None
        total = sum(item["price"] for item in event["metadata"]["line_items"])
        assert total == Decimal("0.30")
        assert str(total) == "0.30"
        assert event["amount"] == Decimal("0.30")
        assert str(event["amount"]) == "0.30"

    def test_null_price_line_item_parses_to_zero(
        self, provider: ShopifySourcePlugin
    ) -> None:
        """Explicit null price parses to Decimal 0 instead of crashing."""
        line_items = provider._extract_line_items(
            {"line_items": [{"name": "Gift Card", "price": None, "quantity": 1}]}
        )
        assert line_items[0]["price"] == Decimal("0")


@pytest.mark.django_db
class TestShopifyOAuthCallbackSecret:
    """Tests for webhook secret handling in the OAuth callback."""

    @pytest.fixture
    def oauth_user(self, workspace: Workspace) -> Any:
        """Create a user with a profile bound to the workspace."""
        from core.models import UserProfile
        from django.contrib.auth.models import User

        user = User.objects.create_user(
            username="oauthuser",
            password="testpass123",
            email="oauth@example.com",
        )
        UserProfile.objects.create(user=user, workspace=workspace)
        return user

    def _start_callback(self, client: Client) -> None:
        """Store OAuth state in the session as shopify_connect would."""
        session = client.session
        session["shopify_oauth_state"] = "test_state"
        session["shopify_shop_domain"] = "teststore.myshopify.com"
        session.save()

    def _callback_params(self) -> dict[str, str]:
        """Return valid OAuth callback query parameters."""
        return {
            "code": "test_code",
            "state": "test_state",
            "shop": "teststore.myshopify.com",
        }

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_CLIENT_SECRET="",
    )
    def test_missing_client_secret_fails_early_and_preserves_secret(
        self,
        client: Client,
        workspace: Workspace,
        oauth_user: Any,
    ) -> None:
        """Unset SHOPIFY_CLIENT_SECRET fails the callback cleanly.

        The callback must not clear an existing integration's
        webhook_secret or create an integration with an empty secret.
        """
        from django.contrib.messages import get_messages
        from django.urls import reverse

        existing = Integration.objects.create(
            workspace=workspace,
            integration_type="shopify",
            webhook_secret="existing-secret",
            is_active=True,
        )

        client.force_login(oauth_user)
        self._start_callback(client)

        with patch("core.views.integrations.shopify.requests.post") as mock_post:
            response = client.get(
                reverse("core:shopify_connect_callback"),
                self._callback_params(),
            )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        messages = list(get_messages(response.wsgi_request))
        assert any("not configured" in str(m).lower() for m in messages)

        # No token exchange or webhook creation was attempted
        mock_post.assert_not_called()

        # The existing webhook secret was not cleared
        existing.refresh_from_db()
        assert existing.webhook_secret == "existing-secret"

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_CLIENT_SECRET="oauth-app-secret",
        SHOPIFY_API_VERSION="2025-01",
        BASE_URL="http://localhost:8000",
    )
    def test_successful_callback_stores_webhook_secret(
        self,
        client: Client,
        workspace: Workspace,
        oauth_user: Any,
    ) -> None:
        """A successful OAuth callback stores the client secret."""
        from django.urls import reverse

        token_response = Mock()
        token_response.status_code = 200
        token_response.raise_for_status = Mock()
        token_response.json.return_value = {
            "access_token": "test_access_token",
            "scope": "read_orders,read_customers",
        }
        webhook_response = Mock()
        webhook_response.status_code = 201
        webhook_response.json.return_value = {"webhook": {"id": 12345}}

        client.force_login(oauth_user)
        self._start_callback(client)

        with patch("core.views.integrations.shopify.requests.post") as mock_post:
            mock_post.side_effect = [token_response] + [webhook_response] * 10
            response = client.get(
                reverse("core:shopify_connect_callback"),
                self._callback_params(),
            )

        assert response.status_code == 302
        integration = Integration.objects.get(
            workspace=workspace, integration_type="shopify"
        )
        assert integration.is_active is True
        # Webhook secret must be stored so HMAC validation can succeed
        assert integration.webhook_secret == "oauth-app-secret"


def _get_backfill() -> Any:
    """Import the backfill function from the data migration module."""
    from importlib import import_module

    module = import_module("core.migrations.0020_backfill_shopify_webhook_secret")
    return module.backfill_shopify_webhook_secret


@pytest.mark.django_db
class TestWebhookSecretBackfillMigration:
    """Tests for the 0020 data migration backfilling webhook secrets."""

    @override_settings(SHOPIFY_CLIENT_SECRET="app-client-secret")
    def test_backfill_only_touches_empty_shopify_secrets(self) -> None:
        """Only Shopify rows with empty webhook_secret are backfilled."""
        workspace_a = Workspace.objects.create(name="A")
        workspace_b = Workspace.objects.create(name="B")
        empty_shopify = Integration.objects.create(
            workspace=workspace_a,
            integration_type="shopify",
            webhook_secret="",
            is_active=True,
        )
        populated_shopify = Integration.objects.create(
            workspace=workspace_b,
            integration_type="shopify",
            webhook_secret="existing-secret",
            is_active=True,
        )
        chargify = Integration.objects.create(
            workspace=workspace_a,
            integration_type="chargify",
            webhook_secret="",
            is_active=True,
        )

        _get_backfill()(django_apps, None)

        empty_shopify.refresh_from_db()
        populated_shopify.refresh_from_db()
        chargify.refresh_from_db()
        assert empty_shopify.webhook_secret == "app-client-secret"
        assert populated_shopify.webhook_secret == "existing-secret"
        assert chargify.webhook_secret == ""

    @override_settings(SHOPIFY_CLIENT_SECRET="app-client-secret")
    def test_backfill_is_idempotent(self, workspace: Workspace) -> None:
        """Running the backfill twice yields the same result."""
        empty = Integration.objects.create(
            workspace=workspace,
            integration_type="shopify",
            webhook_secret="",
            is_active=True,
        )

        backfill = _get_backfill()
        backfill(django_apps, None)
        backfill(django_apps, None)

        empty.refresh_from_db()
        assert empty.webhook_secret == "app-client-secret"

    @override_settings(SHOPIFY_CLIENT_SECRET="")
    def test_backfill_noop_without_configured_secret(
        self, workspace: Workspace
    ) -> None:
        """Backfill is a no-op when SHOPIFY_CLIENT_SECRET is unset."""
        empty = Integration.objects.create(
            workspace=workspace,
            integration_type="shopify",
            webhook_secret="",
            is_active=True,
        )

        _get_backfill()(django_apps, None)

        empty.refresh_from_db()
        assert empty.webhook_secret == ""

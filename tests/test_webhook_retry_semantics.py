"""Tests for webhook dedup and retry semantics.

Verifies that webhooks are never silently lost:
- The dedup marker is written only after successful queue/dispatch, so a
  failed dispatch is retried by the provider instead of being suppressed.
- Deduplication is keyed on the provider event id (or a composite of
  event type and object id), so distinct events for the same object
  both process.
- Delivery and billing failures surface as 5xx so providers redeliver.
- Chargify webhooks without an id are rejected instead of bypassing dedup.
"""

import json
from typing import Any, Generator
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlencode

import pytest
from core.models import GlobalBillingIntegration, Integration, Workspace
from django.test import Client
from plugins.destinations.base import BaseDestinationPlugin
from webhooks.webhook_router import _get_dedup_key, _process_webhook_data


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


@pytest.fixture
def mock_provider() -> Mock:
    """Create a mock source provider for router-level tests.

    Returns:
        Mock provider with empty customer data.
    """
    provider = Mock()
    provider.get_customer_data.return_value = {}
    return provider


class TestDedupKey:
    """Tests for the deduplication key derivation."""

    def test_prefers_event_id(self) -> None:
        """The provider event id wins over the object id."""
        event_data = {
            "type": "subscription_created",
            "event_id": "evt_123",
            "external_id": "sub_ABC",
        }
        assert _get_dedup_key(event_data) == "evt_123"

    def test_falls_back_to_composite_key(self) -> None:
        """Without an event id, the key combines type and object id."""
        event_data = {"type": "subscription_created", "external_id": "sub_ABC"}
        assert _get_dedup_key(event_data) == "subscription_created:sub_ABC"

    def test_empty_without_identifiers(self) -> None:
        """No identifiers means no dedup key (dedup disabled)."""
        assert _get_dedup_key({"type": "payment_success"}) == ""


class TestDedupMarkerAfterDispatch:
    """Finding 1: dedup marker must be written only after dispatch."""

    def _event_data(self) -> dict[str, Any]:
        """Build a Stripe-style event with an idempotency key."""
        return {
            "type": "payment_success",
            "customer_id": "cus_1",
            "external_id": "in_1",
            "event_id": "evt_1",
            "idempotency_key": "idem_1",
            "amount": 10.0,
        }

    def test_failed_queue_is_not_dedup_marked(
        self, mock_consolidation_cache: dict, mock_provider: Mock
    ) -> None:
        """An event whose dispatch raises is retried, not dedup-suppressed."""
        with patch(
            "webhooks.webhook_router.pending_event_queue.queue_event",
            side_effect=RuntimeError("redis unavailable"),
        ):
            with pytest.raises(RuntimeError):
                _process_webhook_data(
                    self._event_data(), mock_provider, "customer_stripe", None
                )

        # The provider retry (same event) must process, not be suppressed
        with patch(
            "webhooks.webhook_router.pending_event_queue.queue_event"
        ) as mock_queue:
            response = _process_webhook_data(
                self._event_data(), mock_provider, "customer_stripe", None
            )

        assert response.status_code == 200
        assert "queued" in json.loads(response.content)["message"]
        mock_queue.assert_called_once()

    def test_successful_queue_is_dedup_marked(
        self, mock_consolidation_cache: dict, mock_provider: Mock
    ) -> None:
        """After a successful dispatch, a redelivery is suppressed."""
        with patch("webhooks.webhook_router.pending_event_queue.queue_event"):
            first = _process_webhook_data(
                self._event_data(), mock_provider, "customer_stripe", None
            )
            second = _process_webhook_data(
                self._event_data(), mock_provider, "customer_stripe", None
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert "duplicate suppressed" in json.loads(second.content)["message"]


class TestDistinctEventsSameObject:
    """Finding 2: dedup must not collide distinct events for one object."""

    @patch("django.conf.settings.EVENT_PROCESSOR")
    def test_created_then_updated_both_process(
        self,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        mock_provider: Mock,
    ) -> None:
        """subscription.created and .updated for one subscription both process."""
        mock_processor.process_event_rich.return_value = {"blocks": []}

        created = {
            "type": "subscription_created",
            "customer_id": "cus_1",
            "external_id": "sub_ABC",
            "event_id": "evt_created_1",
            "idempotency_key": None,
            "amount": 26.60,
        }
        updated = {
            "type": "subscription_updated",
            "customer_id": "cus_1",
            "external_id": "sub_ABC",
            "event_id": "evt_updated_2",
            "idempotency_key": None,
            "amount": 49.00,
        }

        with patch("webhooks.webhook_router.pending_event_queue.queue_event"):
            first = _process_webhook_data(
                created, mock_provider, "customer_stripe", None
            )
            second = _process_webhook_data(
                updated, mock_provider, "customer_stripe", None
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert "duplicate" not in json.loads(second.content)["message"]

    @pytest.mark.django_db
    def test_stripe_parser_surfaces_event_id(self) -> None:
        """parse_webhook exposes the evt_... id separately from external_id."""
        from plugins.sources.stripe import StripeSourcePlugin

        plugin = StripeSourcePlugin(webhook_secret="whsec_test")

        mock_event = Mock()
        mock_event.id = "evt_outer_123"
        mock_event.type = "customer.subscription.updated"
        mock_event.data.object = {
            "id": "sub_ABC",
            "customer": "cus_1",
            "status": "active",
            "plan": {"amount": 4900, "interval": "month"},
        }
        mock_event.data.previous_attributes = None
        mock_event.request = None

        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.POST = None
        mock_request.headers = {"Stripe-Signature": "sig"}
        mock_request.body = b"{}"

        with patch(
            "plugins.sources.stripe.stripe.Webhook.construct_event",
            return_value=mock_event,
        ):
            event_data = plugin.parse_webhook(mock_request)

        assert event_data is not None
        assert event_data["event_id"] == "evt_outer_123"
        assert event_data["external_id"] == "sub_ABC"


@pytest.mark.django_db
class TestImmediateDeliveryFailureReturns5xx:
    """Finding 3: Slack delivery failures must return 5xx, not 200."""

    @pytest.fixture
    def workspace(self) -> Workspace:
        """Create a workspace with Chargify and Slack integrations."""
        workspace = Workspace.objects.create(
            name="Test Workspace", shop_domain="test.myshopify.com"
        )
        Integration.objects.create(
            workspace=workspace,
            integration_type="chargify",
            webhook_secret="test-webhook-secret",
            is_active=True,
        )
        Integration.objects.create(
            workspace=workspace,
            integration_type="slack_notifications",
            is_active=True,
            oauth_credentials={
                "incoming_webhook": {"url": "https://hooks.slack.com/test"}
            },
        )
        return workspace

    def _post_chargify(self, client: Client, workspace: Workspace) -> Any:
        """Send a valid Chargify payment_success webhook."""
        return client.post(
            f"/webhook/customer/{workspace.uuid}/chargify/",
            data=urlencode(
                {
                    "event": "payment_success",
                    "payload[subscription][id]": "sub_789",
                    "payload[subscription][customer][id]": "cust_123",
                    "payload[subscription][customer][email]": "test@example.com",
                    "payload[subscription][product][name]": "Premium Plan",
                    "payload[transaction][id]": "txn_1",
                    "payload[transaction][amount_in_cents]": "2999",
                    "created_at": "2024-03-15T10:00:00Z",
                }
            ),
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_retry_1",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256="sig",
        )

    @patch("django.conf.settings.EVENT_PROCESSOR")
    @patch("plugins.sources.chargify.ChargifySourcePlugin.validate_webhook")
    def test_slack_failure_returns_5xx_then_retry_processes(
        self,
        mock_validate: Mock,
        mock_processor: Mock,
        mock_consolidation_cache: dict,
        client: Client,
        workspace: Workspace,
    ) -> None:
        """A failed Slack send returns 5xx; the retry is not dedup'd."""
        mock_validate.return_value = True
        mock_processor.process_event_rich.return_value = {"blocks": []}

        slack_plugin = MagicMock(spec=BaseDestinationPlugin)
        mock_registry = Mock()
        mock_registry.get.return_value = slack_plugin

        with patch(
            "plugins.registry.PluginRegistry.instance", return_value=mock_registry
        ):
            # First delivery: Slack is down -> 5xx so Chargify retries
            slack_plugin.send.side_effect = Exception("slack webhook revoked")
            first = self._post_chargify(client, workspace)
            assert first.status_code == 500

            # Retry: Slack is back -> processed, not suppressed as duplicate
            slack_plugin.send.side_effect = None
            second = self._post_chargify(client, workspace)
            assert second.status_code == 200
            assert "successfully" in second.json()["message"]

            # Third delivery of the same webhook id is now a duplicate
            third = self._post_chargify(client, workspace)
            assert third.status_code == 200
            assert "duplicate suppressed" in third.json()["message"]

        assert slack_plugin.send.call_count == 2


@pytest.mark.django_db
class TestBillingWebhookPropagatesDbErrors:
    """Finding 5: billing handler errors must surface as 5xx."""

    @pytest.fixture(autouse=True)
    def _enable_billing(self, settings) -> None:
        """Enable billing: the endpoint 404s when DISABLE_BILLING is set."""
        settings.DISABLE_BILLING = False

    @pytest.fixture
    def billing_integration(self) -> GlobalBillingIntegration:
        """Create an active global billing integration."""
        GlobalBillingIntegration.objects.filter(
            integration_type="stripe_billing"
        ).delete()
        return GlobalBillingIntegration.objects.create(
            integration_type="stripe_billing",
            webhook_secret="whsec_test_secret",
            is_active=True,
        )

    def test_db_error_in_subscription_updated_returns_5xx(
        self, client: Client, billing_integration: GlobalBillingIntegration
    ) -> None:
        """A DB error inside handle_subscription_updated returns 5xx."""
        from django.db import OperationalError

        mock_event = Mock()
        mock_event.id = "evt_billing_1"
        mock_event.type = "customer.subscription.updated"
        mock_event.data.object = {
            "id": "sub_1",
            "customer": "cus_1",
            "status": "active",
            "plan": {"amount": 999, "interval": "month"},
        }
        mock_event.data.previous_attributes = None
        mock_event.request = None

        with (
            patch(
                "plugins.sources.stripe.StripeSourcePlugin.validate_webhook",
                return_value=True,
            ),
            patch(
                "plugins.sources.stripe.stripe.Webhook.construct_event",
                return_value=mock_event,
            ),
            patch(
                "webhooks.services.billing.Workspace.objects.filter",
                side_effect=OperationalError("connection lost"),
            ),
        ):
            response = client.post(
                "/webhook/billing/stripe/",
                data=json.dumps({"type": "customer.subscription.updated"}),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="sig",
            )

        assert response.status_code == 500


@pytest.mark.django_db
class TestChargifyMissingWebhookId:
    """Finding 7: a missing X-Chargify-Webhook-Id must return 400."""

    @pytest.fixture
    def workspace(self) -> Workspace:
        """Create a workspace with an active Chargify integration."""
        workspace = Workspace.objects.create(
            name="Test Workspace", shop_domain="test.myshopify.com"
        )
        Integration.objects.create(
            workspace=workspace,
            integration_type="chargify",
            webhook_secret="test-webhook-secret",
            is_active=True,
        )
        return workspace

    @patch("plugins.sources.chargify.ChargifySourcePlugin.validate_webhook")
    def test_missing_webhook_id_returns_400(
        self, mock_validate: Mock, client: Client, workspace: Workspace
    ) -> None:
        """A webhook without an id has no dedup key and is rejected."""
        mock_validate.return_value = True

        response = client.post(
            f"/webhook/customer/{workspace.uuid}/chargify/",
            data=urlencode(
                {
                    "event": "payment_success",
                    "payload[subscription][customer][id]": "cust_123",
                    "payload[transaction][amount_in_cents]": "2999",
                    "created_at": "2024-03-15T10:00:00Z",
                }
            ),
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256="sig",
        )

        assert response.status_code == 400
        assert "X-Chargify-Webhook-Id" in response.json()["message"]

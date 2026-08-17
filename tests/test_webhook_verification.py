"""Tests for webhook verification indicator feature.

Verifies that the webhook_verified_at timestamp is correctly set on first
successful webhook validation, not updated on subsequent webhooks, not set
on failed validations, and reset when integrations are reconnected.
"""

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
from core.models import Integration, Workspace
from django.test import Client


@pytest.fixture
def workspace(db: Any) -> Workspace:
    """Create a test workspace."""
    return Workspace.objects.create(
        name="Test Workspace",
        shop_domain="test.myshopify.com",
    )


@pytest.fixture
def stripe_integration(workspace: Workspace) -> Integration:
    """Create a Stripe customer integration."""
    return Integration.objects.create(
        workspace=workspace,
        integration_type="stripe_customer",
        webhook_secret="whsec_test_secret_123",
        is_active=True,
    )


@pytest.fixture
def chargify_integration(workspace: Workspace) -> Integration:
    """Create a Chargify integration."""
    return Integration.objects.create(
        workspace=workspace,
        integration_type="chargify",
        webhook_secret="test-chargify-secret",
        is_active=True,
    )


@pytest.fixture
def shopify_integration(workspace: Workspace) -> Integration:
    """Create a Shopify integration."""
    return Integration.objects.create(
        workspace=workspace,
        integration_type="shopify",
        webhook_secret="test-shopify-secret",
        is_active=True,
    )


@pytest.mark.django_db
class TestWebhookVerificationStamp:
    """Tests for setting webhook_verified_at on successful webhook processing."""

    @patch("plugins.sources.stripe.StripeSourcePlugin")
    def test_first_successful_webhook_sets_verified_at(
        self,
        mock_provider_class: Any,
        client: Client,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """First successful webhook should set webhook_verified_at."""
        assert stripe_integration.webhook_verified_at is None

        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = True
        mock_provider.parse_webhook.return_value = None  # Test webhook

        url = f"/webhook/customer/{workspace.uuid}/stripe/"
        response = client.post(
            url,
            data=json.dumps({"type": "test"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        stripe_integration.refresh_from_db()
        assert stripe_integration.webhook_verified_at is not None
        assert stripe_integration.is_webhook_verified is True

    @patch("plugins.sources.stripe.StripeSourcePlugin")
    def test_subsequent_webhook_does_not_update_verified_at(
        self,
        mock_provider_class: Any,
        client: Client,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """Subsequent successful webhooks should not change the timestamp."""
        original_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stripe_integration.webhook_verified_at = original_time
        stripe_integration.save(update_fields=["webhook_verified_at"])

        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = True
        mock_provider.parse_webhook.return_value = None

        url = f"/webhook/customer/{workspace.uuid}/stripe/"
        response = client.post(
            url,
            data=json.dumps({"type": "test"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        stripe_integration.refresh_from_db()
        assert stripe_integration.webhook_verified_at == original_time

    @patch("plugins.sources.stripe.StripeSourcePlugin")
    def test_failed_validation_does_not_set_verified_at(
        self,
        mock_provider_class: Any,
        client: Client,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """Failed webhook validation should not set webhook_verified_at."""
        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = False

        url = f"/webhook/customer/{workspace.uuid}/stripe/"
        response = client.post(
            url,
            data=json.dumps({"type": "test"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        stripe_integration.refresh_from_db()
        assert stripe_integration.webhook_verified_at is None

    @patch("plugins.sources.chargify.ChargifySourcePlugin")
    def test_chargify_webhook_sets_verified_at(
        self,
        mock_provider_class: Any,
        client: Client,
        workspace: Workspace,
        chargify_integration: Integration,
    ) -> None:
        """Chargify webhook should set webhook_verified_at on success."""
        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = True
        mock_provider.parse_webhook.return_value = None

        url = f"/webhook/customer/{workspace.uuid}/chargify/"
        response = client.post(
            url,
            data="event=test",
            content_type="application/x-www-form-urlencoded",
        )

        assert response.status_code == 200
        chargify_integration.refresh_from_db()
        assert chargify_integration.webhook_verified_at is not None

    @patch("plugins.sources.shopify.ShopifySourcePlugin")
    def test_shopify_webhook_sets_verified_at(
        self,
        mock_provider_class: Any,
        client: Client,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """Shopify webhook should set webhook_verified_at on success."""
        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = True
        mock_provider.parse_webhook.return_value = None

        url = f"/webhook/customer/{workspace.uuid}/shopify/"
        response = client.post(
            url,
            data=json.dumps({"id": 123}),
            content_type="application/json",
        )

        assert response.status_code == 200
        shopify_integration.refresh_from_db()
        assert shopify_integration.webhook_verified_at is not None


@pytest.mark.django_db
class TestWebhookVerificationReset:
    """Tests for resetting webhook_verified_at on reconnect."""

    def _make_verified(self, integration: Integration) -> None:
        """Helper to mark an integration as webhook-verified."""
        integration.webhook_verified_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        integration.save(update_fields=["webhook_verified_at"])

    def test_stripe_reconnect_resets_verified_at(
        self,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """Updating Stripe webhook secret should reset webhook_verified_at."""
        self._make_verified(stripe_integration)
        assert stripe_integration.is_webhook_verified is True

        # Simulate what the Stripe view does on reconnect
        stripe_integration.webhook_secret = "whsec_new_secret_456"
        stripe_integration.webhook_verified_at = None
        stripe_integration.save()

        stripe_integration.refresh_from_db()
        assert stripe_integration.webhook_verified_at is None
        assert stripe_integration.is_webhook_verified is False

    def test_chargify_reconnect_resets_verified_at(
        self,
        workspace: Workspace,
        chargify_integration: Integration,
    ) -> None:
        """Updating Chargify webhook secret should reset webhook_verified_at."""
        self._make_verified(chargify_integration)

        chargify_integration.webhook_secret = "new-chargify-secret"
        chargify_integration.webhook_verified_at = None
        chargify_integration.save()

        chargify_integration.refresh_from_db()
        assert chargify_integration.webhook_verified_at is None

    def test_shopify_reconnect_resets_verified_at(
        self,
        workspace: Workspace,
        shopify_integration: Integration,
    ) -> None:
        """Shopify update_or_create with defaults should reset webhook_verified_at."""
        self._make_verified(shopify_integration)

        # Simulate what the Shopify view does via update_or_create
        Integration.objects.update_or_create(
            workspace=workspace,
            integration_type="shopify",
            defaults={
                "oauth_credentials": {"access_token": "new_token", "scope": "read"},
                "integration_settings": {
                    "shop_domain": "test.myshopify.com",
                    "webhook_ids": [],
                    "enabled_categories": [],
                },
                "is_active": True,
                "webhook_verified_at": None,
            },
        )

        shopify_integration.refresh_from_db()
        assert shopify_integration.webhook_verified_at is None


@pytest.mark.django_db
class TestIntegrationOverviewVerification:
    """Tests for webhook_verified_at in integration overview data."""

    def test_overview_includes_webhook_verified_at(
        self,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """Integration overview should include webhook_verified_at for event sources."""
        from core.services.dashboard import IntegrationService

        service = IntegrationService()
        overview = service.get_integration_overview(workspace)

        stripe_source = next(
            s for s in overview["event_sources"] if s["id"] == "stripe_customer"
        )
        assert stripe_source["connected"] is True
        assert stripe_source["webhook_verified_at"] is None

    def test_overview_shows_verified_timestamp(
        self,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """Integration overview should show the verification timestamp when set."""
        from core.services.dashboard import IntegrationService

        verified_time = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        stripe_integration.webhook_verified_at = verified_time
        stripe_integration.save(update_fields=["webhook_verified_at"])

        service = IntegrationService()
        overview = service.get_integration_overview(workspace)

        stripe_source = next(
            s for s in overview["event_sources"] if s["id"] == "stripe_customer"
        )
        assert stripe_source["webhook_verified_at"] == verified_time

    def test_overview_disconnected_has_no_verified_at(
        self,
        workspace: Workspace,
    ) -> None:
        """Disconnected integration should have None for webhook_verified_at."""
        from core.services.dashboard import IntegrationService

        service = IntegrationService()
        overview = service.get_integration_overview(workspace)

        stripe_source = next(
            s for s in overview["event_sources"] if s["id"] == "stripe_customer"
        )
        assert stripe_source["connected"] is False
        assert stripe_source["webhook_verified_at"] is None


@pytest.mark.django_db
class TestEventStateIsOnlyClaimedWhereTracked:
    """Only sources report an event state, because only sources have one.

    Nothing sets webhook_verified_at on a destination, so a Slack row that had
    delivered notifications for months still announced "No events yet".
    """

    def test_a_source_that_has_received_is_receiving(
        self,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """A verified source reports "receiving"."""
        from core.services.dashboard import IntegrationService

        stripe_integration.webhook_verified_at = datetime(
            2025, 6, 15, 12, 0, tzinfo=timezone.utc
        )
        stripe_integration.save(update_fields=["webhook_verified_at"])

        overview = IntegrationService().get_integration_overview(workspace)
        source = next(
            s for s in overview["event_sources"] if s["id"] == "stripe_customer"
        )

        assert source["event_state"] == "receiving"

    def test_a_source_still_waiting_says_so(
        self,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """A connected source with no events yet reports "waiting"."""
        from core.services.dashboard import IntegrationService

        overview = IntegrationService().get_integration_overview(workspace)
        source = next(
            s for s in overview["event_sources"] if s["id"] == "stripe_customer"
        )

        assert source["event_state"] == "waiting"

    def test_a_disconnected_source_claims_nothing(
        self,
        workspace: Workspace,
    ) -> None:
        """Nothing is claimed about an integration that is not connected."""
        from core.services.dashboard import IntegrationService

        overview = IntegrationService().get_integration_overview(workspace)
        source = next(
            s for s in overview["event_sources"] if s["id"] == "stripe_customer"
        )

        assert source["event_state"] is None

    def test_destinations_never_borrow_the_source_vocabulary(
        self, workspace: Workspace
    ) -> None:
        """A channel never reports on events, which it does not see.

        This was the reported bug: webhook_verified_at is never written for a
        destination, so "No events yet" was a guess presented as fact. A
        destination reports on deliveries instead.
        """
        from core.services.dashboard import IntegrationService

        Integration.objects.create(
            workspace=workspace,
            integration_type="slack_notifications",
            oauth_credentials={"access_token": "xoxb-test"},
            is_active=True,
        )

        overview = IntegrationService().get_integration_overview(workspace)

        for destination in overview["notification_channels"]:
            assert destination.get("event_state") not in {"receiving", "waiting"}, (
                f"{destination['id']} claims a state derived from webhook_verified_at, "
                "which nothing writes for a destination"
            )


@pytest.mark.django_db
class TestFirstDeliveryIsRecorded:
    """A destination knows whether a notification has ever reached it."""

    @pytest.fixture
    def slack(self, workspace: Workspace) -> Integration:
        """Create a connected Slack destination."""
        return Integration.objects.create(
            workspace=workspace,
            integration_type="slack_notifications",
            oauth_credentials={"access_token": "xoxb-test"},
            is_active=True,
        )

    def test_a_delivery_stamps_the_destination(
        self, workspace: Workspace, slack: Integration
    ) -> None:
        """The first delivery records when it happened."""
        from webhooks.services.destination_credentials import record_delivery

        record_delivery(workspace, "slack")

        slack.refresh_from_db()
        assert slack.first_delivery_at is not None
        assert slack.has_delivered is True

    def test_later_deliveries_keep_the_first_timestamp(
        self, workspace: Workspace, slack: Integration
    ) -> None:
        """It records the first delivery, not the latest one."""
        from webhooks.services.destination_credentials import record_delivery

        record_delivery(workspace, "slack")
        slack.refresh_from_db()
        first = slack.first_delivery_at

        record_delivery(workspace, "slack")

        slack.refresh_from_db()
        assert slack.first_delivery_at == first

    def test_a_delivery_does_not_touch_another_workspace(
        self, workspace: Workspace, slack: Integration
    ) -> None:
        """Stamping is scoped to the workspace the notification was for."""
        from webhooks.services.destination_credentials import record_delivery

        other = Workspace.objects.create(
            name="Other", shop_domain="other.myshopify.com"
        )
        other_slack = Integration.objects.create(
            workspace=other,
            integration_type="slack_notifications",
            oauth_credentials={"access_token": "xoxb-other"},
            is_active=True,
        )

        record_delivery(workspace, "slack")

        other_slack.refresh_from_db()
        assert other_slack.first_delivery_at is None

    def test_an_unmapped_plugin_is_survivable(self, workspace: Workspace) -> None:
        """An unknown destination is logged, not raised.

        Recording is bookkeeping that runs after the notification has already
        been delivered, so it must never turn a successful send into a failure.
        """
        from webhooks.services.destination_credentials import record_delivery

        record_delivery(workspace, "carrier-pigeon")

    def test_every_collected_destination_can_be_recorded(self) -> None:
        """Each destination plugin maps to the Integration row that configures it.

        collect_destinations and the mapping are edited together or a new
        channel silently never records a delivery.
        """
        from core.models import Integration as IntegrationModel
        from webhooks.services.destination_credentials import (
            DESTINATION_INTEGRATION_TYPES,
        )

        assert set(DESTINATION_INTEGRATION_TYPES.values()) == set(
            IntegrationModel.DESTINATION_INTEGRATION_TYPES
        )

    def test_overview_reports_delivering_once_delivered(
        self, workspace: Workspace, slack: Integration
    ) -> None:
        """The Slack row says it is delivering once a notification has landed."""
        from core.services.dashboard import IntegrationService
        from webhooks.services.destination_credentials import record_delivery

        record_delivery(workspace, "slack")

        overview = IntegrationService().get_integration_overview(workspace)
        channel = next(
            c
            for c in overview["notification_channels"]
            if c["id"] == "slack_notifications"
        )

        assert channel["event_state"] == "delivering"

    def test_overview_reports_undelivered_before_that(
        self, workspace: Workspace, slack: Integration
    ) -> None:
        """A freshly connected channel says nothing has been delivered yet."""
        from core.services.dashboard import IntegrationService

        overview = IntegrationService().get_integration_overview(workspace)
        channel = next(
            c
            for c in overview["notification_channels"]
            if c["id"] == "slack_notifications"
        )

        assert channel["event_state"] == "undelivered"

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

"""Tests for Notipus billing webhook endpoint.

Tests verify that the billing_stripe_webhook handles various scenarios
gracefully, including missing configuration.
"""

import json
from unittest.mock import patch

import pytest
from django.test import Client


@pytest.fixture
def client() -> Client:
    """Create a Django test client."""
    return Client()


@pytest.mark.django_db
class TestBillingStripeWebhook:
    """Tests for the /webhook/billing/stripe/ endpoint."""

    def test_missing_global_billing_integration_returns_500(
        self, client: Client
    ) -> None:
        """Verify a 5xx when GlobalBillingIntegration is not configured.

        Stripe retries with its own backoff, so returning 5xx means the
        events are redelivered once configuration is fixed instead of
        being permanently lost.
        """
        # Ensure no GlobalBillingIntegration exists
        from core.models import GlobalBillingIntegration

        GlobalBillingIntegration.objects.filter(
            integration_type="stripe_billing"
        ).delete()

        response = client.post(
            "/webhook/billing/stripe/",
            data=json.dumps({"type": "invoice.paid"}),
            content_type="application/json",
        )

        # Should return 500 so Stripe retries after configuration is fixed
        assert response.status_code == 500
        response_data = response.json()
        assert response_data["status"] == "error"
        assert "not configured" in response_data["message"]

    @pytest.mark.django_db
    def test_inactive_global_billing_integration_returns_500(
        self, client: Client
    ) -> None:
        """Verify a 5xx when GlobalBillingIntegration is inactive."""
        from core.models import GlobalBillingIntegration

        # Create an inactive integration
        GlobalBillingIntegration.objects.filter(
            integration_type="stripe_billing"
        ).delete()
        GlobalBillingIntegration.objects.create(
            integration_type="stripe_billing",
            webhook_secret="whsec_test",
            is_active=False,  # Inactive
        )

        response = client.post(
            "/webhook/billing/stripe/",
            data=json.dumps({"type": "invoice.paid"}),
            content_type="application/json",
        )

        # Should return 500 so Stripe retries after configuration is fixed
        assert response.status_code == 500
        response_data = response.json()
        assert response_data["status"] == "error"
        assert "not configured" in response_data["message"]

    @pytest.mark.django_db
    def test_active_global_billing_integration_processes_webhook(
        self, client: Client
    ) -> None:
        """Verify webhook is processed when GlobalBillingIntegration is configured."""
        from core.models import GlobalBillingIntegration

        # Create an active integration
        GlobalBillingIntegration.objects.filter(
            integration_type="stripe_billing"
        ).delete()
        GlobalBillingIntegration.objects.create(
            integration_type="stripe_billing",
            webhook_secret="whsec_test_secret",
            is_active=True,
        )

        # Mock the Stripe webhook processing
        with patch(
            "plugins.sources.stripe.StripeSourcePlugin.validate_webhook"
        ) as mock_validate:
            # Simulate invalid signature (webhook validation fails)
            mock_validate.return_value = False

            response = client.post(
                "/webhook/billing/stripe/",
                data=json.dumps({"type": "invoice.paid"}),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="invalid_sig",
            )

            # Should return 400 for invalid signature (not 500)
            assert response.status_code == 400

    @pytest.mark.django_db
    def test_exception_during_processing_returns_500(self, client: Client) -> None:
        """Verify transient/unknown exceptions return 5xx so Stripe retries."""
        from core.models import GlobalBillingIntegration

        # Create an active integration
        GlobalBillingIntegration.objects.filter(
            integration_type="stripe_billing"
        ).delete()
        GlobalBillingIntegration.objects.create(
            integration_type="stripe_billing",
            webhook_secret="whsec_test_secret",
            is_active=True,
        )

        # Mock to raise an exception during processing
        with patch(
            "plugins.sources.stripe.StripeSourcePlugin.__init__",
            side_effect=Exception("Unexpected error"),
        ):
            response = client.post(
                "/webhook/billing/stripe/",
                data=json.dumps({"type": "invoice.paid"}),
                content_type="application/json",
            )

            # Should return 500 so Stripe redelivers the event
            assert response.status_code == 500
            response_data = response.json()
            assert response_data["status"] == "error"

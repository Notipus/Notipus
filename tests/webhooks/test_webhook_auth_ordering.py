"""Tests for webhook authentication ordering (issue #75).

Signature validation must run before any side effects: rate limiting
(quota billing), payload logging, and Redis storage. Unauthenticated
requests must not consume workspace quota, must not persist payloads,
and must never leak signature values into logs.
"""

import json
import logging
from typing import Any
from unittest.mock import patch

import pytest
from core.models import Integration, Workspace
from django.test import Client
from webhooks.services.rate_limiter import rate_limiter
from webhooks.services.webhook_storage import webhook_storage_service


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


def _all_captured_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Collect all captured log output including extra record attributes."""
    parts = [caplog.text]
    parts.extend(str(vars(record)) for record in caplog.records)
    return "\n".join(parts)


@pytest.mark.django_db
class TestWebhookAuthOrdering:
    """Signature validation must precede rate limiting, logging, storage."""

    def test_unsigned_webhook_does_not_consume_quota_or_store(
        self,
        client: Client,
        workspace: Workspace,
        stripe_integration: Integration,
        settings: Any,
    ) -> None:
        """An empty, unsigned POST must not bill quota or persist a payload."""
        settings.LOG_WEBHOOKS = True

        url = f"/webhook/customer/{workspace.uuid}/stripe/"
        with (
            patch.object(rate_limiter, "enforce_rate_limit") as mock_enforce,
            patch.object(rate_limiter, "increment_usage") as mock_increment,
            patch.object(webhook_storage_service, "store_webhook") as mock_store,
        ):
            response = client.post(url, data="", content_type="application/json")

        assert response.status_code == 400
        mock_enforce.assert_not_called()
        mock_increment.assert_not_called()
        mock_store.assert_not_called()

    def test_invalid_stripe_signature_value_not_logged(
        self,
        client: Client,
        workspace: Workspace,
        stripe_integration: Integration,
        settings: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An invalid Stripe-Signature value must never appear in logs."""
        settings.LOG_WEBHOOKS = True
        settings.DISABLE_BILLING = False

        signature_canary = "t=1234567890,v1=SIGLEAKCANARYDEADBEEF"
        url = f"/webhook/customer/{workspace.uuid}/stripe/"
        with caplog.at_level(logging.DEBUG):
            response = client.post(
                url,
                data=json.dumps({"type": "invoice.paid"}),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=signature_canary,
            )

        assert response.status_code == 400
        assert "SIGLEAKCANARYDEADBEEF" not in _all_captured_log_text(caplog)

    @patch("plugins.sources.stripe.StripeSourcePlugin")
    def test_valid_webhook_consumes_exactly_one_quota_unit(
        self,
        mock_provider_class: Any,
        client: Client,
        workspace: Workspace,
        stripe_integration: Integration,
    ) -> None:
        """An accepted webhook must increment rate-limit usage exactly once."""
        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = True
        mock_provider.parse_webhook.return_value = None  # Test webhook

        url = f"/webhook/customer/{workspace.uuid}/stripe/"
        with patch.object(
            rate_limiter, "increment_usage", return_value=1
        ) as mock_increment:
            response = client.post(
                url,
                data=json.dumps({"type": "test"}),
                content_type="application/json",
            )

        assert response.status_code == 200
        mock_increment.assert_called_once()

    @patch("plugins.sources.stripe.StripeSourcePlugin")
    def test_valid_webhook_is_stored_after_validation(
        self,
        mock_provider_class: Any,
        client: Client,
        workspace: Workspace,
        stripe_integration: Integration,
        settings: Any,
    ) -> None:
        """A validated webhook is still logged/stored when LOG_WEBHOOKS=True."""
        settings.LOG_WEBHOOKS = True

        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = True
        mock_provider.parse_webhook.return_value = None  # Test webhook

        url = f"/webhook/customer/{workspace.uuid}/stripe/"
        with patch.object(webhook_storage_service, "store_webhook") as mock_store:
            response = client.post(
                url,
                data=json.dumps({"type": "test"}),
                content_type="application/json",
            )

        assert response.status_code == 200
        mock_store.assert_called_once()
        store_args = mock_store.call_args[0]
        assert store_args[1] == "stripe"
        assert store_args[2] == str(workspace.uuid)

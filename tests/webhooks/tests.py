import json
from decimal import Decimal
from unittest.mock import Mock, patch

from core.models import Integration, Workspace
from django.test import TestCase
from plugins.sources.base import InvalidDataError
from plugins.sources.stripe import StripeSourcePlugin


class ChargifyWebhookTest(TestCase):
    def setUp(self):
        # Create test workspace and integration
        self.workspace = Workspace.objects.create(
            name="Test Workspace", shop_domain="test.myshopify.com"
        )

        self.integration = Integration.objects.create(
            workspace=self.workspace,
            integration_type="chargify",
            webhook_secret="test-webhook-secret",
            is_active=True,
        )

        # Use workspace-specific webhook URL
        self.url = f"/webhook/customer/{self.workspace.uuid}/chargify/"

        self.headers = {
            "HTTP_X_Chargify_Webhook_Id": "12345",
            "HTTP_X_Chargify_Webhook_Signature_Hmac_Sha_256": "test_signature",
        }
        self.data = {
            "event": "payment_success",
            "payload[subscription][customer][id]": "67890",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][first_name]": "Test",
            "payload[subscription][customer][last_name]": "User",
            "payload[subscription][id]": "11111",
            "payload[subscription][product][name]": "Premium Plan",
            "payload[transaction][id]": "22222",
            "payload[transaction][amount_in_cents]": "2999",
            "created_at": "2024-01-01T00:00:00Z",
        }

    @patch("django.conf.settings.EVENT_PROCESSOR")
    @patch("django.conf.settings.SLACK_CLIENT")
    @patch("plugins.sources.chargify.ChargifySourcePlugin")
    def test_valid_webhook(self, mock_provider_class, mock_slack, mock_processor):
        # Mock the provider instance
        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = True
        mock_provider.parse_webhook.return_value = {
            "type": "payment_success",
            "customer_id": "67890",
        }
        mock_provider.get_customer_data.return_value = {
            "email": "test@example.com",
            "company": "Test Company",
        }
        mock_processor.format_notification.return_value = {"text": "Payment received"}

        response = self.client.post(
            self.url,
            self.data,
            content_type="application/x-www-form-urlencoded",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        response_data = json.loads(response.content)
        self.assertEqual(response_data["status"], "success")

        # Verify provider was created with correct webhook secret
        mock_provider_class.assert_called_once_with(
            webhook_secret="test-webhook-secret"
        )

    @patch("plugins.sources.chargify.ChargifySourcePlugin")
    def test_invalid_signature(self, mock_provider_class):
        # Mock the provider instance with invalid signature
        mock_provider = mock_provider_class.return_value
        mock_provider.validate_webhook.return_value = False

        headers = self.headers.copy()
        headers["HTTP_X_Chargify_Webhook_Signature_Hmac_Sha_256"] = "invalid_signature"
        response = self.client.post(
            self.url,
            self.data,
            content_type="application/x-www-form-urlencoded",
            **headers,
        )
        # Our improved error handling returns 400 for validation errors
        self.assertEqual(response.status_code, 400)
        response_data = json.loads(response.content)
        self.assertEqual(response_data["status"], "error")

        # Verify provider was created with correct webhook secret
        mock_provider_class.assert_called_once_with(
            webhook_secret="test-webhook-secret"
        )


class StripeProviderTest(TestCase):
    """Test StripeSourcePlugin webhook handling."""

    def setUp(self):
        """Set up test data."""
        self.webhook_secret = "test_webhook_secret"
        self.provider = StripeSourcePlugin(self.webhook_secret)

    def test_get_customer_data(self):
        """Test getting customer data from webhook payload.

        We can't call Stripe API (don't have customer's API key),
        so customer data is extracted from the stored webhook data.
        """
        # Simulate webhook data being stored during parse_webhook
        self.provider._current_webhook_data = {
            "id": "in_123",
            "customer": "cus_123",
            "customer_email": "test@acme.com",
            "customer_name": "Test User",
        }

        result = self.provider.get_customer_data("cus_123")

        expected = {
            "company_name": "",  # Not available in webhook
            "email": "test@acme.com",
            "first_name": "Test",
            "last_name": "User",
            "customer_id": "cus_123",
        }

        self.assertEqual(result, expected)

    def test_get_customer_data_no_webhook_data(self) -> None:
        """Test getting customer data when no webhook data is available."""
        # No webhook data stored
        self.provider._current_webhook_data = None

        result = self.provider.get_customer_data("cus_123")

        # Should return empty data
        expected = {
            "company_name": "",
            "email": "",
            "first_name": "",
            "last_name": "",
            "customer_id": "",
        }

        self.assertEqual(result, expected)

    def test_build_stripe_event_data(self):
        """Test building Stripe event data structure."""
        data = {
            "id": "in_123",
            "status": "succeeded",
            "created": 1234567890,
            "currency": "usd",
        }

        result = self.provider._build_stripe_event_data(
            "payment_success", "cus_123", data, 20.00, idempotency_key=None
        )

        expected = {
            "type": "payment_success",
            "customer_id": "cus_123",
            "provider": "stripe",
            "external_id": "in_123",
            "event_id": None,
            "status": "succeeded",
            "created_at": 1234567890,
            "currency": "USD",
            "amount": 20.00,
            "metadata": {},
            "idempotency_key": None,
        }

        self.assertEqual(result, expected)

    def test_build_stripe_event_data_includes_event_id(self):
        """Test that the Stripe event id is surfaced separately."""
        data = {"id": "sub_123", "status": "active"}

        result = self.provider._build_stripe_event_data(
            "subscription_created",
            "cus_123",
            data,
            20.00,
            idempotency_key=None,
            event_id="evt_abc123",
        )

        self.assertEqual(result["event_id"], "evt_abc123")
        self.assertEqual(result["external_id"], "sub_123")

    def test_handle_stripe_billing_unknown_event(self):
        """Test handling unknown billing event."""
        data = {"amount_due": 1500}

        amount = self.provider._handle_stripe_billing("unknown_event", data)

        self.assertEqual(amount, Decimal("0.00"))

    def test_handle_stripe_billing_missing_amount_paid(self):
        """Test handling payment events with missing amount_paid."""
        data = {}  # Missing amount_paid

        amount = self.provider._handle_stripe_billing("payment_success", data)

        self.assertEqual(amount, Decimal("0.00"))

    def test_handle_stripe_billing_missing_plan_amount(self):
        """Test handling subscription created with missing plan amount."""
        data = {"plan": {}}  # Missing amount

        amount = self.provider._handle_stripe_billing("subscription_created", data)

        self.assertEqual(amount, Decimal("0.00"))

    def test_extract_stripe_event_info_unsupported_event(self):
        """Test extracting info for unsupported event type returns None.

        Unsupported events should be acknowledged but not processed,
        so the method returns (None, None) instead of raising an error.
        """
        mock_event = Mock()
        mock_event.type = "unsupported.event.type"

        event_type, data = self.provider._extract_stripe_event_info(mock_event)

        self.assertIsNone(event_type)
        self.assertIsNone(data)

    def test_extract_stripe_event_info_missing_event_type(self):
        """Test extracting info with missing event type."""
        mock_event = Mock()
        mock_event.type = None

        with self.assertRaises(InvalidDataError):
            self.provider._extract_stripe_event_info(mock_event)

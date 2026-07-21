import json
from unittest.mock import patch

from core.models import Integration, UserProfile, Workspace
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse


class NotificationSettingsViewsTest(TestCase):
    """Test notification settings views"""

    def setUp(self):
        """Set up test data"""
        self.client = Client()

        # Create user and workspace
        self.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )

        self.user_profile = UserProfile.objects.create(
            user=self.user,
            slack_user_id="U123456",
            workspace=self.workspace,
        )

        # Notification settings are created automatically via signal
        self.notification_settings = self.workspace.notification_settings

    def test_get_notification_settings_success(self):
        """Test successful retrieval of notification settings"""
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:get_notification_settings"))

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)

        # Check all fields are present and have correct default values
        expected_fields = [
            "notify_payment_success",
            "notify_payment_failure",
            "notify_subscription_created",
            "notify_subscription_updated",
            "notify_subscription_canceled",
            "notify_trial_ending",
            "notify_trial_expired",
            "notify_customer_updated",
            "notify_signups",
            "notify_shopify_order_created",
            "notify_shopify_order_updated",
            "notify_shopify_order_paid",
        ]

        for field in expected_fields:
            self.assertIn(field, data)
            self.assertTrue(data[field])  # All default to True

    def test_get_notification_settings_unauthenticated(self):
        """Test getting settings without authentication"""
        response = self.client.get(reverse("core:get_notification_settings"))

        # Should redirect to login
        self.assertEqual(response.status_code, 302)

    def test_get_notification_settings_no_user_profile(self):
        """Test getting settings for user without profile"""
        # Create user without profile
        noprofile_user = User.objects.create_user(
            username="noprofile", password="testpass123"
        )

        self.client.force_login(noprofile_user)

        response = self.client.get(reverse("core:get_notification_settings"))

        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertEqual(data["error"], "User profile not found")

    def test_get_notification_settings_no_notification_settings(self):
        """Test settings are recreated with defaults when missing"""
        # Delete notification settings (e.g. legacy workspace predating
        # the post_save signal)
        self.notification_settings.delete()

        self.client.force_login(self.user)

        response = self.client.get(reverse("core:get_notification_settings"))

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["notify_payment_success"])
        self.assertTrue(
            self.workspace.__class__.objects.get(
                pk=self.workspace.pk
            ).notification_settings
        )

    def test_update_notification_settings_success(self):
        """Test successful update of notification settings"""
        self.client.force_login(self.user)

        update_data = {
            "notify_payment_success": False,
            "notify_trial_ending": False,
            "notify_shopify_order_created": True,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "success")
        self.assertIn("updated_fields", data)

        # Check database was updated
        self.notification_settings.refresh_from_db()
        self.assertFalse(self.notification_settings.notify_payment_success)
        self.assertFalse(self.notification_settings.notify_trial_ending)
        self.assertTrue(self.notification_settings.notify_shopify_order_created)
        # Other fields should remain True
        self.assertTrue(self.notification_settings.notify_payment_failure)

    def test_update_notification_settings_partial_update(self):
        """Test partial update of notification settings"""
        self.client.force_login(self.user)

        update_data = {
            "notify_payment_success": False,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["updated_fields"], ["notify_payment_success"])

        # Check only specified field was updated
        self.notification_settings.refresh_from_db()
        self.assertFalse(self.notification_settings.notify_payment_success)
        self.assertTrue(self.notification_settings.notify_payment_failure)

    def test_update_notification_settings_empty_data(self):
        """Test update with empty data"""
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["updated_fields"], [])

    def test_update_notification_settings_invalid_field(self):
        """Test update with invalid field name"""
        self.client.force_login(self.user)

        update_data = {
            "invalid_field": False,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("is not allowed to be updated", data["error"])

    def test_update_notification_settings_invalid_value_type(self):
        """Test update with invalid value type"""
        self.client.force_login(self.user)

        update_data = {
            "notify_payment_success": "not_boolean",
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("must be a boolean value", data["error"])

    def test_update_notification_settings_invalid_json(self):
        """Test update with invalid JSON"""
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data="invalid json",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertEqual(data["error"], "Invalid JSON data")

    def test_update_notification_settings_wrong_method(self):
        """Test update with wrong HTTP method"""
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:update_notification_settings"))

        self.assertEqual(response.status_code, 405)
        data = json.loads(response.content)
        self.assertEqual(data["error"], "Method not allowed")

    def test_update_notification_settings_unauthenticated(self):
        """Test update without authentication"""
        update_data = {
            "notify_payment_success": False,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        # Should redirect to login
        self.assertEqual(response.status_code, 302)

    def test_update_notification_settings_no_user_profile(self):
        """Test update for user without profile"""
        # Create user without profile
        noprofile_user = User.objects.create_user(
            username="noprofile", password="testpass123"
        )

        self.client.force_login(noprofile_user)

        update_data = {
            "notify_payment_success": False,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertEqual(data["error"], "User profile not found")

    def test_update_notification_settings_no_notification_settings(self):
        """Test update recreates settings with defaults when missing"""
        # Delete notification settings (e.g. legacy workspace predating
        # the post_save signal)
        self.notification_settings.delete()

        self.client.force_login(self.user)

        update_data = {
            "notify_payment_success": False,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["updated_fields"], ["notify_payment_success"])
        recreated = self.workspace.__class__.objects.get(
            pk=self.workspace.pk
        ).notification_settings
        self.assertFalse(recreated.notify_payment_success)

    @patch("core.views.settings.logger")
    def test_get_notification_settings_internal_error(self, mock_logger):
        """Test handling of internal errors in get view"""
        self.client.force_login(self.user)

        with patch(
            "core.views.settings.NotificationSettings.objects.get_or_create",
            side_effect=Exception("Database error"),
        ):
            response = self.client.get(reverse("core:get_notification_settings"))

            self.assertEqual(response.status_code, 500)
            data = json.loads(response.content)
            self.assertEqual(data["error"], "Internal server error")

            mock_logger.error.assert_called_once()

    @patch("core.views.settings.logger")
    def test_update_notification_settings_internal_error(self, mock_logger):
        """Test handling of internal errors in update view"""
        self.client.force_login(self.user)

        update_data = {
            "notify_payment_success": False,
        }

        with patch(
            "core.views.settings.NotificationSettings.objects.get_or_create",
            side_effect=Exception("Database error"),
        ):
            response = self.client.post(
                reverse("core:update_notification_settings"),
                data=json.dumps(update_data),
                content_type="application/json",
            )

            self.assertEqual(response.status_code, 500)
            data = json.loads(response.content)
            self.assertEqual(data["error"], "Internal server error")

            mock_logger.error.assert_called_once()

    def test_update_notification_settings_all_fields(self):
        """Test updating all notification settings fields"""
        self.client.force_login(self.user)

        # Update all fields to False
        update_data = {
            "notify_payment_success": False,
            "notify_payment_failure": False,
            "notify_subscription_created": False,
            "notify_subscription_updated": False,
            "notify_subscription_canceled": False,
            "notify_trial_ending": False,
            "notify_trial_expired": False,
            "notify_customer_updated": False,
            "notify_signups": False,
            "notify_shopify_order_created": False,
            "notify_shopify_order_updated": False,
            "notify_shopify_order_paid": False,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data["updated_fields"]), 12)

        # Check all fields were updated
        self.notification_settings.refresh_from_db()
        for field_name in update_data.keys():
            self.assertFalse(getattr(self.notification_settings, field_name))

    def test_update_notification_settings_mixed_values(self):
        """Test updating with mixed boolean values"""
        self.client.force_login(self.user)

        update_data = {
            "notify_payment_success": False,
            "notify_payment_failure": True,
            "notify_trial_ending": False,
            "notify_signups": True,
        }

        response = self.client.post(
            reverse("core:update_notification_settings"),
            data=json.dumps(update_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Check specific field values
        self.notification_settings.refresh_from_db()
        self.assertFalse(self.notification_settings.notify_payment_success)
        self.assertTrue(self.notification_settings.notify_payment_failure)
        self.assertFalse(self.notification_settings.notify_trial_ending)
        self.assertTrue(self.notification_settings.notify_signups)


class StripeIntegrationViewsTest(TestCase):
    """Tests for Stripe manual webhook integration views."""

    def setUp(self) -> None:
        """Set up test data."""
        self.client = Client()

        # Create user and workspace
        self.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )

        self.user_profile = UserProfile.objects.create(
            user=self.user,
            slack_user_id="U123456",
            workspace=self.workspace,
        )

    def test_integrate_stripe_shows_configuration_page(self) -> None:
        """Test that integrate_stripe shows the configuration page."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:integrate_stripe"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/integrate_stripe.html.j2")
        self.assertContains(response, "/webhook/customer/")
        self.assertContains(response, str(self.workspace.uuid))

    def test_integrate_stripe_requires_authentication(self) -> None:
        """Test that integrate_stripe requires authentication."""
        response = self.client.get(reverse("core:integrate_stripe"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_integrate_stripe_shows_webhook_events(self) -> None:
        """Test that the configuration page shows webhook events to enable."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:integrate_stripe"))

        self.assertEqual(response.status_code, 200)
        # Check that some key webhook events are shown
        self.assertContains(response, "customer.subscription.created")
        self.assertContains(response, "invoice.payment_succeeded")

    def test_integrate_stripe_post_creates_integration(self) -> None:
        """Test successful POST creates a new integration."""
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("core:integrate_stripe"),
            {"webhook_secret": "whsec_test123456"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:integrations"))

        # Verify integration was created
        integration = Integration.objects.get(
            workspace=self.workspace,
            integration_type="stripe_customer",
        )
        self.assertTrue(integration.is_active)
        self.assertEqual(integration.webhook_secret, "whsec_test123456")

    def test_integrate_stripe_post_updates_existing(self) -> None:
        """Test POST updates existing integration."""
        self.client.force_login(self.user)

        # Create existing integration
        integration = Integration.objects.create(
            workspace=self.workspace,
            integration_type="stripe_customer",
            webhook_secret="whsec_old_secret",
            is_active=True,
        )

        response = self.client.post(
            reverse("core:integrate_stripe"),
            {"webhook_secret": "whsec_new_secret"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:integrations"))

        # Verify integration was updated
        integration.refresh_from_db()
        self.assertEqual(integration.webhook_secret, "whsec_new_secret")

    def test_integrate_stripe_post_empty_secret_fails(self) -> None:
        """Test POST with empty secret fails validation."""
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("core:integrate_stripe"),
            {"webhook_secret": ""},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:integrate_stripe"))

        # Verify no integration was created
        self.assertFalse(
            Integration.objects.filter(
                workspace=self.workspace,
                integration_type="stripe_customer",
            ).exists()
        )

    def test_integrate_stripe_post_invalid_format_fails(self) -> None:
        """Test POST with invalid secret format fails validation."""
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("core:integrate_stripe"),
            {"webhook_secret": "invalid_secret_format"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:integrate_stripe"))

        # Verify no integration was created
        self.assertFalse(
            Integration.objects.filter(
                workspace=self.workspace,
                integration_type="stripe_customer",
            ).exists()
        )

    def test_integrate_stripe_shows_awaiting_verification_status(self) -> None:
        """An unverified integration shows the awaiting-verification state."""
        self.client.force_login(self.user)

        # Create existing integration that has not received a webhook yet
        Integration.objects.create(
            workspace=self.workspace,
            integration_type="stripe_customer",
            webhook_secret="whsec_test123",
            is_active=True,
        )

        response = self.client.get(reverse("core:integrate_stripe"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Awaiting webhook verification")

    def test_integrate_stripe_shows_existing_connection_status(self) -> None:
        """A verified integration shows the connected state."""
        from django.utils import timezone

        self.client.force_login(self.user)

        Integration.objects.create(
            workspace=self.workspace,
            integration_type="stripe_customer",
            webhook_secret="whsec_test123",
            is_active=True,
            webhook_verified_at=timezone.now(),
        )

        response = self.client.get(reverse("core:integrate_stripe"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Stripe is connected")

    def test_disconnect_stripe_requires_post(self) -> None:
        """Test that disconnect_stripe requires POST method."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:disconnect_stripe"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:integrations"))

    def test_disconnect_stripe_no_integration(self) -> None:
        """Test disconnect when no integration exists."""
        self.client.force_login(self.user)

        response = self.client.post(reverse("core:disconnect_stripe"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:integrations"))

    def test_disconnect_stripe_success(self) -> None:
        """Test successful Stripe disconnection."""
        self.client.force_login(self.user)

        # Create active integration
        integration = Integration.objects.create(
            workspace=self.workspace,
            integration_type="stripe_customer",
            webhook_secret="whsec_test123",
            is_active=True,
        )

        response = self.client.post(reverse("core:disconnect_stripe"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:integrations"))

        # Verify integration was deactivated
        integration.refresh_from_db()
        self.assertFalse(integration.is_active)

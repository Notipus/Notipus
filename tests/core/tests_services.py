from unittest.mock import Mock, patch

from core.models import Company, Integration, UserProfile, Workspace
from core.services.enrichment import DomainEnrichmentService
from core.services.stripe import StripeAPI
from django.test import TestCase
from webhooks.services.billing import BillingService


class DomainEnrichmentServiceTest(TestCase):
    """Test DomainEnrichmentService with plugin-based architecture."""

    def setUp(self) -> None:
        """Set up test data."""
        # Create service with mocked plugin registry
        with patch("core.services.enrichment.PluginRegistry") as mock_registry_class:
            mock_registry = Mock()
            mock_registry.get_enabled.return_value = []
            mock_registry_class.instance.return_value = mock_registry
            self.service = DomainEnrichmentService()
            self.mock_registry = mock_registry

    def test_initialize_with_no_plugins(self) -> None:
        """Test service initialization when no plugins available."""
        with patch("core.services.enrichment.PluginRegistry") as mock_registry_class:
            mock_registry = Mock()
            mock_registry.get_enabled.return_value = []
            mock_registry_class.instance.return_value = mock_registry

            service = DomainEnrichmentService()

            self.assertEqual(len(service._plugins), 0)

    def test_initialize_with_plugins(self) -> None:
        """Test service initialization with available plugins."""
        mock_plugin = Mock()
        mock_plugin.get_plugin_name.return_value = "test_plugin"

        with patch("core.services.enrichment.PluginRegistry") as mock_registry_class:
            mock_registry = Mock()
            mock_registry.get_enabled.return_value = [mock_plugin]
            mock_registry_class.instance.return_value = mock_registry

            service = DomainEnrichmentService()

            self.assertEqual(len(service._plugins), 1)

    def test_enrich_domain_creates_new_company(self) -> None:
        """Test enriching domain creates new company."""
        domain = "newcompany.com"

        # Mock plugin
        mock_plugin = Mock()
        mock_plugin.get_plugin_name.return_value = "testplugin"
        mock_plugin.enrich_domain.return_value = {
            "name": "New Company",
            "logo_url": "https://newcompany.com/logo.png",
        }
        self.service._plugins = [mock_plugin]

        with patch.object(self.service, "_update_company"):
            result = self.service.enrich_domain(domain)

        # Check company was created
        self.assertTrue(Company.objects.filter(domain=domain).exists())
        self.assertEqual(result.domain, domain)
        mock_plugin.enrich_domain.assert_called_once_with(domain)

    def test_enrich_domain_existing_company_with_enrichment(self) -> None:
        """Test enriching existing company that already has enrichment data."""
        domain = "existing.com"

        # Create existing company with enrichment data (_blended_at indicates enriched)
        existing_company = Company.objects.create(
            domain=domain,
            name="Existing Company",
            brand_info={"_blended_at": "2024-01-01T00:00:00Z"},
        )

        # Mock plugin
        mock_plugin = Mock()
        self.service._plugins = [mock_plugin]

        result = self.service.enrich_domain(domain)

        # Should return existing company without calling plugin
        self.assertEqual(result, existing_company)
        mock_plugin.enrich_domain.assert_not_called()

    def test_enrich_domain_no_plugins(self) -> None:
        """Test enriching domain with no plugins available."""
        domain = "noplugins.com"

        # No plugins
        self.service._plugins = []

        result = self.service.enrich_domain(domain)

        # Should create company but not enrich it
        company = Company.objects.get(domain=domain)
        self.assertEqual(company.name, "")
        self.assertEqual(result, company)

    def test_enrich_domain_plugin_exception(self) -> None:
        """Test handling plugin exceptions gracefully."""
        domain = "error.com"

        # Mock plugin that raises exception
        mock_plugin = Mock()
        mock_plugin.get_plugin_name.return_value = "failing_plugin"
        mock_plugin.enrich_domain.side_effect = Exception("API Error")
        self.service._plugins = [mock_plugin]

        with patch("core.services.enrichment.logger") as mock_logger:
            result = self.service.enrich_domain(domain)

            # Should create company but not update it
            company = Company.objects.get(domain=domain)
            self.assertEqual(company.name, "")
            self.assertEqual(result, company)

            # Should log warning for plugin failure
            mock_logger.warning.assert_called()

    def test_update_company_with_blended_data(self) -> None:
        """Test updating company with blended enrichment data."""
        company = Company.objects.create(domain="update.com")

        blended_data = {
            "name": "Updated Company",
            "_blended_at": "2024-01-01T00:00:00Z",
            "_sources": {"plugin1": {"fetched_at": "2024-01-01", "raw": {}}},
        }

        with patch("core.services.enrichment.get_logo_storage_service"):
            self.service._update_company(company, blended_data)

        company.refresh_from_db()
        self.assertEqual(company.name, "Updated Company")
        self.assertEqual(company.brand_info["_blended_at"], "2024-01-01T00:00:00Z")

    def test_has_enrichment_true(self) -> None:
        """Test _has_enrichment returns True when company has enrichment data."""
        company = Company.objects.create(
            domain="enriched.com",
            brand_info={"_blended_at": "2024-01-01T00:00:00Z"},
        )

        self.assertTrue(self.service._has_enrichment(company))

    def test_has_enrichment_false(self) -> None:
        """Test _has_enrichment returns False when company lacks enrichment data."""
        company = Company.objects.create(domain="notenriched.com", brand_info={})

        self.assertFalse(self.service._has_enrichment(company))


class BillingServiceTest(TestCase):
    """Test BillingService"""

    def setUp(self):
        """Set up test data"""
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
            stripe_customer_id="cus_test123",
        )

    @patch("webhooks.services.billing.BillingService.sync_workspace_from_stripe")
    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_created_success(self, mock_logger, mock_sync):
        """Test successful subscription creation handling"""
        subscription_data = {
            "customer": "cus_test123",
            "items": {"data": [{"plan": {"id": "plan_basic"}}]},
            "current_period_start": 1234567890,
        }

        BillingService.handle_subscription_created(subscription_data)

        self.workspace.refresh_from_db()
        # subscription_plan is set by sync_workspace_from_stripe, not directly
        self.assertEqual(self.workspace.subscription_status, "active")
        self.assertEqual(self.workspace.billing_cycle_anchor, 1234567890)

        mock_logger.info.assert_called_once_with(
            "Subscription created for customer cus_test123, syncing..."
        )
        mock_sync.assert_called_once_with("cus_test123")

    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_created_missing_customer(self, mock_logger):
        """Test subscription creation with missing customer ID"""
        subscription_data = {"items": {"data": [{"plan": {"id": "plan_basic"}}]}}

        BillingService.handle_subscription_created(subscription_data)

        self.workspace.refresh_from_db()
        # Should not update workspace - plan stays at default (free)
        self.assertEqual(self.workspace.subscription_plan, "free")

        mock_logger.error.assert_called_once_with(
            "Missing customer ID in subscription data"
        )

    @patch("webhooks.services.billing.BillingService.sync_workspace_from_stripe")
    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_created_missing_plan(self, mock_logger, mock_sync):
        """Test subscription creation with missing plan ID in webhook data.

        The webhook handler no longer extracts plan_id directly - it delegates
        to sync_workspace_from_stripe which fetches the full subscription.
        """
        subscription_data = {
            "customer": "cus_test123",
            "items": {"data": [{}]},  # Missing plan - but handled by sync
        }

        BillingService.handle_subscription_created(subscription_data)

        self.workspace.refresh_from_db()
        # Status is updated, plan extraction delegated to sync
        self.assertEqual(self.workspace.subscription_status, "active")

        mock_logger.info.assert_called_once_with(
            "Subscription created for customer cus_test123, syncing..."
        )
        mock_sync.assert_called_once_with("cus_test123")

    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_created_customer_not_found(self, mock_logger):
        """Test subscription creation for non-existent customer"""
        subscription_data = {
            "customer": "cus_nonexistent",
            "items": {"data": [{"plan": {"id": "plan_basic"}}]},
            "current_period_start": 1234567890,
        }

        BillingService.handle_subscription_created(subscription_data)

        mock_logger.warning.assert_called_once_with(
            "No workspace found for customer cus_nonexistent"
        )

    @patch("webhooks.services.billing.logger")
    def test_handle_payment_success(self, mock_logger):
        """Test successful payment handling"""
        invoice_data = {"customer": "cus_test123", "period_end": 1234567890}

        BillingService.handle_payment_success(invoice_data)

        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.subscription_status, "active")
        self.assertEqual(self.workspace.billing_cycle_anchor, 1234567890)

        mock_logger.info.assert_called_once_with(
            "Updated payment status to active for customer cus_test123"
        )

    @patch("webhooks.services.billing.logger")
    def test_handle_payment_success_missing_customer(self, mock_logger):
        """Test payment success with missing customer ID"""
        invoice_data = {"period_end": 1234567890}

        BillingService.handle_payment_success(invoice_data)

        mock_logger.error.assert_called_once_with("Missing customer ID in invoice data")

    @patch("webhooks.services.billing.logger")
    def test_handle_payment_failed(self, mock_logger):
        """Test failed payment handling"""
        invoice_data = {"customer": "cus_test123"}

        BillingService.handle_payment_failed(invoice_data)

        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.subscription_status, "past_due")

        mock_logger.warning.assert_called_once_with(
            "Updated payment status to past_due for customer cus_test123"
        )

    @patch("webhooks.services.billing.logger")
    def test_handle_payment_failed_missing_customer(self, mock_logger):
        """Test payment failure with missing customer ID"""
        invoice_data = {}

        BillingService.handle_payment_failed(invoice_data)

        mock_logger.error.assert_called_once_with("Missing customer ID in invoice data")

    @patch("webhooks.services.billing.Workspace.objects")
    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_created_exception(self, mock_logger, mock_objects):
        """Test exception handling in subscription creation"""
        mock_objects.filter.side_effect = Exception("Database error")

        subscription_data = {
            "customer": "cus_test123",
            "items": {"data": [{"plan": {"id": "plan_basic"}}]},
        }

        BillingService.handle_subscription_created(subscription_data)

        mock_logger.error.assert_called_once_with(
            "Error handling subscription created: Database error"
        )

    @patch("webhooks.services.billing.Workspace.objects")
    @patch("webhooks.services.billing.logger")
    def test_handle_payment_success_exception(self, mock_logger, mock_objects):
        """Test exception handling in payment success"""
        mock_objects.filter.side_effect = Exception("Database error")

        invoice_data = {"customer": "cus_test123"}

        BillingService.handle_payment_success(invoice_data)

        mock_logger.error.assert_called_once_with(
            "Error handling payment success: Database error"
        )

    @patch("webhooks.services.billing.Workspace.objects")
    @patch("webhooks.services.billing.logger")
    def test_handle_payment_failed_exception(self, mock_logger, mock_objects):
        """Test exception handling in payment failure"""
        mock_objects.filter.side_effect = Exception("Database error")

        invoice_data = {"customer": "cus_test123"}

        BillingService.handle_payment_failed(invoice_data)

        mock_logger.error.assert_called_once_with(
            "Error handling payment failure: Database error"
        )

    @patch("webhooks.services.billing.BillingService.sync_workspace_from_stripe")
    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_updated_success(self, mock_logger, mock_sync):
        """Test successful subscription update handling"""
        subscription_data = {
            "customer": "cus_test123",
            "status": "active",
            "items": {"data": [{"plan": {"id": "plan_pro"}}]},
            "current_period_end": 1234567890,
        }

        BillingService.handle_subscription_updated(subscription_data)

        self.workspace.refresh_from_db()
        # subscription_plan is set by sync_workspace_from_stripe, not directly
        self.assertEqual(self.workspace.subscription_status, "active")
        self.assertEqual(self.workspace.billing_cycle_anchor, 1234567890)

        mock_logger.info.assert_called_once()
        self.assertIn("active", mock_logger.info.call_args[0][0])
        mock_sync.assert_called_once_with("cus_test123")

    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_updated_status_changes(self, mock_logger):
        """Test subscription update with various status changes"""
        status_test_cases = [
            ("trialing", "trial"),
            ("past_due", "past_due"),
            ("canceled", "cancelled"),
            ("unpaid", "past_due"),
        ]

        for stripe_status, expected_internal_status in status_test_cases:
            with self.subTest(stripe_status=stripe_status):
                # Reset workspace
                self.workspace.subscription_status = "active"
                self.workspace.save()

                subscription_data = {
                    "customer": "cus_test123",
                    "status": stripe_status,
                }

                BillingService.handle_subscription_updated(subscription_data)

                self.workspace.refresh_from_db()
                self.assertEqual(
                    self.workspace.subscription_status, expected_internal_status
                )

    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_updated_missing_customer(self, mock_logger):
        """Test subscription update with missing customer ID"""
        subscription_data = {"status": "active"}

        BillingService.handle_subscription_updated(subscription_data)

        mock_logger.error.assert_called_once_with(
            "Missing customer ID in subscription data"
        )

    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_deleted_success(self, mock_logger):
        """Test successful subscription deletion handling"""
        subscription_data = {"customer": "cus_test123"}

        BillingService.handle_subscription_deleted(subscription_data)

        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.subscription_status, "cancelled")

        mock_logger.info.assert_called_once_with(
            "Marked subscription as cancelled for customer cus_test123"
        )

    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_deleted_missing_customer(self, mock_logger):
        """Test subscription deletion with missing customer ID"""
        subscription_data = {}

        BillingService.handle_subscription_deleted(subscription_data)

        mock_logger.error.assert_called_once_with(
            "Missing customer ID in subscription data"
        )

    @patch("webhooks.services.billing.logger")
    def test_handle_subscription_deleted_customer_not_found(self, mock_logger):
        """Test subscription deletion for non-existent customer"""
        subscription_data = {"customer": "cus_nonexistent"}

        BillingService.handle_subscription_deleted(subscription_data)

        mock_logger.warning.assert_called_once_with(
            "No workspace found for customer cus_nonexistent"
        )


class StripeAPITest(TestCase):
    """Test StripeAPI service."""

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_success(self, mock_create: Mock) -> None:
        """Test successful customer creation."""
        # Mock successful customer creation
        mock_customer = Mock()
        mock_customer.to_dict.return_value = {
            "id": "cus_test123",
            "email": "test@example.com",
            "name": "Test Customer",
        }
        mock_create.return_value = mock_customer

        customer_data = {
            "email": "test@example.com",
            "name": "Test Customer",
            "metadata": {"source": "test"},
        }

        # Use instance method
        api = StripeAPI()
        result = api.create_stripe_customer(customer_data)

        expected = {
            "id": "cus_test123",
            "email": "test@example.com",
            "name": "Test Customer",
        }

        self.assertEqual(result, expected)
        mock_create.assert_called_once_with(**customer_data)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_stripe_error(self, mock_create: Mock) -> None:
        """Test customer creation with Stripe error."""
        # Mock Stripe error using the actual stripe library structure
        mock_create.side_effect = Exception("Test Stripe error")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        api = StripeAPI()
        result = api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_empty_data(self, mock_create: Mock) -> None:
        """Test customer creation with empty data."""
        # Mock successful creation with empty data
        mock_customer = Mock()
        mock_customer.to_dict.return_value = {"id": "cus_empty"}
        mock_create.return_value = mock_customer

        api = StripeAPI()
        result = api.create_stripe_customer({})

        self.assertEqual(result, {"id": "cus_empty"})
        mock_create.assert_called_once_with()


class DashboardServiceTest(TestCase):
    """Test DashboardService"""

    def setUp(self):
        """Set up test data"""
        from django.contrib.auth.models import User

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
            stripe_customer_id="cus_test123",
        )
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        self.user_profile = UserProfile.objects.create(
            user=self.user,
            workspace=self.workspace,
        )

    @patch("core.services.dashboard.rate_limiter")
    @patch("core.services.dashboard.DatabaseLookupService")
    def test_get_dashboard_data_success(self, mock_db_service, mock_rate_limiter):
        """Test successful dashboard data retrieval"""
        from core.services.dashboard import DashboardService

        # Mock rate limiter
        mock_rate_limiter.check_rate_limit.return_value = (
            True,
            {"current_usage": 50, "limit": 1000, "remaining": 950},
        )
        mock_rate_limiter.get_usage_stats.return_value = {}

        # Mock database service
        mock_db_instance = Mock()
        mock_db_instance.get_recent_webhook_activity.return_value = []
        mock_db_service.return_value = mock_db_instance

        service = DashboardService()
        result = service.get_dashboard_data(self.user)

        self.assertIsNotNone(result)
        self.assertEqual(result["workspace"], self.workspace)
        self.assertEqual(result["user_profile"], self.user_profile)
        self.assertIn("integrations", result)
        self.assertIn("recent_activity", result)
        self.assertIn("usage_data", result)
        self.assertIn("trial_info", result)

        # Verify workspace_id is passed to get_recent_webhook_activity
        mock_db_instance.get_recent_webhook_activity.assert_called_once_with(
            workspace_id=str(self.workspace.uuid), days=7, limit=100
        )

    @patch("core.services.dashboard.rate_limiter")
    @patch("core.services.dashboard.DatabaseLookupService")
    def test_get_dashboard_data_no_profile(self, mock_db_service, mock_rate_limiter):
        """Test dashboard data retrieval when user has no profile"""
        from core.services.dashboard import DashboardService
        from django.contrib.auth.models import User

        user_without_profile = User.objects.create_user(
            username="noprofile",
            email="noprofile@example.com",
            password="testpass123",
        )

        service = DashboardService()
        result = service.get_dashboard_data(user_without_profile)

        self.assertIsNone(result)

    def test_get_integration_data(self):
        """Test integration data retrieval"""
        from core.services.dashboard import DashboardService

        # Create some integrations
        Integration.objects.create(
            workspace=self.workspace,
            integration_type="slack_notifications",
            is_active=True,
        )
        Integration.objects.create(
            workspace=self.workspace,
            integration_type="shopify",
            is_active=True,
        )

        service = DashboardService()
        result = service._get_integration_data(self.workspace)

        self.assertTrue(result["has_slack"])
        self.assertTrue(result["has_shopify"])
        self.assertFalse(result["has_chargify"])
        self.assertFalse(result["has_stripe"])

    def test_get_trial_info_active_trial(self) -> None:
        """Test trial info for active trial."""
        from core.services.dashboard import DashboardService
        from django.utils import timezone

        # Set workspace as trial with a paid plan (free plan can't have trial status)
        self.workspace.subscription_plan = "pro"
        self.workspace.subscription_status = "trial"
        self.workspace.trial_end_date = timezone.now() + timezone.timedelta(days=10)
        self.workspace.save()

        service = DashboardService()
        result = service._get_trial_info(self.workspace)

        self.assertTrue(result["is_trial"])
        # Days remaining can be 9 or 10 depending on exact timing
        self.assertIn(result["trial_days_remaining"], [9, 10])

    def test_get_trial_info_not_trial(self):
        """Test trial info for non-trial subscription"""
        from core.services.dashboard import DashboardService

        self.workspace.subscription_status = "active"
        self.workspace.save()

        service = DashboardService()
        result = service._get_trial_info(self.workspace)

        self.assertFalse(result["is_trial"])
        self.assertEqual(result["trial_days_remaining"], 0)

    @patch("core.services.dashboard.rate_limiter")
    def test_get_usage_data_success(self, mock_rate_limiter):
        """Test usage data retrieval"""
        from core.services.dashboard import DashboardService

        mock_rate_limiter.check_rate_limit.return_value = (
            True,
            {"current_usage": 500, "limit": 1000, "remaining": 500},
        )
        mock_rate_limiter.get_usage_stats.return_value = {"monthly": []}

        service = DashboardService()
        result = service._get_usage_data(self.workspace)

        self.assertTrue(result["is_allowed"])
        self.assertEqual(result["usage_percentage"], 50.0)

    @patch("core.services.dashboard.rate_limiter")
    def test_get_usage_data_error_handling(self, mock_rate_limiter):
        """Test usage data error handling"""
        from core.services.dashboard import DashboardService

        mock_rate_limiter.check_rate_limit.side_effect = Exception("Redis error")

        service = DashboardService()
        result = service._get_usage_data(self.workspace)

        # Should return default values on error
        self.assertTrue(result["is_allowed"])
        self.assertEqual(result["usage_percentage"], 0)

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_transform_activity_data(self, mock_db_service):
        """Test activity data transformation"""
        from core.services.dashboard import DashboardService

        service = DashboardService()

        raw_activity = [
            {
                "type": "payment_success",
                "provider": "stripe",
                "status": "completed",
                "amount": "99.99",
                "currency": "USD",
                "timestamp": 1234567890,
                "external_id": "pi_123",
                "customer_id": "cus_123",
            },
        ]

        result = service._transform_activity_data(raw_activity)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "payment_success")
        self.assertEqual(result[0]["provider"], "stripe")
        self.assertEqual(result[0]["amount"], "99.99")

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_transform_activity_data_with_enriched_fields(self, mock_db_service):
        """Test activity data transformation with enriched fields"""
        from core.services.dashboard import DashboardService

        service = DashboardService()

        raw_activity = [
            {
                "type": "payment",
                "event_type": "payment_success",
                "provider": "stripe",
                "status": "success",
                "severity": "success",
                "amount": 299.00,
                "currency": "USD",
                "timestamp": 1234567890,
                "external_id": "pi_123",
                "customer_id": "cus_123",
                "headline": "$299.00 from Acme Corp",
                "company_name": "Acme Corp",
                "company_logo_url": "https://logo.clearbit.com/acme.com",
                "company_domain": "acme.com",
                "customer_email": "billing@acme.com",
                "customer_name": "John Doe",
                "customer_ltv": "$2.5k",
                "customer_tenure": "Since Mar 2024",
                "customer_status_flags": ["vip"],
                "insight_text": "First payment - Welcome aboard!",
                "insight_icon": "celebration",
                "plan_name": "Pro Plan",
                "payment_method": "visa",
                "card_last4": "4242",
            },
        ]

        result = service._transform_activity_data(raw_activity)

        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(item["headline"], "$299.00 from Acme Corp")
        self.assertEqual(item["company_name"], "Acme Corp")
        self.assertEqual(item["company_logo_url"], "https://logo.clearbit.com/acme.com")
        self.assertEqual(item["customer_email"], "billing@acme.com")
        self.assertEqual(item["customer_ltv"], "$2.5k")
        self.assertEqual(item["customer_tenure"], "Since Mar 2024")
        self.assertEqual(item["customer_status_flags"], ["vip"])
        self.assertEqual(item["insight_text"], "First payment - Welcome aboard!")
        self.assertEqual(item["insight_icon"], "celebration")
        self.assertEqual(item["plan_name"], "Pro Plan")

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_empty(self, mock_db_service):
        """Test deduplication with empty activity list"""
        from core.services.dashboard import DashboardService

        service = DashboardService()
        result = service._deduplicate_activity([])

        self.assertEqual(result, [])

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_single_event(self, mock_db_service):
        """Test deduplication with single event"""
        from core.services.dashboard import DashboardService
        from django.utils import timezone

        service = DashboardService()

        activity = [
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": timezone.now(),
                "amount": 100.00,
            }
        ]

        result = service._deduplicate_activity(activity)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_count"], 1)

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_groups_same_customer_type_date(self, mock_db_service):
        """Test deduplication groups events by customer+type+date"""
        from datetime import timedelta

        from core.services.dashboard import DashboardService
        from django.utils import timezone

        service = DashboardService()

        now = timezone.now()
        # Same customer, same type, same day - should be grouped
        activity = [
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 100.00,
            },
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now - timedelta(hours=2),
                "amount": 200.00,
            },
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now - timedelta(hours=4),
                "amount": 150.00,
            },
        ]

        result = service._deduplicate_activity(activity)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_count"], 3)
        self.assertEqual(result[0]["total_amount"], 450.00)

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_keeps_different_customers_separate(
        self, mock_db_service
    ):
        """Test deduplication keeps different customers separate"""
        from core.services.dashboard import DashboardService
        from django.utils import timezone

        service = DashboardService()

        now = timezone.now()
        activity = [
            {
                "customer_email": "alice@example.com",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 100.00,
            },
            {
                "customer_email": "bob@example.com",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 200.00,
            },
        ]

        result = service._deduplicate_activity(activity)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["event_count"], 1)
        self.assertEqual(result[1]["event_count"], 1)

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_keeps_different_event_types_separate(
        self, mock_db_service
    ):
        """Test deduplication keeps different event types separate"""
        from core.services.dashboard import DashboardService
        from django.utils import timezone

        service = DashboardService()

        now = timezone.now()
        activity = [
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 100.00,
            },
            {
                "customer_email": "test@example.com",
                "event_type": "subscription_created",
                "processed_at": now,
                "amount": 100.00,
            },
        ]

        result = service._deduplicate_activity(activity)

        self.assertEqual(len(result), 2)

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_keeps_different_dates_separate(self, mock_db_service):
        """Test deduplication keeps different dates separate"""
        from datetime import timedelta

        from core.services.dashboard import DashboardService
        from django.utils import timezone

        service = DashboardService()

        now = timezone.now()
        yesterday = now - timedelta(days=1)

        activity = [
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 100.00,
            },
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": yesterday,
                "amount": 100.00,
            },
        ]

        result = service._deduplicate_activity(activity)

        self.assertEqual(len(result), 2)

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_uses_most_recent_event(self, mock_db_service):
        """Test deduplication uses most recent event as representative"""
        from datetime import timedelta

        from core.services.dashboard import DashboardService
        from django.utils import timezone

        service = DashboardService()

        now = timezone.now()
        activity = [
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now - timedelta(hours=4),
                "amount": 100.00,
                "headline": "Old headline",
            },
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 300.00,
                "headline": "Most recent headline",
            },
            {
                "customer_email": "test@example.com",
                "event_type": "payment_success",
                "processed_at": now - timedelta(hours=2),
                "amount": 200.00,
                "headline": "Middle headline",
            },
        ]

        result = service._deduplicate_activity(activity)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["headline"], "Most recent headline")
        self.assertEqual(result[0]["amount"], 300.00)
        self.assertEqual(result[0]["event_count"], 3)

    @patch("core.services.dashboard.DatabaseLookupService")
    def test_deduplicate_activity_fallback_to_customer_id(self, mock_db_service):
        """Test deduplication falls back to customer_id when email is missing"""
        from core.services.dashboard import DashboardService
        from django.utils import timezone

        service = DashboardService()

        now = timezone.now()
        activity = [
            {
                "customer_id": "cus_123",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 100.00,
            },
            {
                "customer_id": "cus_123",
                "event_type": "payment_success",
                "processed_at": now,
                "amount": 200.00,
            },
        ]

        result = service._deduplicate_activity(activity)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_count"], 2)

    @patch("core.services.dashboard.rate_limiter")
    @patch("core.services.dashboard.DatabaseLookupService")
    def test_recent_activity_passes_workspace_id(
        self, mock_db_service, mock_rate_limiter
    ):
        """Test that _get_recent_activity passes workspace_id to db service."""
        from core.services.dashboard import DashboardService

        mock_db_instance = Mock()
        mock_db_instance.get_recent_webhook_activity.return_value = []
        mock_db_service.return_value = mock_db_instance

        service = DashboardService()
        service._get_recent_activity(self.workspace)

        mock_db_instance.get_recent_webhook_activity.assert_called_once_with(
            workspace_id=str(self.workspace.uuid), days=7, limit=100
        )

    @patch("core.services.dashboard.rate_limiter")
    @patch("core.services.dashboard.DatabaseLookupService")
    def test_recent_activity_isolates_workspaces(
        self, mock_db_service, mock_rate_limiter
    ):
        """Test that each workspace queries with its own workspace_id."""
        from core.services.dashboard import DashboardService

        workspace_b = Workspace.objects.create(
            name="Other Workspace",
            shop_domain="other.myshopify.com",
        )

        mock_db_instance = Mock()
        mock_db_instance.get_recent_webhook_activity.return_value = []
        mock_db_service.return_value = mock_db_instance

        service = DashboardService()

        service._get_recent_activity(self.workspace)
        service._get_recent_activity(workspace_b)

        calls = mock_db_instance.get_recent_webhook_activity.call_args_list
        self.assertEqual(len(calls), 2)

        # First call uses workspace A's UUID
        self.assertEqual(calls[0].kwargs["workspace_id"], str(self.workspace.uuid))
        # Second call uses workspace B's UUID
        self.assertEqual(calls[1].kwargs["workspace_id"], str(workspace_b.uuid))

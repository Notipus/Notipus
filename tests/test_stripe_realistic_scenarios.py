"""Integration tests for Stripe webhook processing with real payload structures.

These tests simulate the complete webhook processing pipeline from raw Stripe
webhook payload to final Slack message, based on actual production data
(with customer information redacted).

The tests verify:
- Event parsing and normalization
- Event consolidation/filtering
- Customer data enrichment
- Final Slack message content and structure using RichNotification
"""

from typing import Any
from unittest.mock import Mock, patch

import pytest
from plugins.sources.stripe import StripeSourcePlugin
from webhooks.models.rich_notification import RichNotification
from webhooks.services.event_consolidation import EventConsolidationService
from webhooks.services.event_processor import EventProcessor


class TestTrialSignupIntegration:
    """Integration test: Trial signup webhook flow.

    Simulates a real trial signup where Stripe fires 3 events:
    1. customer.subscription.created - should generate notification
    2. invoice.paid ($0) - should be filtered (zero amount)
    3. invoice.payment_succeeded ($0) - should be filtered (zero amount)

    Verifies only 1 Slack notification is generated with correct content.
    """

    @pytest.fixture
    def stripe_plugin(self) -> StripeSourcePlugin:
        """Create a Stripe plugin instance."""
        return StripeSourcePlugin()

    @pytest.fixture
    def event_processor(self) -> EventProcessor:
        """Create an event processor instance."""
        return EventProcessor()

    @pytest.fixture
    def consolidation_service(self) -> EventConsolidationService:
        """Create a consolidation service instance."""
        return EventConsolidationService()

    @pytest.fixture
    def subscription_created_payload(self) -> dict[str, Any]:
        """Real subscription.created webhook payload (redacted).

        Based on actual Stripe webhook captured from production.
        """
        return {
            "id": "evt_1ABC123DEF456GHI789",
            "object": "event",
            "api_version": "2025-12-15.clover",
            "created": 1769327717,
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_1ABC123DEF456GHI",
                    "object": "subscription",
                    "customer": "cus_TestCustomer123",
                    "status": "trialing",
                    "currency": "usd",
                    "created": 1769327717,
                    "current_period_start": 1769327717,
                    "current_period_end": 1770537317,
                    "trial_start": 1769327717,
                    "trial_end": 1770537317,
                    "cancel_at_period_end": False,
                    "canceled_at": None,
                    "plan": {
                        "id": "price_1ABC123",
                        "object": "plan",
                        "amount": 2660,
                        "currency": "usd",
                        "interval": "month",
                        "product": "prod_TestProduct",
                    },
                },
            },
        }

    @pytest.fixture
    def invoice_paid_zero_payload(self) -> dict[str, Any]:
        """Real invoice.paid webhook with $0 amount (trial)."""
        return {
            "id": "evt_2ABC123DEF456GHI789",
            "object": "event",
            "api_version": "2025-12-15.clover",
            "created": 1769327718,
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_1ABC123DEF456GHI",
                    "object": "invoice",
                    "customer": "cus_TestCustomer123",
                    "amount_due": 0,
                    "amount_paid": 0,
                    "amount_remaining": 0,
                    "currency": "usd",
                    "status": "paid",
                    "billing_reason": "subscription_create",
                    "subscription": "sub_1ABC123DEF456GHI",
                    "created": 1769327717,
                },
            },
        }

    @pytest.fixture
    def customer_data(self) -> dict[str, Any]:
        """Customer data as returned by Stripe API."""
        return {
            "email": "john.smith@acmecorp.com",
            "first_name": "John",
            "last_name": "Smith",
            "company": "",  # Often empty in Stripe
        }

    def test_trial_subscription_generates_trial_started_notification(
        self,
        stripe_plugin: StripeSourcePlugin,
        event_processor: EventProcessor,
        subscription_created_payload: dict[str, Any],
        customer_data: dict[str, Any],
    ) -> None:
        """Test subscription.created with status=trialing generates trial_started."""
        # Create mock Stripe event object
        mock_event = Mock()
        mock_event.type = subscription_created_payload["type"]
        mock_event.data.object = subscription_created_payload["data"]["object"]
        mock_event.data.previous_attributes = None

        # Extract event info - initially subscription_created
        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        assert event_type == "subscription_created"

        # Handle billing - should detect trial and return 0 amount
        amount = stripe_plugin._handle_stripe_billing(event_type, data)
        assert amount == 0.0  # No payment for trials
        assert data.get("_is_trial") is True  # Trial flag should be set

        # Transform event type for trials (as done in parse_webhook)
        if data.get("_is_trial"):
            event_type = "trial_started"

        # Build event data
        event_data = stripe_plugin._build_stripe_event_data(
            event_type=event_type,
            customer_id=data["customer"],
            data=data,
            amount=amount,
        )

        # Verify trial metadata
        assert event_data["metadata"]["is_trial"] is True
        assert event_data["metadata"]["trial_days"] == 14  # 14-day trial
        assert event_data["metadata"]["plan_amount"] == 26.60  # Future billing amount

        # Build rich notification
        notification = event_processor.build_rich_notification(
            event_data, customer_data
        )

        # Verify notification structure
        assert isinstance(notification, RichNotification)
        # Headline should indicate trial, not payment
        assert "Trial started" in notification.headline
        assert "New customer" not in notification.headline

        # Format for Slack
        slack_message = event_processor.process_event_rich(
            event_data, customer_data, target="slack"
        )

        # Verify Slack message structure (attachments format for colored messages)
        assert "attachments" in slack_message
        assert len(slack_message["attachments"]) > 0
        attachment = slack_message["attachments"][0]
        assert "blocks" in attachment
        assert "color" in attachment

        # Verify header block exists with trial headline
        header_block = attachment["blocks"][0]
        assert header_block["type"] == "header"

    def test_non_trial_subscription_generates_new_customer_notification(
        self,
        stripe_plugin: StripeSourcePlugin,
        event_processor: EventProcessor,
        customer_data: dict[str, Any],
    ) -> None:
        """Test that subscription.created with status=active shows 'New customer!'."""
        # Create a non-trial subscription payload (status=active, no trial fields)
        active_subscription_data = {
            "id": "sub_active123",
            "object": "subscription",
            "customer": "cus_TestCustomer123",
            "status": "active",  # Not trialing!
            "currency": "usd",
            "created": 1769327717,
            "current_period_start": 1769327717,
            "current_period_end": 1770537317,
            "cancel_at_period_end": False,
            "canceled_at": None,
            "plan": {
                "id": "price_1ABC123",
                "object": "plan",
                "amount": 2660,
                "currency": "usd",
                "interval": "month",
                "product": "prod_TestProduct",
            },
        }

        mock_event = Mock()
        mock_event.type = "customer.subscription.created"
        mock_event.data.object = active_subscription_data
        mock_event.data.previous_attributes = None

        # Extract event info
        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        assert event_type == "subscription_created"

        # Handle billing - should NOT detect trial, return actual amount
        amount = stripe_plugin._handle_stripe_billing(event_type, data)
        assert amount == 26.60  # Actual payment amount
        assert data.get("_is_trial") is None  # Not a trial

        # Build event data
        event_data = stripe_plugin._build_stripe_event_data(
            event_type=event_type,
            customer_id=data["customer"],
            data=data,
            amount=amount,
        )

        # Verify NOT trial metadata
        assert event_data["metadata"].get("is_trial") is None

        # Build rich notification
        notification = event_processor.build_rich_notification(
            event_data, customer_data
        )

        # Verify notification shows "New customer!"
        assert "New customer" in notification.headline

    def test_trial_does_not_show_first_payment_insight(
        self,
        stripe_plugin: StripeSourcePlugin,
        event_processor: EventProcessor,
        subscription_created_payload: dict[str, Any],
        customer_data: dict[str, Any],
    ) -> None:
        """Test that trial subscriptions don't show 'First payment' insight."""
        mock_event = Mock()
        mock_event.type = subscription_created_payload["type"]
        mock_event.data.object = subscription_created_payload["data"]["object"]
        mock_event.data.previous_attributes = None

        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        stripe_plugin._handle_stripe_billing(event_type, data)

        # Transform to trial_started
        if data.get("_is_trial"):
            event_type = "trial_started"

        event_data = stripe_plugin._build_stripe_event_data(
            event_type=event_type,
            customer_id=data["customer"],
            data=data,
            amount=0.0,
        )

        # Build notification
        notification = event_processor.build_rich_notification(
            event_data, customer_data
        )

        # Insight should be trial-related, NOT "First payment"
        assert notification.insight is not None
        assert "First payment" not in notification.insight.text
        assert "trial" in notification.insight.text.lower()

    def test_trial_does_not_show_payment_info(
        self,
        stripe_plugin: StripeSourcePlugin,
        event_processor: EventProcessor,
        subscription_created_payload: dict[str, Any],
        customer_data: dict[str, Any],
    ) -> None:
        """Test that trial subscriptions don't show payment details.

        Trials should not display payment info since no payment has occurred.
        This ensures customer success teams see it as a trial, not a payment.
        """
        mock_event = Mock()
        mock_event.type = subscription_created_payload["type"]
        mock_event.data.object = subscription_created_payload["data"]["object"]
        mock_event.data.previous_attributes = None

        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        stripe_plugin._handle_stripe_billing(event_type, data)

        # Transform to trial_started
        if data.get("_is_trial"):
            event_type = "trial_started"

        event_data = stripe_plugin._build_stripe_event_data(
            event_type=event_type,
            customer_id=data["customer"],
            data=data,
            amount=0.0,
        )

        # Build notification
        notification = event_processor.build_rich_notification(
            event_data, customer_data
        )

        # Payment info should be None for trials
        assert notification.payment is None, "Trials should not have payment info"

        # Amount in event data should be 0
        assert event_data["amount"] == 0.0

    def test_zero_amount_invoice_filtered_before_notification(
        self,
        consolidation_service: EventConsolidationService,
        invoice_paid_zero_payload: dict[str, Any],
    ) -> None:
        """Test that $0 invoice.paid is filtered and no notification generated."""
        # This event should be filtered by consolidation service
        should_notify = consolidation_service.should_send_notification(
            event_type="invoice_paid",
            customer_id="cus_TestCustomer123",
            workspace_id="ws_test123",
            amount=0.0,  # $0 trial invoice
        )

        assert should_notify is False

    def test_full_trial_signup_flow_single_notification(
        self,
        stripe_plugin: StripeSourcePlugin,
        event_processor: EventProcessor,
        consolidation_service: EventConsolidationService,
        subscription_created_payload: dict[str, Any],
        invoice_paid_zero_payload: dict[str, Any],
        customer_data: dict[str, Any],
    ) -> None:
        """Integration test: Full trial signup flow produces exactly 1 notification."""
        notifications_sent: list[dict[str, Any]] = []

        with patch("webhooks.services.event_consolidation.cache") as mock_cache:
            mock_cache.get.return_value = None

            # Event 1: subscription.created (with status=trialing)
            mock_event_1 = Mock()
            mock_event_1.type = subscription_created_payload["type"]
            mock_event_1.data.object = subscription_created_payload["data"]["object"]
            mock_event_1.data.previous_attributes = None

            event_type_1, data_1 = stripe_plugin._extract_stripe_event_info(
                mock_event_1
            )
            amount_1 = stripe_plugin._handle_stripe_billing(event_type_1, data_1)

            # Transform to trial_started if it's a trial (as done in parse_webhook)
            if data_1.get("_is_trial"):
                event_type_1 = "trial_started"

            event_data_1 = stripe_plugin._build_stripe_event_data(
                event_type_1, data_1["customer"], data_1, amount_1
            )

            # Verify this is now a trial_started event
            assert event_data_1["type"] == "trial_started"
            assert event_data_1["metadata"]["is_trial"] is True

            if consolidation_service.should_send_notification(
                event_type_1, data_1["customer"], "ws_test", amount_1
            ):
                slack_message = event_processor.process_event_rich(
                    event_data_1, customer_data, target="slack"
                )
                notifications_sent.append(slack_message)

            # Event 2: invoice.paid ($0) - should be filtered
            mock_event_2 = Mock()
            mock_event_2.type = invoice_paid_zero_payload["type"]
            mock_event_2.data.object = invoice_paid_zero_payload["data"]["object"]
            mock_event_2.data.previous_attributes = None

            event_type_2, data_2 = stripe_plugin._extract_stripe_event_info(
                mock_event_2
            )
            amount_2 = stripe_plugin._handle_stripe_billing(event_type_2, data_2)

            if consolidation_service.should_send_notification(
                event_type_2, data_2["customer"], "ws_test", amount_2
            ):
                event_data_2 = stripe_plugin._build_stripe_event_data(
                    event_type_2, data_2["customer"], data_2, amount_2
                )
                slack_message = event_processor.process_event_rich(
                    event_data_2, customer_data, target="slack"
                )
                notifications_sent.append(slack_message)

        # Only 1 notification should be sent
        assert len(notifications_sent) == 1


class TestTrialConversionIntegration:
    """Integration test: Trial conversion to paid subscription.

    When a trial converts, we receive invoice.payment_succeeded with:
    - billing_reason="subscription_cycle"
    - Positive amount (first real payment)

    The Slack notification should show trial conversion in headline.
    """

    @pytest.fixture
    def stripe_plugin(self) -> StripeSourcePlugin:
        """Create a Stripe plugin instance."""
        return StripeSourcePlugin()

    @pytest.fixture
    def event_processor(self) -> EventProcessor:
        """Create an event processor instance."""
        return EventProcessor()

    @pytest.fixture
    def trial_conversion_payload(self) -> dict[str, Any]:
        """Invoice payload for first real payment after trial."""
        return {
            "id": "evt_conversion123",
            "object": "event",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_conversion123",
                    "object": "invoice",
                    "customer": "cus_TestCustomer123",
                    "amount_due": 2660,
                    "amount_paid": 2660,
                    "amount_remaining": 0,
                    "currency": "usd",
                    "status": "paid",
                    "billing_reason": "subscription_cycle",
                    "subscription": "sub_1ABC123DEF456GHI",
                    "created": 1770537317,
                },
            },
        }

    @pytest.fixture
    def customer_data(self) -> dict[str, Any]:
        """Customer data."""
        return {
            "email": "john.smith@acmecorp.com",
            "first_name": "John",
            "last_name": "Smith",
            "company": "Acme Corp",
        }

    def test_trial_conversion_detected_in_metadata(
        self,
        stripe_plugin: StripeSourcePlugin,
        trial_conversion_payload: dict[str, Any],
    ) -> None:
        """Test that trial conversion is detected in event metadata."""
        # Parse the webhook
        mock_event = Mock()
        mock_event.type = trial_conversion_payload["type"]
        mock_event.data.object = trial_conversion_payload["data"]["object"]
        mock_event.data.previous_attributes = None

        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        assert event_type == "payment_success"

        # Handle billing - should detect trial conversion
        amount = stripe_plugin._handle_stripe_billing(event_type, data)
        assert amount == 26.60
        assert data.get("_is_trial_conversion") is True

        # Build event data
        event_data = stripe_plugin._build_stripe_event_data(
            event_type, data["customer"], data, amount
        )
        assert event_data["metadata"]["is_trial_conversion"] is True

    def test_trial_conversion_generates_rich_notification(
        self,
        stripe_plugin: StripeSourcePlugin,
        event_processor: EventProcessor,
        trial_conversion_payload: dict[str, Any],
        customer_data: dict[str, Any],
    ) -> None:
        """Test that trial conversion produces a RichNotification with metadata."""
        mock_event = Mock()
        mock_event.type = trial_conversion_payload["type"]
        mock_event.data.object = trial_conversion_payload["data"]["object"]
        mock_event.data.previous_attributes = None

        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        stripe_plugin._handle_stripe_billing(event_type, data)
        event_data = stripe_plugin._build_stripe_event_data(
            event_type, data["customer"], data, 26.60
        )

        # Build notification
        notification = event_processor.build_rich_notification(
            event_data, customer_data
        )

        assert isinstance(notification, RichNotification)
        # Headlines are event-focused (no company name)
        assert "Trial converted" in notification.headline


class TestSubscriptionUpgradeIntegration:
    """Integration test: Subscription plan upgrade.

    When a customer upgrades, subscription.updated event includes
    previous_attributes with old plan amount.

    Slack notification should indicate it's an upgrade.
    """

    @pytest.fixture
    def stripe_plugin(self) -> StripeSourcePlugin:
        """Create a Stripe plugin instance."""
        return StripeSourcePlugin()

    @pytest.fixture
    def event_processor(self) -> EventProcessor:
        """Create an event processor instance."""
        return EventProcessor()

    @pytest.fixture
    def upgrade_payload(self) -> dict[str, Any]:
        """Subscription updated payload showing upgrade."""
        return {
            "id": "evt_upgrade123",
            "object": "event",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_1ABC123DEF456GHI",
                    "object": "subscription",
                    "customer": "cus_TestCustomer123",
                    "status": "active",
                    "currency": "usd",
                    "plan": {
                        "id": "price_pro_monthly",
                        "amount": 4900,  # $49/mo (new)
                        "currency": "usd",
                        "interval": "month",
                    },
                },
                "previous_attributes": {
                    "plan": {
                        "id": "price_basic_monthly",
                        "amount": 2660,  # $26.60/mo (old)
                    },
                },
            },
        }

    @pytest.fixture
    def customer_data(self) -> dict[str, Any]:
        """Customer data."""
        return {
            "email": "jane@bigcorp.com",
            "first_name": "Jane",
            "last_name": "Doe",
            "company": "BigCorp Inc",
        }

    def test_upgrade_detected_in_metadata(
        self,
        stripe_plugin: StripeSourcePlugin,
        upgrade_payload: dict[str, Any],
    ) -> None:
        """Test that upgrade is detected via previous_attributes."""
        mock_event = Mock()
        mock_event.type = upgrade_payload["type"]
        mock_event.data.object = upgrade_payload["data"]["object"]
        mock_event.data.previous_attributes = upgrade_payload["data"][
            "previous_attributes"
        ]

        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        assert event_type == "subscription_updated"
        assert "_previous_attributes" in data

        # Handle billing - should detect upgrade
        amount = stripe_plugin._handle_stripe_billing(event_type, data)
        assert amount == 49.00
        assert data.get("_change_direction") == "upgrade"

        # Build event data
        event_data = stripe_plugin._build_stripe_event_data(
            event_type, data["customer"], data, amount
        )
        assert event_data["metadata"]["change_direction"] == "upgrade"

    def test_upgrade_generates_rich_notification(
        self,
        stripe_plugin: StripeSourcePlugin,
        event_processor: EventProcessor,
        upgrade_payload: dict[str, Any],
        customer_data: dict[str, Any],
    ) -> None:
        """Test that upgrade produces a RichNotification."""
        mock_event = Mock()
        mock_event.type = upgrade_payload["type"]
        mock_event.data.object = upgrade_payload["data"]["object"]
        mock_event.data.previous_attributes = upgrade_payload["data"][
            "previous_attributes"
        ]

        event_type, data = stripe_plugin._extract_stripe_event_info(mock_event)
        stripe_plugin._handle_stripe_billing(event_type, data)
        event_data = stripe_plugin._build_stripe_event_data(
            event_type, data["customer"], data, 49.00
        )

        # Build notification
        notification = event_processor.build_rich_notification(
            event_data, customer_data
        )

        assert isinstance(notification, RichNotification)
        # Headlines are event-focused (show upgrade with amounts)
        assert "Upgraded" in notification.headline
        # Should show old and new amounts
        assert "$26.60" in notification.headline or "$49.00" in notification.headline


class TestDisplayNameInRichNotification:
    """Integration test: Customer display name in RichNotification.

    Tests the fallback logic for display names:
    - Company name if available
    - Customer name if no company
    - Email domain for business emails
    - Email username for free email providers
    """

    @pytest.fixture
    def event_processor(self) -> EventProcessor:
        """Create an event processor instance."""
        return EventProcessor()

    @pytest.fixture
    def payment_event_data(self) -> dict[str, Any]:
        """Generic payment event data."""
        return {
            "type": "payment_success",
            "customer_id": "cus_123",
            "provider": "stripe",
            "external_id": "evt_123",
            "status": "succeeded",
            "created_at": 1769327717,
            "currency": "USD",
            "amount": 99.00,
            "metadata": {},
        }

    def test_company_name_in_headline(
        self,
        event_processor: EventProcessor,
        payment_event_data: dict[str, Any],
    ) -> None:
        """Test that company info is available in notification (not headline).

        Headlines are now event-focused. Customer info is in CustomerInfo.
        """
        customer_data = {
            "email": "billing@acme.com",
            "first_name": "John",
            "last_name": "Doe",
            "company": "Acme Corporation",
        }

        notification = event_processor.build_rich_notification(
            payment_event_data, customer_data
        )

        # Headlines are event-focused
        assert "$99.00" in notification.headline
        # Company info is in CustomerInfo
        assert notification.customer is not None
        assert notification.customer.company_name == "Acme Corporation"

    def test_personal_name_in_headline_when_no_company(
        self,
        event_processor: EventProcessor,
        payment_event_data: dict[str, Any],
    ) -> None:
        """Test customer name is available in CustomerInfo when no company."""
        customer_data = {
            "email": "john.doe@gmail.com",
            "first_name": "John",
            "last_name": "Doe",
            "company": "",
        }

        notification = event_processor.build_rich_notification(
            payment_event_data, customer_data
        )

        # Headlines are event-focused
        assert "$99.00" in notification.headline
        # Name is in CustomerInfo
        assert notification.customer is not None
        assert notification.customer.name == "John Doe"

    def test_business_email_in_headline(
        self,
        event_processor: EventProcessor,
        payment_event_data: dict[str, Any],
    ) -> None:
        """Test email is available in CustomerInfo when no name/company."""
        customer_data = {
            "email": "billing@techstartup.io",
            "first_name": "",
            "last_name": "",
            "company": "",
        }

        notification = event_processor.build_rich_notification(
            payment_event_data, customer_data
        )

        # Headlines are event-focused
        assert "$99.00" in notification.headline
        # Email is in CustomerInfo
        assert notification.customer is not None
        assert notification.customer.email == "billing@techstartup.io"

    def test_gmail_email_in_headline(
        self,
        event_processor: EventProcessor,
        payment_event_data: dict[str, Any],
    ) -> None:
        """Test email is available in CustomerInfo for free email providers."""
        customer_data = {
            "email": "cooluser123@gmail.com",
            "first_name": "",
            "last_name": "",
            "company": "",
        }

        notification = event_processor.build_rich_notification(
            payment_event_data, customer_data
        )

        # Headlines are event-focused
        assert "$99.00" in notification.headline
        # Email is in CustomerInfo
        assert notification.customer is not None
        assert notification.customer.email == "cooluser123@gmail.com"

    def test_individual_ignored_in_headline(
        self,
        event_processor: EventProcessor,
        payment_event_data: dict[str, Any],
    ) -> None:
        """Test 'Individual' company handling in CustomerInfo."""
        customer_data = {
            "email": "someone@enterprise.com",
            "first_name": "",
            "last_name": "",
            "company": "Individual",
        }

        notification = event_processor.build_rich_notification(
            payment_event_data, customer_data
        )

        # Headlines are event-focused (no customer info)
        assert "$99.00" in notification.headline
        # Email is in CustomerInfo
        assert notification.customer is not None
        assert notification.customer.email == "someone@enterprise.com"


class TestWebhookCustomerDataExtraction:
    """Integration test: Customer data extraction from webhook payload.

    We can't call Stripe API (don't have customer's API key), so customer
    data must be extracted directly from the webhook payload via get_customer_data().
    """

    @pytest.fixture
    def stripe_plugin(self) -> StripeSourcePlugin:
        """Create a Stripe plugin instance."""
        return StripeSourcePlugin()

    def test_get_customer_data_extracts_email_from_webhook(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that customer_email is extracted from stored webhook data."""
        # Simulate webhook data being stored during parse_webhook
        stripe_plugin._current_webhook_data = {
            "id": "in_test123",
            "customer": "cus_test123",
            "customer_email": "realuser@example.com",
            "customer_name": None,  # Often null in Stripe
        }

        customer_data = stripe_plugin.get_customer_data("cus_test123")

        assert customer_data["email"] == "realuser@example.com"
        assert customer_data["first_name"] == ""
        assert customer_data["last_name"] == ""

    def test_get_customer_data_extracts_name_from_webhook(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that customer_name is split into first/last from webhook data."""
        stripe_plugin._current_webhook_data = {
            "id": "sub_test123",
            "customer": "cus_test123",
            "customer_email": "subscriber@company.com",
            "customer_name": "John Doe",
        }

        customer_data = stripe_plugin.get_customer_data("cus_test123")

        assert customer_data["email"] == "subscriber@company.com"
        assert customer_data["first_name"] == "John"
        assert customer_data["last_name"] == "Doe"

    def test_get_customer_data_handles_single_name(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that single word name is handled correctly."""
        stripe_plugin._current_webhook_data = {
            "id": "in_test123",
            "customer": "cus_test123",
            "customer_email": "prince@music.com",
            "customer_name": "Prince",
        }

        customer_data = stripe_plugin.get_customer_data("cus_test123")

        assert customer_data["first_name"] == "Prince"
        assert customer_data["last_name"] == ""

    def test_get_customer_data_returns_empty_when_no_webhook_data(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that empty data is returned when no webhook data available."""
        stripe_plugin._current_webhook_data = None

        customer_data = stripe_plugin.get_customer_data("cus_test123")

        assert customer_data["email"] == ""
        assert customer_data["first_name"] == ""
        assert customer_data["last_name"] == ""
        assert customer_data["company_name"] == ""

    def test_idempotency_key_extracted_correctly(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that idempotency_key is extracted from event request."""
        mock_event = Mock()
        mock_event.request = Mock()
        mock_event.request.idempotency_key = "unique-key-12345"

        result = stripe_plugin._extract_idempotency_key(mock_event)
        assert result == "unique-key-12345"

    def test_idempotency_key_none_when_no_request(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that idempotency_key is None when request is None."""
        mock_event = Mock()
        mock_event.request = None

        result = stripe_plugin._extract_idempotency_key(mock_event)
        assert result is None

    def test_idempotency_key_handles_dict_request(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that idempotency_key works with dict-style request."""
        mock_event = Mock()
        mock_event.request = {"idempotency_key": "dict-key-67890", "id": "req_123"}

        result = stripe_plugin._extract_idempotency_key(mock_event)
        assert result == "dict-key-67890"

    def test_cache_customer_email_from_invoice(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that customer email is cached from invoice events."""
        with patch("plugins.sources.stripe.cache") as mock_cache:
            stripe_plugin._cache_customer_email("cus_test123", "test@example.com")

            mock_cache.set.assert_called_once()
            call_args = mock_cache.set.call_args
            assert call_args[0][0] == "stripe_customer_email:cus_test123"
            assert call_args[0][1] == "test@example.com"

    def test_get_cached_customer_email(self, stripe_plugin: StripeSourcePlugin) -> None:
        """Test that cached customer email is retrieved."""
        with patch("plugins.sources.stripe.cache") as mock_cache:
            mock_cache.get.return_value = "cached@example.com"

            result = stripe_plugin._get_cached_customer_email("cus_test123")

            assert result == "cached@example.com"
            mock_cache.get.assert_called_once_with("stripe_customer_email:cus_test123")

    def test_get_customer_data_uses_cached_email_for_subscription(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that subscription events use cached email when not in payload.

        Subscription events don't include customer_email, but invoice events do.
        We cache the email from invoice events and use it for subscriptions.
        """
        with patch("plugins.sources.stripe.cache") as mock_cache:
            mock_cache.get.return_value = "cached@company.com"

            # Simulate subscription webhook data (no customer_email)
            stripe_plugin._current_webhook_data = {
                "id": "sub_test123",
                "customer": "cus_test123",
                "plan": {"amount": 2660},
                # Note: no customer_email field (subscriptions don't have it)
            }

            customer_data = stripe_plugin.get_customer_data("cus_test123")

            # Should have looked up cached email
            mock_cache.get.assert_called_once_with("stripe_customer_email:cus_test123")
            assert customer_data["email"] == "cached@company.com"

    def test_get_customer_data_prefers_webhook_email_over_cache(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test that webhook email takes precedence over cached email.

        When the webhook payload contains customer_email, we should use it
        instead of looking up the cache.
        """
        with patch("plugins.sources.stripe.cache") as mock_cache:
            mock_cache.get.return_value = "cached@old.com"

            # Simulate invoice webhook data (has customer_email)
            stripe_plugin._current_webhook_data = {
                "id": "in_test123",
                "customer": "cus_test123",
                "customer_email": "invoice@new.com",
            }

            customer_data = stripe_plugin.get_customer_data("cus_test123")

            # Should NOT have looked up cache since webhook has email
            mock_cache.get.assert_not_called()
            assert customer_data["email"] == "invoice@new.com"


class TestExtractPlanName:
    """Test plan name extraction from invoice line items.

    Tests structured field lookups (plan/price objects) and fallback
    to description parsing.
    """

    @pytest.fixture
    def stripe_plugin(self) -> StripeSourcePlugin:
        """Create a Stripe plugin instance."""
        return StripeSourcePlugin()

    def test_prefers_plan_nickname(self, stripe_plugin: StripeSourcePlugin) -> None:
        """Test that plan.nickname is preferred over description parsing."""
        item: dict[str, Any] = {
            "plan": {"nickname": "Business Monthly", "name": "price_123"},
            "description": "2 screen × Something Else (at $26.60 / month)",
        }
        assert (
            stripe_plugin._extract_plan_name_from_line_item(item) == "Business Monthly"
        )

    def test_falls_back_to_plan_name(self, stripe_plugin: StripeSourcePlugin) -> None:
        """Test fallback to plan.name when nickname is absent."""
        item: dict[str, Any] = {
            "plan": {"name": "Pro Plan"},
            "description": "Something in description",
        }
        assert stripe_plugin._extract_plan_name_from_line_item(item) == "Pro Plan"

    def test_uses_price_nickname(self, stripe_plugin: StripeSourcePlugin) -> None:
        """Test price.nickname extraction."""
        item: dict[str, Any] = {
            "price": {"nickname": "Enterprise Annual"},
            "description": "Fallback description",
        }
        assert (
            stripe_plugin._extract_plan_name_from_line_item(item) == "Enterprise Annual"
        )

    def test_uses_expanded_product_name(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test price.product.name when product is expanded."""
        item: dict[str, Any] = {
            "price": {"product": {"name": "Starter Plan"}},
            "description": "Fallback",
        }
        assert stripe_plugin._extract_plan_name_from_line_item(item) == "Starter Plan"

    def test_uses_new_api_pricing_field(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test new API pricing.price_details with expanded objects."""
        item: dict[str, Any] = {
            "pricing": {
                "type": "price_details",
                "price_details": {
                    "price": {"nickname": "Growth Plan"},
                },
            },
            "description": "Fallback",
        }
        assert stripe_plugin._extract_plan_name_from_line_item(item) == "Growth Plan"

    def test_falls_back_to_description_quantity_format(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test description fallback: '2 screen × Plan (at $X / month)'."""
        item: dict[str, Any] = {
            "description": "2 screen × Business Plan Monthly (at $26.60 / month)",
        }
        assert (
            stripe_plugin._extract_plan_name_from_line_item(item)
            == "Business Plan Monthly"
        )

    def test_falls_back_to_description_trial_format(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test description fallback: 'Trial period for Plan (details)'."""
        item: dict[str, Any] = {
            "description": "Trial period for Business Plan Monthly (per screen)",
        }
        assert (
            stripe_plugin._extract_plan_name_from_line_item(item)
            == "Business Plan Monthly"
        )

    def test_falls_back_to_description_simple_format(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test description fallback: 'Plan (details)'."""
        item: dict[str, Any] = {
            "description": "Business Plan Monthly (per screen)",
        }
        assert (
            stripe_plugin._extract_plan_name_from_line_item(item)
            == "Business Plan Monthly"
        )

    def test_falls_back_to_description_plain_name(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test description fallback: plain plan name."""
        item: dict[str, Any] = {"description": "Pro Plan Annual"}
        assert (
            stripe_plugin._extract_plan_name_from_line_item(item) == "Pro Plan Annual"
        )

    def test_empty_item_returns_none(self, stripe_plugin: StripeSourcePlugin) -> None:
        """Test empty line item returns None."""
        assert stripe_plugin._extract_plan_name_from_line_item({}) is None
        assert (
            stripe_plugin._extract_plan_name_from_line_item({"description": ""}) is None
        )

    def test_skips_plan_with_no_name(self, stripe_plugin: StripeSourcePlugin) -> None:
        """Test that plan object without nickname/name falls through."""
        item: dict[str, Any] = {
            "plan": {"amount": 2660, "interval": "month"},
            "description": "Business Plan Monthly (per screen)",
        }
        assert (
            stripe_plugin._extract_plan_name_from_line_item(item)
            == "Business Plan Monthly"
        )


class TestInvoiceMetadataExtraction:
    """Test expanded _add_invoice_metadata() with real invoice structures."""

    @pytest.fixture
    def stripe_plugin(self) -> StripeSourcePlugin:
        """Create a Stripe plugin instance."""
        return StripeSourcePlugin()

    def test_extracts_plan_name_from_line_items(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test plan_name is extracted from line item description."""
        metadata: dict[str, Any] = {}
        data: dict[str, Any] = {
            "subscription": "sub_123",
            "billing_reason": "subscription_cycle",
            "attempt_count": 1,
            "number": "INV-0042",
            "lines": {
                "data": [
                    {
                        "description": (
                            "2 screen × Business Plan Monthly"
                            " (at $26.60 / month)"
                        ),
                        "quantity": 2,
                    }
                ]
            },
        }

        stripe_plugin._add_invoice_metadata(metadata, data)

        assert metadata["plan_name"] == "Business Plan Monthly"
        assert metadata["subscription_id"] == "sub_123"
        assert metadata["billing_reason"] == "subscription_cycle"
        assert metadata["attempt_count"] == 1
        assert metadata["invoice_number"] == "INV-0042"
        assert metadata["quantity"] == 2
        assert metadata["billing_period"] == "monthly"

    def test_extracts_subscription_id_from_new_api_path(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test subscription_id extraction from new Stripe API path."""
        metadata: dict[str, Any] = {}
        data: dict[str, Any] = {
            "subscription": None,
            "parent": {
                "subscription_details": {
                    "subscription": "sub_new_api_456",
                }
            },
            "billing_reason": "subscription_cycle",
            "lines": {"data": []},
        }

        stripe_plugin._add_invoice_metadata(metadata, data)

        assert metadata["subscription_id"] == "sub_new_api_456"
        assert metadata["billing_period"] == "monthly"

    def test_extracts_attempt_count_and_next_retry(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test attempt_count and next_payment_attempt for failures."""
        metadata: dict[str, Any] = {}
        data: dict[str, Any] = {
            "attempt_count": 2,
            "next_payment_attempt": 1740182400,
            "billing_reason": "subscription_cycle",
            "lines": {"data": []},
        }

        stripe_plugin._add_invoice_metadata(metadata, data)

        assert metadata["attempt_count"] == 2
        assert metadata["next_payment_attempt"] == 1740182400

    def test_annual_billing_period_from_description(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test billing period parsed as annual from description."""
        metadata: dict[str, Any] = {}
        data: dict[str, Any] = {
            "subscription": "sub_123",
            "billing_reason": "subscription_cycle",
            "lines": {
                "data": [
                    {
                        "description": "Enterprise Plan (at $999.00 / year)",
                        "quantity": 1,
                    }
                ]
            },
        }

        stripe_plugin._add_invoice_metadata(metadata, data)

        assert metadata["plan_name"] == "Enterprise Plan"
        assert metadata["billing_period"] == "annual"

    def test_no_line_items_still_extracts_other_metadata(
        self, stripe_plugin: StripeSourcePlugin
    ) -> None:
        """Test metadata extraction works even without line items."""
        metadata: dict[str, Any] = {}
        data: dict[str, Any] = {
            "subscription": "sub_789",
            "billing_reason": "subscription_create",
            "number": "INV-0001",
            "lines": {"data": []},
        }

        stripe_plugin._add_invoice_metadata(metadata, data)

        assert metadata["subscription_id"] == "sub_789"
        assert metadata["billing_reason"] == "subscription_create"
        assert metadata["invoice_number"] == "INV-0001"
        assert metadata["billing_period"] == "monthly"


class TestPaymentFailureNotFiltered:
    """Integration test: Payment failures are never filtered.

    Payment failures are critical events that should always generate
    notifications, regardless of amount or consolidation.
    """

    @pytest.fixture
    def consolidation_service(self) -> EventConsolidationService:
        """Create a consolidation service instance."""
        return EventConsolidationService()

    @pytest.fixture
    def event_processor(self) -> EventProcessor:
        """Create an event processor instance."""
        return EventProcessor()

    def test_payment_failure_not_filtered_even_with_zero_amount(
        self,
        consolidation_service: EventConsolidationService,
    ) -> None:
        """Test that payment failures pass through the filter."""
        with patch("webhooks.services.event_consolidation.cache") as mock_cache:
            mock_cache.get.return_value = None

            should_notify = consolidation_service.should_send_notification(
                event_type="payment_failure",
                customer_id="cus_123",
                workspace_id="ws_test",
                amount=0.0,  # Even with $0, should NOT be filtered
            )

            assert should_notify is True

    def test_payment_failure_generates_error_notification(
        self,
        event_processor: EventProcessor,
    ) -> None:
        """Test that payment failure generates error severity notification."""
        from webhooks.models.rich_notification import NotificationSeverity

        event_data = {
            "type": "payment_failure",
            "customer_id": "cus_123",
            "provider": "stripe",
            "external_id": "evt_fail123",
            "status": "failed",
            "created_at": 1769327717,
            "currency": "USD",
            "amount": 99.00,
            "metadata": {},
        }
        customer_data = {
            "email": "billing@company.com",
            "first_name": "John",
            "last_name": "Doe",
            "company": "Test Company",
        }

        notification = event_processor.build_rich_notification(
            event_data, customer_data
        )

        # Payment failures should have error severity
        assert notification.severity == NotificationSeverity.ERROR

        # Headlines are event-focused (no company name)
        assert "failed" in notification.headline.lower()
        assert "$99.00" in notification.headline

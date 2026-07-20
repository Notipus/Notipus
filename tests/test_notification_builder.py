"""Tests for the NotificationBuilder service.

This module tests the NotificationBuilder class that creates
RichNotification objects from event and customer data.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from webhooks.models.rich_notification import (
    EventCategory,
    NotificationSeverity,
    NotificationType,
    RichNotification,
)
from webhooks.services.notification_builder import NotificationBuilder


@pytest.fixture
def builder() -> NotificationBuilder:
    """Create a NotificationBuilder instance."""
    return NotificationBuilder()


@pytest.fixture
def payment_success_event() -> dict:
    """Sample payment success event data."""
    return {
        "type": "payment_success",
        "provider": "stripe",
        "customer_id": "cus_abc123",
        "amount": 299.00,
        "currency": "USD",
        "metadata": {
            "plan_name": "Enterprise",
            "subscription_id": "sub_123",
            "billing_period": "monthly",
            "card_brand": "visa",
            "card_last4": "4242",
        },
    }


@pytest.fixture
def payment_failure_event() -> dict:
    """Sample payment failure event data."""
    return {
        "type": "payment_failure",
        "provider": "chargify",
        "amount": 99.00,
        "currency": "USD",
        "metadata": {
            "plan_name": "Pro",
            "subscription_id": "sub_456",
            "failure_reason": "Card declined",
        },
    }


@pytest.fixture
def subscription_created_event() -> dict:
    """Sample subscription created event data."""
    return {
        "type": "subscription_created",
        "provider": "stripe",
        "amount": 49.00,
        "currency": "USD",
        "metadata": {
            "plan_name": "Starter",
            "subscription_id": "sub_789",
            "billing_period": "monthly",
        },
    }


@pytest.fixture
def customer_data() -> dict:
    """Sample customer data."""
    return {
        "email": "alice@acme.com",
        "first_name": "Alice",
        "last_name": "Smith",
        "company_name": "Acme Inc",
        "orders_count": 5,
        "total_spent": 1500.00,
        "created_at": "2024-03-15T10:00:00Z",
    }


@pytest.fixture
def new_customer_data() -> dict:
    """Sample data for a new customer (first payment)."""
    return {
        "email": "bob@newco.com",
        "first_name": "Bob",
        "last_name": "Jones",
        "company_name": "NewCo",
        "orders_count": 0,
        "total_spent": 0,
    }


@pytest.fixture
def mock_company() -> MagicMock:
    """Create a mock Company object."""
    company = MagicMock()
    company.domain = "acme.com"
    company.name = "Acme Corporation"
    company.has_logo = True
    company.get_logo_url.return_value = "https://example.com/logo.png"
    company.brand_info = {
        "name": "Acme Corporation",
        "industry": "Technology",
        "year_founded": 2015,
        "employee_count": "51-200",
        "description": "Acme Corporation builds tools for developers.",
        "logo_url": "https://example.com/logo.png",
        "links": [
            {"name": "linkedin", "url": "https://linkedin.com/company/acme-corp"},
            {"name": "twitter", "url": "https://twitter.com/acme"},
        ],
    }
    return company


class TestNotificationBuilderBasic:
    """Test basic NotificationBuilder functionality."""

    def test_build_returns_rich_notification(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test that build returns a RichNotification object."""
        result = builder.build(payment_success_event, customer_data)

        assert isinstance(result, RichNotification)

    def test_build_requires_event_data(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test that build raises ValueError for missing event data."""
        with pytest.raises(ValueError, match="Missing event data"):
            builder.build({}, customer_data)

        with pytest.raises(ValueError, match="Missing event data"):
            builder.build(None, customer_data)  # type: ignore

    def test_build_requires_customer_data(
        self, builder: NotificationBuilder, payment_success_event: dict
    ) -> None:
        """Test that build raises ValueError for missing customer data."""
        with pytest.raises(ValueError, match="Missing customer data"):
            builder.build(payment_success_event, {})

        with pytest.raises(ValueError, match="Missing customer data"):
            builder.build(payment_success_event, None)  # type: ignore

    def test_build_requires_event_type(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test that build raises ValueError for missing event type."""
        with pytest.raises(ValueError, match="Missing event type"):
            builder.build({"provider": "stripe"}, customer_data)


class TestNotificationTypes:
    """Test notification type detection."""

    def test_payment_success_type(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test payment success notification type."""
        result = builder.build(payment_success_event, customer_data)

        assert result.type == NotificationType.PAYMENT_SUCCESS
        assert result.severity == NotificationSeverity.SUCCESS
        assert result.headline_icon == "money"

    def test_payment_failure_type(
        self,
        builder: NotificationBuilder,
        payment_failure_event: dict,
        customer_data: dict,
    ) -> None:
        """Test payment failure notification type."""
        result = builder.build(payment_failure_event, customer_data)

        assert result.type == NotificationType.PAYMENT_FAILURE
        assert result.severity == NotificationSeverity.ERROR
        assert result.headline_icon == "error"

    def test_subscription_created_type(
        self,
        builder: NotificationBuilder,
        subscription_created_event: dict,
        customer_data: dict,
    ) -> None:
        """Test subscription created notification type."""
        result = builder.build(subscription_created_event, customer_data)

        assert result.type == NotificationType.SUBSCRIPTION_CREATED
        # New subscription is a positive event
        assert result.severity == NotificationSeverity.SUCCESS
        assert result.headline_icon == "celebration"

    def test_subscription_canceled_type(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test subscription canceled notification type."""
        event = {"type": "subscription_canceled", "provider": "stripe"}
        result = builder.build(event, customer_data)

        assert result.type == NotificationType.SUBSCRIPTION_CANCELED
        assert result.severity == NotificationSeverity.WARNING
        assert result.headline_icon == "warning"


class TestHeadlineBuilding:
    """Test headline generation."""

    def test_payment_success_headline_with_amount(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test payment success headline includes amount (event-focused, no company)."""
        result = builder.build(payment_success_event, customer_data)

        assert "$299.00" in result.headline
        assert "received" in result.headline.lower()

    def test_payment_failure_headline(
        self,
        builder: NotificationBuilder,
        payment_failure_event: dict,
        customer_data: dict,
    ) -> None:
        """Test payment failure headline (event-focused, no company)."""
        result = builder.build(payment_failure_event, customer_data)

        assert "failed" in result.headline.lower()
        assert "payment" in result.headline.lower()

    def test_subscription_created_headline(
        self,
        builder: NotificationBuilder,
        subscription_created_event: dict,
        new_customer_data: dict,
    ) -> None:
        """Test subscription created headline."""
        result = builder.build(subscription_created_event, new_customer_data)

        assert "New" in result.headline or "subscription" in result.headline.lower()

    def test_company_info_available_when_enriched(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        mock_company: MagicMock,
    ) -> None:
        """Test that enriched company info is available in notification."""
        result = builder.build(payment_success_event, customer_data, mock_company)

        # Company name is shown in body (CompanyInfo), not headline
        assert result.company is not None
        assert result.company.name == "Acme Corporation"


class TestPaymentInfo:
    """Test PaymentInfo extraction."""

    def test_payment_info_extracted(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test that payment info is extracted correctly."""
        result = builder.build(payment_success_event, customer_data)

        assert result.payment is not None
        assert result.payment.amount == 299.00
        assert result.payment.currency == "USD"
        assert result.payment.plan_name == "Enterprise"
        assert result.payment.subscription_id == "sub_123"

    def test_payment_method_extraction(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test payment method is extracted."""
        result = builder.build(payment_success_event, customer_data)

        assert result.payment is not None
        assert result.payment.payment_method == "visa"
        assert result.payment.card_last4 == "4242"

    def test_recurring_detection_with_subscription(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test recurring payment detection."""
        result = builder.build(payment_success_event, customer_data)

        assert result.is_recurring is True
        assert result.billing_interval == "monthly"

    def test_one_time_payment_detection(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test one-time payment detection."""
        event = {
            "type": "payment_success",
            "provider": "shopify",
            "amount": 50.00,
            "currency": "USD",
            "metadata": {"order_number": "1234"},
        }
        result = builder.build(event, customer_data)

        assert result.is_recurring is False

    def test_arr_calculation_monthly(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test ARR calculation for monthly payments."""
        result = builder.build(payment_success_event, customer_data)

        assert result.payment is not None
        arr = result.payment.get_arr()
        assert arr == 299.00 * 12


class TestCustomerInfo:
    """Test CustomerInfo building."""

    def test_customer_info_built(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test customer info is built correctly."""
        result = builder.build(payment_success_event, customer_data)

        assert result.customer.email == "alice@acme.com"
        assert result.customer.name == "Alice Smith"
        assert result.customer.company_name == "Acme Inc"
        assert result.customer.orders_count == 5
        assert result.customer.total_spent == 1500.00

    def test_tenure_display_formatted(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test tenure display formatting."""
        result = builder.build(payment_success_event, customer_data)

        assert result.customer.tenure_display is not None
        assert "Since" in result.customer.tenure_display
        assert "Mar 2024" in result.customer.tenure_display

    def test_ltv_display_formatted(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test LTV display formatting."""
        result = builder.build(payment_success_event, customer_data)

        assert result.customer.ltv_display is not None
        assert "$1.5k" in result.customer.ltv_display


class TestCompanyEnrichment:
    """Test company enrichment integration."""

    def test_company_info_built_from_enrichment(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        mock_company: MagicMock,
    ) -> None:
        """Test company info is built from enriched Company model."""
        result = builder.build(payment_success_event, customer_data, mock_company)

        assert result.company is not None
        assert result.company.name == "Acme Corporation"
        assert result.company.domain == "acme.com"
        assert result.company.industry == "Technology"
        assert result.company.year_founded == 2015
        assert result.company.logo_url is not None

    def test_no_company_info_without_enrichment(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test company info is None without enrichment."""
        result = builder.build(payment_success_event, customer_data)

        assert result.company is None

    def test_linkedin_url_extracted_from_brand_info(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        mock_company: MagicMock,
    ) -> None:
        """Test LinkedIn URL is extracted from brand_info links array."""
        result = builder.build(payment_success_event, customer_data, mock_company)

        assert result.company is not None
        assert result.company.linkedin_url == "https://linkedin.com/company/acme-corp"

    def test_linkedin_url_none_when_not_in_links(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test LinkedIn URL is None when not in brand_info links."""
        company = MagicMock()
        company.domain = "test.com"
        company.name = "Test Corp"
        company.has_logo = False
        company.brand_info = {
            "name": "Test Corp",
            "links": [
                {"name": "twitter", "url": "https://twitter.com/test"},
            ],
        }

        result = builder.build(payment_success_event, customer_data, company)

        assert result.company is not None
        assert result.company.linkedin_url is None

    def test_linkedin_url_none_when_no_links(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test LinkedIn URL is None when no links in brand_info."""
        company = MagicMock()
        company.domain = "test.com"
        company.name = "Test Corp"
        company.has_logo = False
        company.brand_info = {
            "name": "Test Corp",
        }

        result = builder.build(payment_success_event, customer_data, company)

        assert result.company is not None
        assert result.company.linkedin_url is None


class TestActionButtons:
    """Test action button generation."""

    def test_stripe_action_buttons(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test Stripe-specific action buttons."""
        result = builder.build(payment_success_event, customer_data)

        assert len(result.actions) > 0
        stripe_buttons = [a for a in result.actions if a.text == "View in Stripe"]
        assert len(stripe_buttons) == 1
        # URL must point at the customer the parser identified
        # (event_data["customer_id"], not a metadata key).
        assert (
            stripe_buttons[0].url == "https://dashboard.stripe.com/customers/cus_abc123"
        )

    def test_stripe_button_omitted_without_customer_id(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test Stripe button is omitted when customer_id is missing."""
        del payment_success_event["customer_id"]
        result = builder.build(payment_success_event, customer_data)

        action_texts = [a.text for a in result.actions]
        assert "View in Stripe" not in action_texts

    def test_chargify_button_with_site_subdomain(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test Chargify button links to the per-site dashboard subdomain."""
        event = {
            "type": "payment_success",
            "provider": "chargify",
            "customer_id": "12345",
            "amount": 99.00,
            "currency": "USD",
            "metadata": {
                "subscription_id": "sub_456",
                "site_subdomain": "acme-billing",
            },
        }
        result = builder.build(event, customer_data)

        chargify_buttons = [a for a in result.actions if a.text == "View in Chargify"]
        assert len(chargify_buttons) == 1
        assert (
            chargify_buttons[0].url
            == "https://acme-billing.chargify.com/subscriptions/sub_456"
        )

    def test_chargify_button_omitted_without_subdomain(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test Chargify button is omitted when the site subdomain is unknown.

        A hardcoded app.chargify.com URL would not resolve for per-site
        Chargify (Maxio) subdomains, so no button is better than a
        broken one.
        """
        event = {
            "type": "payment_success",
            "provider": "chargify",
            "customer_id": "12345",
            "amount": 99.00,
            "currency": "USD",
            "metadata": {"subscription_id": "sub_456"},
        }
        result = builder.build(event, customer_data)

        action_texts = [a.text for a in result.actions]
        assert "View in Chargify" not in action_texts
        assert not any("chargify.com" in a.url for a in result.actions)

    def test_website_button_with_company(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        mock_company: MagicMock,
    ) -> None:
        """Test website button added when company is enriched."""
        result = builder.build(payment_success_event, customer_data, mock_company)

        action_texts = [a.text for a in result.actions]
        assert "Website" in action_texts

    def test_contact_button_on_failure(
        self,
        builder: NotificationBuilder,
        payment_failure_event: dict,
        customer_data: dict,
    ) -> None:
        """Test contact customer button on payment failure."""
        result = builder.build(payment_failure_event, customer_data)

        action_texts = [a.text for a in result.actions]
        assert "Contact Customer" in action_texts


class TestInsightDetection:
    """Test insight detection integration."""

    def test_first_payment_insight(
        self,
        builder: NotificationBuilder,
        subscription_created_event: dict,
        new_customer_data: dict,
    ) -> None:
        """Test first payment insight detection."""
        result = builder.build(subscription_created_event, new_customer_data)

        assert result.insight is not None
        assert (
            "First payment" in result.insight.text or "Welcome" in result.insight.text
        )

    def test_failure_reason_insight(
        self,
        builder: NotificationBuilder,
        payment_failure_event: dict,
        customer_data: dict,
    ) -> None:
        """Test failure reason shown as insight."""
        result = builder.build(payment_failure_event, customer_data)

        assert result.insight is not None
        assert "declined" in result.insight.text.lower()


class TestProviderInfo:
    """Test provider information."""

    def test_stripe_provider_info(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
    ) -> None:
        """Test Stripe provider information."""
        result = builder.build(payment_success_event, customer_data)

        assert result.provider == "stripe"
        assert result.provider_display == "Stripe"

    def test_chargify_provider_info(
        self,
        builder: NotificationBuilder,
        payment_failure_event: dict,
        customer_data: dict,
    ) -> None:
        """Test Chargify provider information."""
        result = builder.build(payment_failure_event, customer_data)

        assert result.provider == "chargify"
        assert result.provider_display == "Chargify"

    def test_shopify_provider_info(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test Shopify provider information."""
        event = {
            "type": "payment_success",
            "provider": "shopify",
            "amount": 100.00,
            "currency": "USD",
            "metadata": {"order_number": "1001"},
        }
        result = builder.build(event, customer_data)

        assert result.provider == "shopify"
        assert result.provider_display == "Shopify"


class TestUnknownEventType:
    """Test handling of event types missing from EVENT_TYPE_MAP."""

    def test_unknown_event_type_maps_to_custom(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test an unmapped event type produces a CUSTOM notification.

        Unknown events (e.g. a future dispute.created) must never be
        rendered as successful payments.
        """
        event = {"type": "dispute_created", "provider": "stripe"}
        result = builder.build(event, customer_data)

        assert result.type == NotificationType.CUSTOM
        assert result.category == EventCategory.CUSTOM
        assert result.type != NotificationType.PAYMENT_SUCCESS
        assert result.is_payment_event is False


class TestZeroAmountHeadlines:
    """Test that zero amounts are treated as real amounts, not missing."""

    @pytest.mark.parametrize("zero_amount", [0, 0.0, Decimal("0")])
    def test_zero_amount_payment_success_headline(
        self,
        builder: NotificationBuilder,
        customer_data: dict,
        zero_amount: object,
    ) -> None:
        """Test a $0 payment renders '$0.00 received', not a bare headline."""
        event = {
            "type": "payment_success",
            "provider": "stripe",
            "customer_id": "cus_abc123",
            "amount": zero_amount,
            "currency": "USD",
            "metadata": {},
        }
        result = builder.build(event, customer_data)

        assert result.headline == "$0.00 received"

    def test_zero_amount_payment_failure_retry_headline(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test a $0 payment failure keeps the retry suffix.

        Stripe's attempt_count includes the initial attempt, so
        attempt_count=3 is the second retry.
        """
        event = {
            "type": "payment_failure",
            "provider": "stripe",
            "customer_id": "cus_abc123",
            "amount": 0,
            "currency": "USD",
            "metadata": {"attempt_count": 3},
        }
        result = builder.build(event, customer_data)

        assert result.headline == "$0.00 payment failed (retry #2)"

    def test_downgrade_to_zero_keeps_amount_change_framing(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test a $299 -> $0 downgrade renders both amounts."""
        event = {
            "type": "subscription_updated",
            "provider": "chargify",
            "customer_id": "12345",
            "amount": 0,
            "currency": "USD",
            "metadata": {
                "change_direction": "downgrade",
                "previous_amount": 299,
                "billing_period": "monthly",
            },
        }
        result = builder.build(event, customer_data)

        assert result.headline == "Downgraded: $299.00/mo to $0.00/mo"

    def test_upgrade_from_zero_keeps_amount_change_framing(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test a $0 -> $99 upgrade renders both amounts."""
        event = {
            "type": "subscription_updated",
            "provider": "chargify",
            "customer_id": "12345",
            "amount": 99,
            "currency": "USD",
            "metadata": {
                "change_direction": "upgrade",
                "previous_amount": 0,
                "billing_period": "monthly",
            },
        }
        result = builder.build(event, customer_data)

        assert result.headline == "Upgraded: $0.00/mo to $99.00/mo"


class TestSubscriptionRenewedHeadline:
    """Test the subscription_renewed headline branch."""

    def test_renewed_headline_with_plan_and_amount(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test renewal headline includes plan, amount and interval."""
        event = {
            "type": "subscription_renewed",
            "provider": "chargify",
            "customer_id": "12345",
            "amount": 299.00,
            "currency": "USD",
            "metadata": {
                "plan_name": "Enterprise",
                "subscription_id": "sub_123",
                "billing_period": "annual",
            },
        }
        result = builder.build(event, customer_data)

        assert result.type == NotificationType.SUBSCRIPTION_RENEWED
        assert result.headline == "Subscription renewed: Enterprise ($299.00/yr)"

    def test_renewed_headline_with_amount_only(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test renewal headline with amount but no plan name."""
        event = {
            "type": "subscription_renewed",
            "provider": "stripe",
            "customer_id": "cus_abc123",
            "amount": 49.00,
            "currency": "USD",
            "metadata": {"billing_period": "monthly"},
        }
        result = builder.build(event, customer_data)

        assert result.headline == "Subscription renewed at $49.00/mo"

    def test_renewed_headline_without_amount(
        self, builder: NotificationBuilder, customer_data: dict
    ) -> None:
        """Test renewal headline falls back gracefully without amount."""
        event = {
            "type": "subscription_renewed",
            "provider": "stripe",
            "customer_id": "cus_abc123",
            "metadata": {},
        }
        result = builder.build(event, customer_data)

        assert result.headline == "Subscription renewed"


class TestSilentFallbackLogging:
    """Test that parse fallbacks are logged instead of silently swallowed."""

    def test_invalid_created_at_logs_warning(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test an unparseable created_at logs a warning and yields no tenure."""
        customer_data["created_at"] = "not-a-date"

        with caplog.at_level(
            "WARNING", logger="webhooks.services.notification_builder"
        ):
            result = builder.build(payment_success_event, customer_data)

        assert result.customer.tenure_display is None
        assert any("tenure" in record.getMessage() for record in caplog.records)

    def test_invalid_total_spent_logs_warning(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test an unparseable lifetime value logs a warning and defaults to 0."""
        customer_data["total_spent"] = "not-a-number"

        with caplog.at_level(
            "WARNING", logger="webhooks.services.notification_builder"
        ):
            result = builder.build(payment_success_event, customer_data)

        assert result.customer.total_spent is None
        assert any("lifetime value" in record.getMessage() for record in caplog.records)

    def test_zero_total_spent_is_not_missing(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test a legitimate zero total_spent parses cleanly.

        A genuinely-zero-spend customer must not fall back to
        lifetime_value and must not trigger a parse warning.
        """
        customer_data["total_spent"] = 0
        customer_data["lifetime_value"] = 500.0

        with caplog.at_level(
            "WARNING", logger="webhooks.services.notification_builder"
        ):
            result = builder.build(payment_success_event, customer_data)

        # Zero spend renders as no LTV display; a fallback to
        # lifetime_value would have produced "$500" here.
        assert result.customer.ltv_display is None
        assert result.customer.total_spent is None
        assert not any(
            "lifetime value" in record.getMessage() for record in caplog.records
        )

    @pytest.mark.parametrize("zero_value", [0.0, Decimal("0")])
    def test_zero_lifetime_value_variants_do_not_warn(
        self,
        builder: NotificationBuilder,
        payment_success_event: dict,
        customer_data: dict,
        caplog: pytest.LogCaptureFixture,
        zero_value: object,
    ) -> None:
        """Test 0.0 and Decimal('0') lifetime values parse without warning."""
        customer_data["total_spent"] = zero_value

        with caplog.at_level(
            "WARNING", logger="webhooks.services.notification_builder"
        ):
            result = builder.build(payment_success_event, customer_data)

        assert result.customer.ltv_display is None
        assert not any(
            "lifetime value" in record.getMessage() for record in caplog.records
        )

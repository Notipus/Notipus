"""Correctness regression tests for the InsightDetector.

These tests pin down three fixes:

1. First-payment detection must NOT fire for providers (Stripe, Chargify)
   that never populate order/payment history - only Shopify does. Absent
   history fields previously defaulted to zero and mislabeled every renewal
   as a first payment.
2. The trial insight must render the event's currency (not a hardcoded "$")
   and the actual billing interval (not a hardcoded "/mo").
3. The LTV milestone must not be claimed one payment early. Shopify's
   customer ``total_spent`` at orders/paid time already includes the current
   order, so the reported value IS the new lifetime value.
"""

import pytest
from webhooks.services.insight_detector import InsightDetector


@pytest.fixture
def detector() -> InsightDetector:
    """Create an InsightDetector with default configuration.

    Returns:
        A default InsightDetector instance.
    """
    return InsightDetector()


class TestFirstPaymentRequiresHistoryFields:
    """First payment only fires when order/payment history is known."""

    def test_stripe_payment_without_history_is_not_first_payment(
        self, detector: InsightDetector
    ) -> None:
        """Test a Stripe payment lacking history fields is not a first payment.

        Stripe's get_customer_data never returns orders_count or
        payment_history, so a monthly renewal must not be mislabeled.
        """
        event = {
            "type": "payment_success",
            "provider": "stripe",
            "amount": 49.00,
            "currency": "USD",
            "metadata": {},
        }
        # Shape mirrors StripeSourcePlugin.get_customer_data: no history keys.
        customer_data = {
            "company_name": "Acme Inc",
            "email": "billing@acme.com",
            "customer_id": "cus_123",
        }

        result = detector._detect_first_payment(event, customer_data)

        assert result is None

    def test_chargify_payment_without_history_is_not_first_payment(
        self, detector: InsightDetector
    ) -> None:
        """Test a Chargify payment lacking history fields is not a first payment.

        Chargify's get_customer_data also omits orders_count and
        payment_history, so its renewals must not be mislabeled either.
        """
        event = {
            "type": "payment_success",
            "provider": "chargify",
            "amount": 99.00,
            "currency": "USD",
            "metadata": {},
        }
        customer_data = {
            "company_name": "Beta LLC",
            "email": "ops@beta.example",
            "customer_id": "42",
            "plan_name": "Pro",
        }

        result = detector._detect_first_payment(event, customer_data)

        assert result is None

    def test_stripe_renewal_does_not_mask_large_payment_insight(
        self, detector: InsightDetector
    ) -> None:
        """Test a large Stripe renewal surfaces the large-payment insight.

        First payment sits high in the priority order; when it stops
        firing spuriously, a lower-priority insight can surface instead.
        """
        event = {
            "type": "payment_success",
            "provider": "stripe",
            "amount": 5000.00,
            "currency": "USD",
            "metadata": {},
        }
        customer_data = {"email": "billing@acme.com", "customer_id": "cus_123"}

        result = detector.detect(event, customer_data)

        assert result is not None
        assert result.icon == "money"
        assert "First payment" not in result.text

    def test_shopify_first_order_is_still_first_payment(
        self, detector: InsightDetector
    ) -> None:
        """Test a Shopify first order (orders_count=1) still fires first payment.

        Shopify does populate orders_count, so the history is known and a
        genuine first order must still be celebrated.
        """
        event = {
            "type": "payment_success",
            "provider": "shopify",
            "amount": 29.99,
            "currency": "USD",
            "metadata": {},
        }
        customer_data = {
            "email": "new@shop.example",
            "orders_count": 1,
            "total_spent": "29.99",
        }

        result = detector._detect_first_payment(event, customer_data)

        assert result is not None
        assert result.icon == "new"
        assert "First payment" in result.text


class TestTrialInsightCurrencyAndInterval:
    """The trial insight honors the event currency and billing interval."""

    def test_trial_renders_non_usd_currency(self, detector: InsightDetector) -> None:
        """Test a EUR trial renders the euro symbol, not a dollar sign."""
        event = {
            "type": "trial_started",
            "currency": "EUR",
            "metadata": {
                "trial_days": 14,
                "plan_amount": 29.00,
                "billing_period": "monthly",
            },
        }

        result = detector._detect_trial_started(event, {})

        assert result is not None
        assert result.text == "14-day trial, then €29.00/mo"

    def test_trial_uses_annual_interval(self, detector: InsightDetector) -> None:
        """Test an annual trial renders "/yr" instead of the hardcoded "/mo"."""
        event = {
            "type": "trial_started",
            "currency": "USD",
            "metadata": {
                "trial_days": 30,
                "plan_amount": 500.00,
                "billing_period": "annual",
            },
        }

        result = detector._detect_trial_started(event, {})

        assert result is not None
        assert result.text == "30-day trial, then $500.00/yr"

    def test_trial_without_interval_defaults_to_monthly(
        self, detector: InsightDetector
    ) -> None:
        """Test a trial with no billing_period preserves the "/mo" default."""
        event = {
            "type": "trial_started",
            "currency": "GBP",
            "metadata": {"trial_days": 7, "plan_amount": 10.00},
        }

        result = detector._detect_trial_started(event, {})

        assert result is not None
        assert result.text == "7-day trial, then £10.00/mo"


class TestLtvMilestoneNoDoubleCount:
    """The LTV milestone is not claimed one payment early."""

    def test_milestone_not_claimed_early(self, detector: InsightDetector) -> None:
        """Test a customer sitting below a milestone does not celebrate it.

        total_spent already includes the current order (800 prior + 100
        now = 900), so at $900 lifetime the $1,000 milestone is not reached.
        The old code added the current amount again (900 + 100 = 1,000) and
        celebrated a payment early.
        """
        event = {"type": "payment_success", "amount": 100.00}
        customer_data = {
            "orders_count": 9,
            "total_spent": "900.00",
            "payment_history": [{"status": "success", "amount": 100}] * 3,
        }

        result = detector.detect(event, customer_data)

        assert result is None

    def test_milestone_fires_when_actually_crossed(
        self, detector: InsightDetector
    ) -> None:
        """Test the milestone fires when total_spent actually reaches it.

        total_spent already includes the current order (900 prior + 100 now
        = 1,000), so the $1,000 milestone is genuinely crossed.
        """
        event = {"type": "payment_success", "amount": 100.00}
        customer_data = {
            "orders_count": 10,
            "total_spent": "1000.00",
            "payment_history": [{"status": "success", "amount": 100}] * 3,
        }

        result = detector.detect(event, customer_data)

        assert result is not None
        assert result.icon == "celebration"
        assert "1,000" in result.text

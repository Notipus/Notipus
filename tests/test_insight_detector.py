"""Tests for the InsightDetector service.

This module tests the InsightDetector class that identifies
milestones and generates insights for notifications.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from webhooks.services.insight_detector import InsightDetector, MilestoneConfig


def _years_ago(years: int, offset_days: int = 0) -> str:
    """Build an ISO timestamp exactly N calendar years before now.

    Args:
        years: Number of years to go back.
        offset_days: Extra days to shift the result by (can be negative).

    Returns:
        ISO-formatted UTC timestamp string.
    """
    now = datetime.now(timezone.utc)
    day = now.day
    if now.month == 2 and now.day == 29:
        day = 28  # Leap day has no exact match in non-leap years
    created = now.replace(year=now.year - years, day=day)
    return (created + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def mock_insight_cache():
    """Stateful cache mock for anniversary dedup (cache.add semantics).

    Yields:
        Mock cache whose add() returns True only for previously-unseen keys.
    """
    store: dict = {}

    def mock_add(key: str, value, timeout=None) -> bool:
        if key in store:
            return False
        store[key] = value
        return True

    with patch("webhooks.services.insight_detector.cache") as mock:
        mock.add = mock_add
        yield mock


@pytest.fixture
def detector() -> InsightDetector:
    """Create an InsightDetector instance with default config."""
    return InsightDetector()


@pytest.fixture
def custom_detector() -> InsightDetector:
    """Create an InsightDetector with custom config."""
    config = MilestoneConfig(
        ltv_milestones=[500, 1000, 2500],
        vip_ltv_threshold=5000,
    )
    return InsightDetector(config)


@pytest.fixture
def payment_success_event() -> dict:
    """Sample payment success event."""
    return {
        "type": "payment_success",
        "provider": "stripe",
        "amount": 299.00,
        "currency": "USD",
        "metadata": {},
    }


@pytest.fixture
def payment_failure_event() -> dict:
    """Sample payment failure event."""
    return {
        "type": "payment_failure",
        "provider": "stripe",
        "amount": 99.00,
        "currency": "USD",
        "metadata": {"failure_reason": "Card declined"},
    }


@pytest.fixture
def new_customer_data() -> dict:
    """Sample new customer data (first payment)."""
    return {
        "email": "new@example.com",
        "orders_count": 0,
        "total_spent": 0,
        "payment_history": [],
    }


@pytest.fixture
def existing_customer_data() -> dict:
    """Sample existing customer data."""
    return {
        "email": "existing@example.com",
        "orders_count": 10,
        "total_spent": 4500.00,
        "payment_history": [
            {"status": "success", "amount": 500},
            {"status": "success", "amount": 500},
            {"status": "success", "amount": 500},
        ],
        "created_at": "2024-01-15T10:00:00Z",
    }


class TestInsightDetectorBasic:
    """Test basic InsightDetector functionality."""

    def test_detect_returns_insight_info_or_none(
        self,
        detector: InsightDetector,
        payment_success_event: dict,
        existing_customer_data: dict,
    ) -> None:
        """Test detect returns InsightInfo or None."""
        result = detector.detect(payment_success_event, existing_customer_data)

        # Result should be InsightInfo or None
        assert result is None or hasattr(result, "icon")

    def test_default_config(self, detector: InsightDetector) -> None:
        """Test default milestone configuration."""
        assert 1000 in detector.config.ltv_milestones
        assert 5000 in detector.config.ltv_milestones
        assert detector.config.vip_ltv_threshold == 10000


class TestFirstPaymentDetection:
    """Test first payment detection."""

    def test_detect_first_payment_new_customer(
        self,
        detector: InsightDetector,
        payment_success_event: dict,
        new_customer_data: dict,
    ) -> None:
        """Test first payment detection for new customer."""
        result = detector.detect(payment_success_event, new_customer_data)

        assert result is not None
        assert result.icon == "new"
        assert "First payment" in result.text or "Welcome" in result.text

    def test_detect_first_payment_subscription_created(
        self, detector: InsightDetector, new_customer_data: dict
    ) -> None:
        """Test first payment detection on subscription created."""
        event = {"type": "subscription_created", "amount": 49.00}
        result = detector.detect(event, new_customer_data)

        assert result is not None
        assert "First payment" in result.text or "Welcome" in result.text

    def test_no_first_payment_for_existing_customer(
        self,
        detector: InsightDetector,
        payment_success_event: dict,
        existing_customer_data: dict,
    ) -> None:
        """Test no first payment insight for existing customer."""
        result = detector.detect(payment_success_event, existing_customer_data)

        # Should not be first payment insight
        if result is not None:
            assert "First payment" not in result.text

    def test_no_first_payment_without_orders_count(
        self, detector: InsightDetector, payment_success_event: dict
    ) -> None:
        """Test that missing history data never reads as a first payment.

        Stripe/Chargify webhook payloads carry no order count. Absence of
        history is not evidence of a first payment - defaulting to 0 would
        label every renewal "First payment from this customer".
        """
        customer = {"email": "renewal@example.com", "customer_id": "cus_123"}

        result = detector.detect(payment_success_event, customer)

        assert result is None or "First payment" not in result.text


class TestLTVMilestoneDetection:
    """Test LTV milestone detection."""

    def test_detect_ltv_milestone_1000(self, detector: InsightDetector) -> None:
        """Test detection of $1000 LTV milestone."""
        event = {"type": "payment_success", "amount": 200.00}
        customer = {
            "orders_count": 5,
            "total_spent": 900.00,  # Will cross $1000 with this payment
            "payment_history": [{"status": "success", "amount": 300}] * 3,
        }

        result = detector.detect(event, customer)

        assert result is not None
        assert "1,000" in result.text
        assert result.icon == "celebration"

    def test_detect_ltv_milestone_5000(self, detector: InsightDetector) -> None:
        """Test detection of $5000 LTV milestone."""
        event = {"type": "payment_success", "amount": 500.00}
        customer = {
            "orders_count": 20,
            "total_spent": 4800.00,  # Will cross $5000 with this payment
            "payment_history": [{"status": "success", "amount": 300}] * 5,
        }

        result = detector.detect(event, customer)

        assert result is not None
        assert "5,000" in result.text
        assert result.icon == "celebration"

    def test_no_milestone_when_not_crossed(self, detector: InsightDetector) -> None:
        """Test no milestone when not crossed."""
        event = {"type": "payment_success", "amount": 100.00}
        customer = {
            "orders_count": 5,
            "total_spent": 500.00,  # Won't cross any milestone
            "payment_history": [{"status": "success", "amount": 100}] * 5,
        }

        result = detector.detect(event, customer)

        # Should not be LTV milestone insight (may be another type or None)
        if result is not None:
            assert "Crossed" not in result.text or "$1,000" not in result.text

    def test_custom_ltv_milestones(self, custom_detector: InsightDetector) -> None:
        """Test custom LTV milestones."""
        event = {"type": "payment_success", "amount": 100.00}
        customer = {
            "orders_count": 3,
            "total_spent": 450.00,  # Will cross $500 with this payment
            "payment_history": [{"status": "success", "amount": 150}] * 3,
        }

        result = custom_detector.detect(event, customer)

        assert result is not None
        assert "500" in result.text

    def test_no_milestone_without_ltv_data(self, detector: InsightDetector) -> None:
        """Test that unknown LTV never fires a milestone.

        Stripe payloads carry no lifetime spend; treating unknown as $0
        would make a single $5k invoice from a years-old customer claim
        "Crossed $5,000 lifetime!".
        """
        event = {"type": "payment_success", "amount": 5000.00, "orders_count": 9}
        customer = {"email": "big@example.com", "customer_id": "cus_123"}

        result = detector._detect_ltv_milestone(event, customer)

        assert result is None

    def test_milestone_uses_event_currency(self, detector: InsightDetector) -> None:
        """Test that milestone text uses the payment's currency, not $."""
        event = {"type": "payment_success", "amount": 200.00, "currency": "EUR"}
        customer = {"orders_count": 5, "total_spent": 900.00}

        result = detector._detect_ltv_milestone(event, customer)

        assert result is not None
        assert "€1,000" in result.text


class TestLargestLTVMilestone:
    """Test that the LARGEST crossed LTV milestone is celebrated."""

    def test_big_jump_fires_largest_milestone(self, detector: InsightDetector) -> None:
        """Test $500 -> $60,500 fires the $50,000 milestone, not $1,000."""
        event = {"type": "payment_success", "amount": 60000.00}
        customer = {
            "orders_count": 10,
            "total_spent": 500.00,
            "payment_history": [{"status": "success", "amount": 250}] * 2,
        }

        result = detector.detect(event, customer)

        assert result is not None
        assert "50,000" in result.text
        assert "1,000" not in result.text.replace("50,000", "")

    def test_crossing_single_milestone_unchanged(
        self, detector: InsightDetector
    ) -> None:
        """Test that crossing exactly one milestone still fires it."""
        event = {"type": "payment_success", "amount": 200.00}
        customer = {
            "orders_count": 10,
            "total_spent": 4900.00,  # Crosses only $5,000
            "payment_history": [{"status": "success", "amount": 200}] * 2,
        }

        result = detector.detect(event, customer)

        assert result is not None
        assert "5,000" in result.text


class TestAnniversaryDetection:
    """Test true-date anniversary detection with dedup."""

    @pytest.fixture
    def anniversary_customer(self) -> dict:
        """Customer created exactly one year ago with quiet history.

        History is shaped so no higher-priority insight (first payment, LTV
        milestone, growth) fires before the anniversary check.
        """
        return {
            "email": "loyal@example.com",
            "orders_count": 12,
            "total_spent": 300.00,
            "payment_history": [{"status": "success", "amount": 25}] * 2,
            "created_at": _years_ago(1),
        }

    @pytest.fixture
    def anniversary_event(self) -> dict:
        """Payment event for the anniversary customer."""
        return {
            "type": "payment_success",
            "customer_id": "cus_anniv",
            "workspace_id": "ws_123",
            "amount": 25.00,
            "metadata": {},
        }

    def test_fires_on_true_anniversary_date(
        self,
        detector: InsightDetector,
        anniversary_event: dict,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test that the 1-year anniversary fires on the true date."""
        result = detector.detect(anniversary_event, anniversary_customer)

        assert result is not None
        assert "1 year anniversary" in result.text
        assert result.icon == "celebration"

    def test_fires_within_tolerance_window(
        self,
        detector: InsightDetector,
        anniversary_event: dict,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test that a payment 2 days after the true date still fires."""
        anniversary_customer["created_at"] = _years_ago(1, offset_days=-2)

        result = detector.detect(anniversary_event, anniversary_customer)

        assert result is not None
        assert "1 year anniversary" in result.text

    def test_does_not_fire_outside_tolerance_window(
        self,
        detector: InsightDetector,
        anniversary_event: dict,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test that a payment 10 days off the true date does not fire.

        The old `days // 30` heuristic matched a ~30-day span (any payment
        between day 360 and day 389), so a weekly payer got several
        "1 year anniversary!" insights in a row.
        """
        anniversary_customer["created_at"] = _years_ago(1, offset_days=-10)

        result = detector.detect(anniversary_event, anniversary_customer)

        assert result is None

    def test_fires_exactly_once_per_anniversary(
        self,
        detector: InsightDetector,
        anniversary_event: dict,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test dedup: a second payment inside the window does not re-fire."""
        first = detector.detect(anniversary_event, anniversary_customer)
        second = detector.detect(anniversary_event, anniversary_customer)

        assert first is not None
        assert "1 year anniversary" in first.text
        assert second is None

    def test_different_customers_dedup_independently(
        self,
        detector: InsightDetector,
        anniversary_event: dict,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test that one customer's celebration doesn't block another's."""
        first = detector.detect(anniversary_event, anniversary_customer)

        other_event = dict(anniversary_event, customer_id="cus_other")
        second = detector.detect(other_event, anniversary_customer)

        assert first is not None
        assert second is not None

    def test_two_year_anniversary(
        self,
        detector: InsightDetector,
        anniversary_event: dict,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test the 2-year anniversary text."""
        anniversary_customer["created_at"] = _years_ago(2)

        result = detector.detect(anniversary_event, anniversary_customer)

        assert result is not None
        assert "2 year anniversary" in result.text

    def test_no_anniversary_without_customer_id(
        self,
        detector: InsightDetector,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test that anniversary needs a customer id (for attribution/dedup)."""
        event = {"type": "payment_success", "amount": 25.00, "metadata": {}}

        result = detector.detect(event, anniversary_customer)

        assert result is None

    def test_no_anniversary_without_workspace_id(
        self,
        detector: InsightDetector,
        anniversary_customer: dict,
        mock_insight_cache,
    ) -> None:
        """Test that anniversary is skipped when the workspace is unknown.

        The dedup key is tenant-scoped; customer ids are only unique per
        provider account, so an unscoped claim could collide across
        tenants. Skipping is the only behavior that cannot collide.
        """
        event = {
            "type": "payment_success",
            "customer_id": "cus_anniv",
            "amount": 25.00,
            "metadata": {},
        }

        result = detector.detect(event, anniversary_customer)

        assert result is None

    def test_add_months_clamps_short_months(self, detector: InsightDetector) -> None:
        """Test that month arithmetic clamps Jan 31 to end of February."""
        jan_31 = datetime(2025, 1, 31, tzinfo=timezone.utc)

        result = detector._add_months(jan_31, 13)

        assert result == datetime(2026, 2, 28, tzinfo=timezone.utc)

    def test_add_months_handles_leap_year(self, detector: InsightDetector) -> None:
        """Test that a Jan 31 anniversary lands on Feb 29 in leap years."""
        jan_31 = datetime(2027, 1, 31, tzinfo=timezone.utc)

        result = detector._add_months(jan_31, 13)

        assert result == datetime(2028, 2, 29, tzinfo=timezone.utc)


class TestTrialConvertedDetection:
    """Test stateless trial conversion detection from invoice metadata."""

    def test_detects_trial_conversion_from_metadata(
        self, detector: InsightDetector
    ) -> None:
        """Test that is_trial_conversion metadata yields the insight.

        The Stripe parser flags the first paid invoice after a trial
        (period_start at trial_end) - no cache marker involved, so the
        3-day gap between trial_will_end and the conversion is irrelevant.
        """
        event = {
            "type": "payment_success",
            "customer_id": "cus_123",
            "amount": 29.00,
            "metadata": {"is_trial_conversion": True},
        }

        result = detector.detect(event, {"payment_history": []})

        assert result is not None
        assert "Trial converted" in result.text
        assert result.icon == "celebration"

    def test_no_trial_conversion_without_metadata(
        self, detector: InsightDetector
    ) -> None:
        """Test that an ordinary payment does not claim a trial conversion."""
        event = {
            "type": "payment_success",
            "customer_id": "cus_123",
            "amount": 29.00,
            "metadata": {},
        }

        result = detector._detect_trial_converted(event, {})

        assert result is None

    def test_trial_conversion_on_invoice_paid(self, detector: InsightDetector) -> None:
        """Test that invoice_paid events can also carry the conversion flag."""
        event = {
            "type": "invoice_paid",
            "customer_id": "cus_123",
            "amount": 29.00,
            "metadata": {"is_trial_conversion": True},
        }

        result = detector._detect_trial_converted(event, {})

        assert result is not None
        assert "Trial converted" in result.text

    def test_none_metadata_does_not_crash(self, detector: InsightDetector) -> None:
        """Test that an explicit metadata=None is handled gracefully."""
        event = {
            "type": "payment_success",
            "customer_id": "cus_123",
            "amount": 29.00,
            "metadata": None,
        }

        result = detector._detect_trial_converted(event, {})

        assert result is None


class TestInitialPaymentFailureDetection:
    """Test surfacing of a payment_failure folded into an aggregated event."""

    def test_subscription_with_failed_payment_surfaces_failure(
        self, detector: InsightDetector
    ) -> None:
        """Test that has_payment_failure metadata produces a warning insight.

        This is the aggregation case: subscription.created +
        invoice.payment_failed in the same idempotency bucket must not read
        as a plain "New subscription!".
        """
        event = {
            "type": "subscription_created",
            "customer_id": "cus_123",
            "amount": 49.00,
            "metadata": {
                "has_payment_failure": True,
                "failure_reason": "Your card was declined",
                "decline_code": "insufficient_funds",
                "attempt_count": 1,
            },
        }

        result = detector.detect(event, {"orders_count": 0, "payment_history": []})

        assert result is not None
        assert result.icon == "warning"
        assert "Your card was declined" in result.text

    def test_failure_insight_outranks_first_payment(
        self, detector: InsightDetector, new_customer_data: dict
    ) -> None:
        """Test that the failure warning wins over 'First payment'."""
        event = {
            "type": "subscription_created",
            "amount": 49.00,
            "metadata": {"has_payment_failure": True, "failure_reason": "declined"},
        }

        result = detector.detect(event, new_customer_data)

        assert result is not None
        assert result.icon == "warning"

    def test_failure_insight_includes_retry_info(
        self, detector: InsightDetector
    ) -> None:
        """Test that retry count and next attempt date are included."""
        event = {
            "type": "subscription_created",
            "metadata": {
                "has_payment_failure": True,
                "failure_reason": "Card declined",
                "attempt_count": 2,
                "next_payment_attempt": 1740182400,  # Feb 22 2025
            },
        }

        result = detector._detect_initial_payment_failure(event, {})

        assert result is not None
        assert "attempt #2" in result.text
        assert "Next retry" in result.text

    def test_no_failure_insight_without_flag(self, detector: InsightDetector) -> None:
        """Test that plain subscription_created events are unaffected."""
        event = {"type": "subscription_created", "metadata": {}}

        result = detector._detect_initial_payment_failure(event, {})

        assert result is None

    def test_none_metadata_does_not_crash(self, detector: InsightDetector) -> None:
        """Test that an explicit metadata=None is handled gracefully."""
        event = {"type": "subscription_created", "metadata": None}

        result = detector._detect_initial_payment_failure(event, {})

        assert result is None

    def test_retry_date_from_string_timestamp(self, detector: InsightDetector) -> None:
        """Test that a numeric-string timestamp is still formatted."""
        event = {
            "type": "subscription_created",
            "metadata": {
                "has_payment_failure": True,
                "next_payment_attempt": "1740182400",  # Feb 22 2025, as string
            },
        }

        result = detector._detect_initial_payment_failure(event, {})

        assert result is not None
        assert "Next retry Feb 22" in result.text

    def test_invalid_retry_timestamp_ignored(self, detector: InsightDetector) -> None:
        """Test that an unparseable timestamp drops the retry date, not the insight."""
        event = {
            "type": "subscription_created",
            "metadata": {
                "has_payment_failure": True,
                "failure_reason": "Card declined",
                "next_payment_attempt": "soon",
            },
        }

        result = detector._detect_initial_payment_failure(event, {})

        assert result is not None
        assert "Card declined" in result.text
        assert "Next retry" not in result.text


class TestFailedAttemptDetection:
    """Test failed payment attempt detection."""

    def test_detect_failure_reason(
        self, detector: InsightDetector, payment_failure_event: dict
    ) -> None:
        """Test detection of failure reason."""
        customer = {"payment_history": []}

        result = detector.detect(payment_failure_event, customer)

        assert result is not None
        assert "declined" in result.text.lower()
        assert result.icon == "warning"


class TestVIPDetection:
    """Test VIP customer detection."""

    def test_detect_vip_status(self, detector: InsightDetector) -> None:
        """Test VIP status detection for high LTV customers."""
        event = {"type": "payment_success", "amount": 500.00}
        customer = {
            "orders_count": 50,
            "total_spent": 15000.00,  # High LTV
            "payment_history": [{"status": "success", "amount": 300}] * 5,
        }

        result = detector.detect(event, customer)

        # VIP detection might not be highest priority, check if detected
        # when no higher priority milestones are crossed
        assert result is not None

    def test_vip_text_reflects_threshold_and_currency(
        self, custom_detector: InsightDetector
    ) -> None:
        """Test VIP text renders the configured threshold in event currency."""
        event = {"type": "payment_success", "amount": 100.00, "currency": "EUR"}
        customer = {"total_spent": 6000.00}

        result = custom_detector._detect_vip_status(event, customer)

        assert result is not None
        assert "€5,000+" in result.text

    def test_no_vip_without_ltv_data(self, detector: InsightDetector) -> None:
        """Test that unknown LTV never claims VIP status."""
        event = {"type": "payment_success", "amount": 100.00}
        customer = {"email": "unknown@example.com"}

        result = detector._detect_vip_status(event, customer)

        assert result is None


class TestRiskStatusDetection:
    """Test risk status flag detection."""

    def test_detect_at_risk_on_failure_high_ltv(
        self, detector: InsightDetector, payment_failure_event: dict
    ) -> None:
        """Test at_risk flag on failure with high LTV."""
        customer = {
            "total_spent": 5000.00,
            "payment_history": [],
        }

        flags = detector.detect_risk_status(payment_failure_event, customer)

        assert "at_risk" in flags

    def test_detect_vip_flag(
        self, detector: InsightDetector, payment_success_event: dict
    ) -> None:
        """Test VIP flag for high LTV customers."""
        customer = {
            "total_spent": 15000.00,  # Over VIP threshold
            "payment_history": [],
        }

        flags = detector.detect_risk_status(payment_success_event, customer)

        assert "vip" in flags

    def test_no_flags_without_ltv_data(
        self, detector: InsightDetector, payment_failure_event: dict
    ) -> None:
        """Test that unknown LTV yields no flags (never guessed from $0)."""
        customer = {"email": "unknown@example.com", "customer_id": "cus_123"}

        flags = detector.detect_risk_status(payment_failure_event, customer)

        assert flags == []

    def test_no_flags_normal_customer(
        self, detector: InsightDetector, payment_success_event: dict
    ) -> None:
        """Test no flags for normal customer."""
        customer = {
            "total_spent": 500.00,
            "payment_history": [{"status": "success"}, {"status": "success"}],
        }

        flags = detector.detect_risk_status(payment_success_event, customer)

        assert "at_risk" not in flags
        assert "vip" not in flags


class TestLargePaymentDetection:
    """Test large payment detection."""

    def test_detect_large_payment(self, detector: InsightDetector) -> None:
        """Test large payment detection."""
        event = {"type": "payment_success", "amount": 1500.00}  # Large payment
        customer = {
            "orders_count": 5,
            "total_spent": 500.00,  # Low total so no LTV milestone
            "payment_history": [{"status": "success", "amount": 100}]
            * 2,  # Low history
        }

        result = detector.detect(event, customer)

        assert result is not None
        # Should detect as large payment if no other milestone triggered
        assert result.icon in ("money", "chart", "celebration")


class TestMilestoneConfigDefaults:
    """Test MilestoneConfig default values."""

    def test_default_ltv_milestones(self) -> None:
        """Test default LTV milestones."""
        config = MilestoneConfig()
        assert config.ltv_milestones == [1000, 5000, 10000, 50000, 100000]

    def test_default_anniversary_months(self) -> None:
        """Test default anniversary months."""
        config = MilestoneConfig()
        assert config.anniversary_months == [12, 24, 36, 48, 60]

    def test_default_vip_threshold(self) -> None:
        """Test default VIP LTV threshold."""
        config = MilestoneConfig()
        assert config.vip_ltv_threshold == 10000

    def test_default_large_payment_threshold(self) -> None:
        """Test default large payment threshold."""
        config = MilestoneConfig()
        assert config.large_payment_threshold == 1000

    def test_custom_config(self) -> None:
        """Test custom configuration."""
        config = MilestoneConfig(
            ltv_milestones=[100, 500, 1000],
            vip_ltv_threshold=2500,
            large_payment_threshold=500,
        )

        assert config.ltv_milestones == [100, 500, 1000]
        assert config.vip_ltv_threshold == 2500
        assert config.large_payment_threshold == 500


class TestStringLTVHandling:
    """Test handling of string LTV values from providers like Shopify."""

    def test_string_total_spent_in_risk_status(
        self, detector: InsightDetector, payment_success_event: dict
    ) -> None:
        """Test that string total_spent doesn't crash detect_risk_status."""
        # Shopify sends total_spent as string "0.00"
        customer = {
            "total_spent": "0.00",  # String, not float
            "payment_history": [],
        }

        # Should not raise TypeError
        flags = detector.detect_risk_status(payment_success_event, customer)
        assert isinstance(flags, list)
        assert "vip" not in flags  # 0.00 is not VIP

    def test_string_total_spent_vip_detection(
        self, detector: InsightDetector, payment_success_event: dict
    ) -> None:
        """Test VIP detection works with string total_spent."""
        customer = {
            "total_spent": "15000.00",  # String over VIP threshold
            "payment_history": [],
        }

        flags = detector.detect_risk_status(payment_success_event, customer)
        assert "vip" in flags

    def test_string_ltv_milestone_detection(self, detector: InsightDetector) -> None:
        """Test LTV milestone detection with string total_spent."""
        event = {"type": "payment_success", "amount": 200.00}
        customer = {
            "orders_count": 5,
            "total_spent": "900.00",  # String that will cross $1000
            "payment_history": [{"status": "success", "amount": 300}] * 3,
        }

        result = detector.detect(event, customer)

        assert result is not None
        assert "1,000" in result.text

    def test_string_amount_in_event(self, detector: InsightDetector) -> None:
        """Test that string amount in event data is handled."""
        event = {"type": "payment_success", "amount": "200.00"}  # String amount
        customer = {
            "orders_count": 5,
            "total_spent": 900.00,
            "payment_history": [{"status": "success", "amount": 300}] * 3,
        }

        result = detector.detect(event, customer)

        assert result is not None
        assert "1,000" in result.text


class TestInsightPriority:
    """Test insight detection priority."""

    def test_first_payment_highest_priority(
        self, detector: InsightDetector, new_customer_data: dict
    ) -> None:
        """Test first payment has highest priority."""
        # Even with large amount, should show first payment
        event = {"type": "payment_success", "amount": 5000.00}

        result = detector.detect(event, new_customer_data)

        assert result is not None
        assert "First payment" in result.text or "Welcome" in result.text

    def test_ltv_milestone_over_large_payment(self, detector: InsightDetector) -> None:
        """Test LTV milestone takes priority over the large-payment insight."""
        # Payment that both crosses a milestone AND clears the large threshold
        event = {"type": "payment_success", "amount": 1500.00}
        customer = {
            "orders_count": 5,
            "total_spent": 900.00,  # Will cross $1000
        }

        result = detector.detect(event, customer)

        assert result is not None
        assert "Crossed" in result.text


class TestFailedAttemptsWithAttemptCount:
    """Test failed attempt detection using Stripe's attempt_count metadata."""

    def test_attempt_count_2_shows_retry(self, detector: InsightDetector) -> None:
        """Test that attempt_count >= 2 shows 'Retry #N'."""
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {
                "attempt_count": 2,
                "next_payment_attempt": 1740182400,  # Feb 22 2025
            },
        }
        customer: dict = {"payment_history": []}

        result = detector.detect(event, customer)

        assert result is not None
        assert "Retry #2" in result.text
        assert "Next attempt" in result.text
        assert result.icon == "warning"

    def test_attempt_count_3_shows_retry(self, detector: InsightDetector) -> None:
        """Test that attempt_count 3 shows 'Retry #3'."""
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {"attempt_count": 3},
        }
        customer: dict = {"payment_history": []}

        result = detector.detect(event, customer)

        assert result is not None
        assert "Retry #3" in result.text

    def test_attempt_count_1_with_next_retry(self, detector: InsightDetector) -> None:
        """Test attempt_count 1 with next_payment_attempt shows retry date."""
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {
                "attempt_count": 1,
                "next_payment_attempt": 1740182400,
            },
        }
        customer: dict = {"payment_history": []}

        result = detector.detect(event, customer)

        assert result is not None
        assert "Next retry" in result.text

    def test_attempt_count_1_no_next_retry_with_reason(
        self, detector: InsightDetector
    ) -> None:
        """Test attempt_count 1 without next retry falls through to failure_reason."""
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {
                "attempt_count": 1,
                "failure_reason": "Card declined",
            },
        }
        customer: dict = {"payment_history": []}

        result = detector.detect(event, customer)

        assert result is not None
        assert "Card declined" in result.text

    def test_no_attempt_data_yields_no_insight(self, detector: InsightDetector) -> None:
        """Test that a failure with no metadata yields no insight.

        Webhook payloads carry no cross-event history, so with neither
        attempt_count nor failure_reason there is nothing truthful to say.
        """
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {},
        }

        result = detector.detect(event, {})

        assert result is None

    def test_string_next_attempt_timestamp_formatted(
        self, detector: InsightDetector
    ) -> None:
        """Test that a numeric-string next_payment_attempt still formats."""
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {
                "attempt_count": 2,
                "next_payment_attempt": "1740182400",  # Feb 22 2025, as string
            },
        }
        customer: dict = {"payment_history": []}

        result = detector.detect(event, customer)

        assert result is not None
        assert "Retry #2" in result.text
        assert "Next attempt Feb 22" in result.text

    def test_invalid_next_attempt_timestamp_does_not_crash(
        self, detector: InsightDetector
    ) -> None:
        """Test that garbage next_payment_attempt drops the date, not the insight."""
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {
                "attempt_count": 2,
                "next_payment_attempt": "soon",
            },
        }
        customer: dict = {"payment_history": []}

        result = detector.detect(event, customer)

        assert result is not None
        assert "Retry #2" in result.text
        assert "Next attempt" not in result.text

    def test_invalid_timestamp_attempt_1_falls_back_to_reason(
        self, detector: InsightDetector
    ) -> None:
        """Test that attempt 1 with a bad timestamp falls back to the reason."""
        event: dict = {
            "type": "payment_failure",
            "amount": 53.20,
            "metadata": {
                "attempt_count": 1,
                "failure_reason": "Card declined",
                "next_payment_attempt": "not-a-timestamp",
            },
        }
        customer: dict = {"payment_history": []}

        result = detector.detect(event, customer)

        assert result is not None
        assert "Card declined" in result.text

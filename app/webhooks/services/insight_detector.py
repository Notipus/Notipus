"""Insight detector for identifying milestones and generating insights.

This module analyzes payment events and customer data to detect significant
milestones and generate contextual insights for notifications.
"""

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from django.core.cache import cache
from webhooks.models.rich_notification import InsightInfo

# How long the "anniversary fired" dedup marker lives. Must be longer than
# the +/- ANNIVERSARY_TOLERANCE_DAYS detection window so a customer paying
# multiple times inside the window only celebrates once per anniversary.
ANNIVERSARY_DEDUP_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# A payment within this many days of the true anniversary date counts.
ANNIVERSARY_TOLERANCE_DAYS = 3


def _to_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float.

    Handles strings like "0.00" from Shopify and other providers.

    Args:
        value: The value to convert (string, int, float, or None).
        default: Default value if conversion fails.

    Returns:
        The converted float value or default.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@dataclass
class MilestoneConfig:
    """Configuration for milestone detection.

    Attributes:
        ltv_milestones: LTV amounts that trigger celebrations.
        payment_growth_threshold: Percentage increase to highlight.
        vip_ltv_threshold: LTV amount for VIP status.
        anniversary_months: Months that trigger anniversary messages.
        large_payment_threshold: Amount to consider a payment "large".
    """

    ltv_milestones: list[float] = field(
        default_factory=lambda: [1000, 5000, 10000, 50000, 100000]
    )
    payment_growth_threshold: float = 0.20  # 20% growth
    vip_ltv_threshold: float = 10000
    anniversary_months: list[int] = field(default_factory=lambda: [12, 24, 36, 48, 60])
    large_payment_threshold: float = 1000


class InsightDetector:
    """Detects milestones and generates insights from event/customer data.

    This class analyzes payment events and customer history to identify
    significant milestones like first payments, LTV thresholds, and
    anniversaries, generating contextual insights for notifications.
    """

    # Semantic icon names for different insight types
    ICONS = {
        "first_payment": "new",
        "trial_started": "rocket",
        "trial_converted": "celebration",
        "ltv_milestone": "celebration",
        "anniversary": "celebration",
        "payment_growth": "chart",
        "vip_status": "trophy",
        "failed_attempt": "warning",
        "at_risk": "warning",
        "large_payment": "money",
    }

    def __init__(self, config: MilestoneConfig | None = None) -> None:
        """Initialize the insight detector.

        Args:
            config: Optional milestone configuration.
        """
        self.config = config or MilestoneConfig()

    def detect(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect the most significant insight for this event.

        Checks for milestones in priority order and returns the first match.

        Args:
            event_data: Event data dictionary from provider.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo if a milestone is detected, None otherwise.
        """
        # Priority order for milestone detection
        detectors = [
            self._detect_initial_payment_failure,
            self._detect_trial_started,
            self._detect_trial_converted,
            self._detect_first_payment,
            self._detect_ltv_milestone,
            self._detect_anniversary,
            self._detect_payment_growth,
            self._detect_vip_status,
            self._detect_failed_attempts,
            self._detect_large_payment,
        ]

        for detector in detectors:
            insight = detector(event_data, customer_data)
            if insight:
                return insight

        return None

    def detect_risk_status(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> list[str]:
        """Detect risk status flags for the customer.

        Args:
            event_data: Event data dictionary from provider.
            customer_data: Customer data dictionary.

        Returns:
            List of status flags (e.g., ["at_risk", "vip"]).
        """
        flags: list[str] = []

        # Check for VIP status
        ltv = _to_float(customer_data.get("total_spent")) or _to_float(
            customer_data.get("lifetime_value")
        )
        if ltv >= self.config.vip_ltv_threshold:
            flags.append("vip")

        # Check for at-risk status (high LTV + recent failures)
        event_type = event_data.get("type", "")
        if event_type == "payment_failure" and ltv >= 1000:
            flags.append("at_risk")

        # Check for multiple recent failures
        payment_history = customer_data.get("payment_history", [])
        recent_failures = sum(
            1
            for p in payment_history[-5:]
            if p.get("status") == "failed" or p.get("type") == "payment_failure"
        )
        if recent_failures >= 2:
            flags.append("at_risk")

        return flags

    def _detect_first_payment(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect if this is the customer's first payment.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo for first payment or None.
        """
        event_type = event_data.get("type", "")
        # Note: trial_started is excluded - no payment has occurred yet
        if event_type not in ("payment_success", "subscription_created"):
            return None

        # Don't show "first payment" for trials - they haven't paid yet
        metadata = event_data.get("metadata", {})
        if metadata.get("is_trial"):
            return None

        # Check order count or payment history
        orders_count = customer_data.get("orders_count", 0)
        payment_count = len(customer_data.get("payment_history", []))

        # First payment if count is 0 or 1 (including current)
        if orders_count <= 1 and payment_count <= 1:
            return InsightInfo(
                icon=self.ICONS["first_payment"],
                text="First payment from this customer",
            )

        return None

    def _detect_trial_started(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect if this is a new trial starting.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary (unused but required for interface).

        Returns:
            InsightInfo for trial started or None.
        """
        _ = customer_data  # unused
        event_type = event_data.get("type", "")
        if event_type != "trial_started":
            return None

        metadata = event_data.get("metadata", {})
        trial_days = metadata.get("trial_days")
        plan_amount = metadata.get("plan_amount")

        if trial_days and plan_amount:
            return InsightInfo(
                icon=self.ICONS["trial_started"],
                text=f"{trial_days}-day trial, then ${plan_amount:,.2f}/mo",
            )
        elif trial_days:
            return InsightInfo(
                icon=self.ICONS["trial_started"],
                text=f"{trial_days}-day trial - Welcome aboard!",
            )

        return InsightInfo(
            icon=self.ICONS["trial_started"],
            text="New trial - Welcome aboard!",
        )

    def _detect_initial_payment_failure(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect a payment failure folded into an aggregated event.

        When a subscription is created with an immediately-declining card,
        Stripe queues subscription.created and invoice.payment_failed under
        the same idempotency key. The aggregation keeps the subscription
        event as the notification but preserves the failure details in
        metadata (see PendingEventQueue._aggregate_events). Surface that
        failure prominently so it is never silently dropped.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary (unused but required for interface).

        Returns:
            InsightInfo describing the failed payment or None.
        """
        _ = customer_data  # unused
        metadata = event_data.get("metadata") or {}
        if not metadata.get("has_payment_failure"):
            return None

        text = "Payment failed"
        failure_reason = metadata.get("failure_reason")
        decline_code = metadata.get("decline_code")
        if failure_reason:
            text += f": {failure_reason}"
        elif decline_code:
            text += f": {decline_code}"

        attempt_count = metadata.get("attempt_count")
        if attempt_count and attempt_count >= 2:
            text += f" (attempt #{attempt_count})"

        next_retry = self._format_short_date(metadata.get("next_payment_attempt"))
        if next_retry:
            text += f" · Next retry {next_retry}"

        return InsightInfo(icon=self.ICONS["failed_attempt"], text=text)

    def _format_short_date(self, timestamp: Any) -> str | None:
        """Format a unix timestamp as a short human date (e.g. "Feb 22").

        Single formatting path for all insight dates. Portable (no
        glibc-only ``%-d`` strftime code) and tolerant of non-integer
        timestamp values from provider payloads.

        Args:
            timestamp: Unix timestamp (int, float, or numeric string).

        Returns:
            Short date string like "Feb 22", or None if unparseable.
        """
        if timestamp is None:
            return None
        try:
            dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            return None
        return f"{dt.strftime('%b')} {dt.day}"

    def _detect_trial_converted(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect if this payment converted a trial.

        Stateless: the Stripe parser flags the first paid invoice after a
        trial (invoice period_start at the subscription's trial_end) with
        ``is_trial_conversion`` metadata, so no cross-event cache marker is
        needed even though trial_will_end fires ~3 days earlier.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary (unused but required for interface).

        Returns:
            InsightInfo for trial converted or None.
        """
        _ = customer_data  # unused
        event_type = event_data.get("type", "")
        if event_type not in ("payment_success", "invoice_paid"):
            return None

        metadata = event_data.get("metadata") or {}
        if metadata.get("is_trial_conversion"):
            return InsightInfo(
                icon=self.ICONS["trial_converted"],
                text="Trial converted to paid subscription",
            )

        return None

    def _detect_ltv_milestone(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect if this payment crosses an LTV milestone.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo for LTV milestone or None.
        """
        event_type = event_data.get("type", "")
        if event_type != "payment_success":
            return None

        current_amount = _to_float(event_data.get("amount"))
        previous_ltv = _to_float(customer_data.get("total_spent")) or _to_float(
            customer_data.get("lifetime_value")
        )
        new_ltv = previous_ltv + current_amount

        # Celebrate the LARGEST milestone crossed by this payment (a big
        # payment can cross several at once - one Slack message per event).
        crossed = [
            milestone
            for milestone in self.config.ltv_milestones
            if previous_ltv < milestone <= new_ltv
        ]
        if crossed:
            return InsightInfo(
                icon=self.ICONS["ltv_milestone"],
                text=f"Crossed ${max(crossed):,.0f} lifetime!",
            )

        return None

    def _parse_date(self, date_value: Any) -> datetime | None:
        """Parse a date value to datetime.

        Args:
            date_value: String or datetime value.

        Returns:
            Parsed datetime or None if parsing fails.
        """
        try:
            if isinstance(date_value, str):
                # Handle ISO format with timezone
                date_value = date_value.replace("Z", "+00:00")
                return datetime.fromisoformat(date_value)
            elif isinstance(date_value, datetime):
                return date_value
        except (ValueError, TypeError):
            pass
        return None

    def _add_months(self, date: datetime, months: int) -> datetime:
        """Return the date shifted forward by a number of calendar months.

        Clamps the day when the target month is shorter (e.g. a customer
        created Jan 31 has a Feb 28/29 monthly anniversary).

        Args:
            date: Base datetime.
            months: Number of months to add.

        Returns:
            Shifted datetime.
        """
        total_months = date.month - 1 + months
        year = date.year + total_months // 12
        month = total_months % 12 + 1
        # Clamp day to the last day of the target month
        days_in_month = calendar.monthrange(year, month)[1]
        return date.replace(year=year, month=month, day=min(date.day, days_in_month))

    def _detect_anniversary(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect customer anniversary milestones.

        Computes each configured anniversary's TRUE calendar date from the
        customer's creation date and fires only when the payment lands
        within +/- ANNIVERSARY_TOLERANCE_DAYS of it. A cache-backed dedup
        marker ensures a customer paying multiple times inside the window
        (e.g. weekly) celebrates each anniversary exactly once.

        Requires both customer_id and workspace_id: the dedup key is
        tenant-scoped, and customer ids are only unique per provider
        account, so claiming without a workspace could let one tenant's
        celebration swallow another tenant's. Skipping the insight when
        the workspace is unknown is the only behavior that cannot collide
        across tenants.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo for anniversary or None.
        """
        if event_data.get("type", "") != "payment_success":
            return None

        customer_id = event_data.get("customer_id", "")
        workspace_id = event_data.get("workspace_id", "")
        if not customer_id or not workspace_id:
            # Cannot attribute (or tenant-scope the dedup of) an anniversary
            return None

        created_at = customer_data.get("created_at") or customer_data.get(
            "subscription_start"
        )
        created_date = self._parse_date(created_at)
        if not created_date:
            return None

        if created_date.tzinfo is not None:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.now()

        for anniversary_month in self.config.anniversary_months:
            anniversary_date = self._add_months(created_date, anniversary_month)
            days_off = (now.date() - anniversary_date.date()).days
            if abs(days_off) > ANNIVERSARY_TOLERANCE_DAYS:
                continue

            if not self._claim_anniversary(
                workspace_id, customer_id, anniversary_month
            ):
                return None  # Already celebrated this anniversary

            years = anniversary_month // 12
            if years == 1:
                text = "1 year anniversary!"
            else:
                text = f"{years} year anniversary!"
            return InsightInfo(icon=self.ICONS["anniversary"], text=text)

        return None

    def _claim_anniversary(
        self, workspace_id: str, customer_id: str, anniversary_month: int
    ) -> bool:
        """Atomically claim an anniversary celebration for a customer.

        Uses cache.add (SETNX on Redis) so concurrent payments inside the
        detection window cannot both celebrate the same anniversary.

        Args:
            workspace_id: The workspace UUID (never empty; enforced by caller
                so dedup keys are always tenant-scoped).
            customer_id: The customer identifier.
            anniversary_month: The anniversary being claimed, in months.

        Returns:
            True if this event won the claim and should celebrate.
        """
        dedup_key = f"anniversary_sent:{workspace_id}:{customer_id}:{anniversary_month}"
        return bool(cache.add(dedup_key, True, timeout=ANNIVERSARY_DEDUP_TTL_SECONDS))

    def _detect_payment_growth(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect significant payment growth vs average.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo for payment growth or None.
        """
        event_type = event_data.get("type", "")
        if event_type != "payment_success":
            return None

        current_amount = _to_float(event_data.get("amount"))
        if current_amount <= 0:
            return None

        # Calculate average payment from history
        payment_history = customer_data.get("payment_history", [])
        successful_payments = [
            _to_float(p.get("amount"))
            for p in payment_history
            if p.get("status") == "success" and _to_float(p.get("amount")) > 0
        ]

        if len(successful_payments) < 3:  # Need enough history
            return None

        avg_payment = sum(successful_payments) / len(successful_payments)
        if avg_payment <= 0:
            return None

        growth_pct = (current_amount - avg_payment) / avg_payment

        if growth_pct >= self.config.payment_growth_threshold:
            return InsightInfo(
                icon=self.ICONS["payment_growth"],
                text=f"+{growth_pct:.0%} larger than average",
            )

        return None

    def _detect_vip_status(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect VIP customer status.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo for VIP status or None.
        """
        event_type = event_data.get("type", "")
        if event_type != "payment_success":
            return None

        ltv = _to_float(customer_data.get("total_spent")) or _to_float(
            customer_data.get("lifetime_value")
        )

        if ltv >= self.config.vip_ltv_threshold:
            return InsightInfo(
                icon=self.ICONS["vip_status"],
                text="VIP customer ($10k+ LTV)",
            )

        return None

    def _detect_failed_attempts(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect multiple failed payment attempts.

        Uses Stripe's attempt_count from metadata when available (covers most
        production failures). Falls back to payment_history for non-Stripe
        providers.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo for failed attempts or None.
        """
        event_type = event_data.get("type", "")
        if event_type != "payment_failure":
            return None

        metadata = event_data.get("metadata", {})
        failure_reason = metadata.get("failure_reason", "")

        # Use Stripe's attempt_count from metadata (most reliable for Stripe)
        attempt_count = metadata.get("attempt_count")
        if attempt_count is not None and attempt_count >= 1:
            next_date = self._format_short_date(metadata.get("next_payment_attempt"))
            if attempt_count >= 2:
                text = f"Retry #{attempt_count}"
                if next_date:
                    text += f" · Next attempt {next_date}"
                return InsightInfo(icon=self.ICONS["failed_attempt"], text=text)
            # First attempt with next retry scheduled
            if next_date:
                return InsightInfo(
                    icon=self.ICONS["failed_attempt"],
                    text=f"Next retry {next_date}",
                )

        # Fallback: count recent failures from payment_history (non-Stripe)
        payment_history = customer_data.get("payment_history", [])
        recent_failures = sum(
            1
            for p in payment_history[-5:]
            if p.get("status") == "failed" or p.get("type") == "payment_failure"
        )

        if recent_failures >= 2:
            text = f"Attempt #{recent_failures + 1}"
            if failure_reason:
                text += f" - {failure_reason}"
            return InsightInfo(icon=self.ICONS["failed_attempt"], text=text)

        if failure_reason:
            return InsightInfo(
                icon=self.ICONS["failed_attempt"],
                text=failure_reason,
            )

        return None

    def _detect_large_payment(
        self, event_data: dict[str, Any], customer_data: dict[str, Any]
    ) -> InsightInfo | None:
        """Detect unusually large payments.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.

        Returns:
            InsightInfo for large payment or None.
        """
        event_type = event_data.get("type", "")
        if event_type != "payment_success":
            return None

        amount = _to_float(event_data.get("amount"))

        # Use configurable threshold for large payment detection
        if amount >= self.config.large_payment_threshold:
            return InsightInfo(
                icon=self.ICONS["large_payment"],
                text="Large payment received",
            )

        return None

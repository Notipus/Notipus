"""Tests for the weekly / monthly sales snapshot computation."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from core.models import Company, SalesSnapshot, TransactionEvent, Workspace
from core.services.sales_stats import (
    INDUSTRY_CONSUMER,
    INDUSTRY_UNKNOWN,
    compare,
    compute_snapshot,
    current_period,
    format_money_map,
    period_containing,
    store_snapshot,
    stored_or_computed,
)

UTC_TZ = ZoneInfo("UTC")
# Wednesday in ISO week 46 of 2026.
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
WEEK = period_containing("week", date(2026, 11, 18), UTC_TZ)
_seq = count()


def _event(
    workspace: Workspace,
    kind: str = "sale",
    *,
    amount: str = "100",
    currency: str = "USD",
    segment: str = "existing",
    customer: str = "c1",
    domain: str = "",
    consumer: bool = False,
    charge: str = "",
    at: datetime | None = None,
    attempt: int | None = None,
    next_attempt: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> TransactionEvent:
    """Create a ledger row directly.

    Args:
        workspace: Owning workspace.
        kind: Ledger kind.
        amount: Amount in major units.
        currency: Currency code.
        segment: new / existing / unknown.
        customer: Customer reference.
        domain: Company domain.
        consumer: Whether the customer used a personal mailbox.
        charge: Charge chain reference.
        at: When it happened (default: Monday of WEEK, noon).
        attempt: Attempt count for failures.
        next_attempt: Next scheduled retry.
        details: Encrypted customer details.

    Returns:
        The saved row.
    """
    return TransactionEvent.objects.create(
        workspace=workspace,
        provider="stripe",
        kind=kind,
        segment=segment,
        occurred_at=at or datetime(2026, 11, 16, 12, tzinfo=UTC),
        amount=Decimal(amount),
        currency=currency,
        company_domain=domain,
        is_consumer=consumer,
        customer_ref=customer,
        charge_ref=charge or f"charge-{customer}",
        dedupe_key=f"k{next(_seq)}",
        attempt_count=attempt,
        next_attempt_at=next_attempt,
        details=details or {},
    )


def _company(domain: str, industry: str) -> Company:
    """Create a classified company.

    Args:
        domain: Company domain.
        industry: Its industry.

    Returns:
        The company.
    """
    return Company.objects.create(
        domain=domain, name=domain, brand_info={"industry": industry}
    )


@pytest.fixture
def workspace() -> Workspace:
    """A workspace on UTC.

    Returns:
        The workspace.
    """
    return Workspace.objects.create(name="Stats Co")


class TestPeriods:
    """Week and month boundaries."""

    def test_week_runs_monday_to_sunday(self) -> None:
        """ISO weeks start on Monday."""
        assert WEEK.start == date(2026, 11, 16)
        assert WEEK.end == date(2026, 11, 23)
        assert WEEK.label == "Week 47 · Nov 16 – 22, 2026"
        assert WEEK.previous().start == date(2026, 11, 9)

    def test_month_and_cross_month_week(self) -> None:
        """Months span calendar months; labels handle month boundaries."""
        month = period_containing("month", date(2026, 2, 10), UTC_TZ)
        assert (month.start, month.end) == (date(2026, 2, 1), date(2026, 3, 1))
        assert month.label == "February 2026"
        week = period_containing("week", date(2026, 12, 1), UTC_TZ)
        assert week.label == "Week 49 · Nov 30 – Dec 6, 2026"

    def test_current_period_uses_workspace_timezone(self) -> None:
        """Sunday 23:30 UTC is already Monday in Tokyo."""
        sunday_night = datetime(2026, 11, 22, 23, 30, tzinfo=UTC)
        assert current_period("week", UTC_TZ, sunday_night).start == date(2026, 11, 16)
        tokyo = current_period("week", ZoneInfo("Asia/Tokyo"), sunday_night)
        assert tokyo.start == date(2026, 11, 23)


@pytest.mark.django_db
class TestSnapshot:
    """compute_snapshot totals and breakdowns."""

    def test_industry_breakdown(self, workspace: Workspace) -> None:
        """Customers group by their company's current industry.

        Args:
            workspace: Workspace fixture.
        """
        _company("saas.example", "Software & SaaS")
        _company("shop.example", "E-commerce & Retail")
        _event(workspace, customer="a", domain="saas.example", segment="new")
        _event(workspace, customer="a", domain="saas.example", amount="50")
        _event(workspace, customer="b", domain="saas.example", amount="300")
        _event(workspace, customer="c", domain="shop.example", amount="40")
        _event(workspace, customer="d", consumer=True, amount="10", segment="new")
        _event(workspace, customer="e", amount="5", segment="unknown")

        snap = compute_snapshot(workspace, WEEK, now=NOW)
        rows = {row["industry"]: row for row in snap["industries"]}

        saas = rows["Software & SaaS"]
        assert saas["customers"] == 2
        assert (saas["new_customers"], saas["existing_customers"]) == (1, 1)
        assert saas["revenue"] == {"USD": "450.000"}
        assert saas["avg_per_customer"] == "225.00"
        assert rows[INDUSTRY_CONSUMER]["customers"] == 1
        assert rows[INDUSTRY_UNKNOWN]["unknown_customers"] == 1
        # Named industries first, by revenue; the catch-all buckets last.
        assert [r["industry"] for r in snap["industries"]][:2] == [
            "Software & SaaS",
            "E-commerce & Retail",
        ]

        totals = snap["totals"]
        assert totals["customers"] == 5
        assert totals["new_customers"] == 2
        assert totals["existing_customers"] == 2
        assert totals["unknown_customers"] == 1
        assert totals["revenue"] == {"USD": "505.000"}

    def test_refunds_cancellations_and_bands(self, workspace: Workspace) -> None:
        """Refunds net out of revenue; customers fall into revenue bands.

        Args:
            workspace: Workspace fixture.
        """
        _event(workspace, customer="small", amount="50")
        _event(workspace, customer="mid", amount="500")
        _event(workspace, customer="big", amount="7000")
        _event(workspace, "refund", customer="mid", amount="100")
        _event(workspace, "cancellation", customer="gone", amount="0")
        _event(workspace, "trial_started", customer="t", amount="0", segment="new")

        snap = compute_snapshot(workspace, WEEK, now=NOW)
        totals = snap["totals"]
        assert totals["revenue"] == {"USD": "7450.000"}
        assert totals["gross"] == {"USD": "7550.000"}
        assert totals["refund_count"] == 1
        assert totals["cancellations"] == 1
        assert totals["trials_started"] == 1
        assert snap["industries"][0]["bands"] == [1, 1, 0, 1]

    def test_bands_scale_for_other_currencies(self, workspace: Workspace) -> None:
        """¥50,000 is a mid-band customer, not a $5k+ one.

        Args:
            workspace: Workspace fixture.
        """
        _event(workspace, customer="jp", amount="50000", currency="JPY")
        snap = compute_snapshot(workspace, WEEK, now=NOW)
        assert snap["industries"][0]["bands"] == [0, 1, 0, 0]

    def test_currencies_are_never_mixed(self, workspace: Workspace) -> None:
        """Totals keep a per-currency map; the primary is the most common.

        Args:
            workspace: Workspace fixture.
        """
        _event(workspace, customer="a", amount="10", currency="EUR")
        _event(workspace, customer="b", amount="20", currency="EUR")
        _event(workspace, customer="c", amount="1000", currency="USD")
        snap = compute_snapshot(workspace, WEEK, now=NOW)
        assert snap["primary_currency"] == "EUR"
        assert snap["totals"]["revenue"] == {"EUR": "30.000", "USD": "1000.000"}
        assert format_money_map(snap["totals"]["revenue"], "EUR") == "€30 · $1,000"

    def test_identity_is_borrowed_for_rows_without_email(
        self, workspace: Workspace
    ) -> None:
        """A sale without an email inherits the customer's known domain.

        Args:
            workspace: Workspace fixture.
        """
        _company("saas.example", "Software & SaaS")
        _event(
            workspace,
            customer="a",
            domain="saas.example",
            at=datetime(2026, 11, 2, tzinfo=UTC),
        )
        _event(workspace, customer="a", amount="70")
        snap = compute_snapshot(workspace, WEEK, now=NOW)
        assert [r["industry"] for r in snap["industries"]] == ["Software & SaaS"]

    def test_industry_corrections_apply_retroactively(
        self, workspace: Workspace
    ) -> None:
        """Changing a company's industry changes past snapshots too.

        Args:
            workspace: Workspace fixture.
        """
        company = _company("x.example", "Other")
        _event(workspace, customer="a", domain="x.example")
        company.brand_info = {"industry": "Cybersecurity"}
        company.save()
        snap = compute_snapshot(workspace, WEEK, now=NOW)
        assert snap["industries"][0]["industry"] == "Cybersecurity"

    def test_people_only_when_asked(self, workspace: Workspace) -> None:
        """Stored snapshots never contain customer names or emails.

        Args:
            workspace: Workspace fixture.
        """
        details = {"email": "jane@corp.example", "company_name": "Corp Inc"}
        _event(workspace, customer="a", details=details)
        _event(
            workspace,
            "failure",
            customer="b",
            details={"email": "bob@corp.example"},
            next_attempt=NOW + timedelta(days=2),
        )

        private = compute_snapshot(workspace, WEEK, include_people=True, now=NOW)
        assert private["industries"][0]["people"][0]["name"] == "Corp Inc"
        assert private["recovery"]["attention"][0]["name"] == "bob@corp.example"

        stored = store_snapshot(workspace, WEEK, NOW)
        dumped = json.dumps(stored.data)
        assert "corp.example" not in dumped
        assert "Corp Inc" not in dumped
        assert "people" not in dumped and "attention" not in dumped

    def test_other_workspaces_are_invisible(self, workspace: Workspace) -> None:
        """A snapshot only ever reads its own workspace's ledger.

        Args:
            workspace: Workspace fixture.
        """
        other = Workspace.objects.create(name="Other")
        _event(other, customer="z", amount="999")
        snap = compute_snapshot(workspace, WEEK, now=NOW)
        assert snap["totals"]["customers"] == 0


@pytest.mark.django_db
class TestRecovery:
    """Failed payments are followed to recovered / at risk / lost."""

    def test_bounced_twice_then_paid_is_one_recovered_sale(
        self, workspace: Workspace
    ) -> None:
        """Two declines and a success: recovered, and one sale in revenue.

        Args:
            workspace: Workspace fixture.
        """
        monday = datetime(2026, 11, 16, 9, tzinfo=UTC)
        _event(workspace, "failure", charge="inv", at=monday, attempt=1)
        _event(
            workspace,
            "failure",
            charge="inv",
            at=monday + timedelta(days=1),
            attempt=2,
        )
        _event(workspace, "sale", charge="inv", at=monday + timedelta(days=2))

        snap = compute_snapshot(workspace, WEEK, now=NOW)
        recovery = snap["recovery"]
        assert recovery["recovered"]["count"] == 1
        assert recovery["at_risk"]["count"] == 0
        assert recovery["rate"] == 100.0
        assert recovery["median_hours_to_recover"] == 48.0
        assert snap["totals"]["revenue"] == {"USD": "100.000"}

    def test_scheduled_retry_is_at_risk(self, workspace: Workspace) -> None:
        """A failure with a future retry is still at risk.

        Args:
            workspace: Workspace fixture.
        """
        _event(workspace, "failure", next_attempt=NOW + timedelta(days=3))
        recovery = compute_snapshot(workspace, WEEK, now=NOW)["recovery"]
        assert recovery["at_risk"]["count"] == 1
        assert recovery["rate"] is None

    def test_cancellation_after_failure_is_lost(self, workspace: Workspace) -> None:
        """A customer who cancels after a decline is lost.

        Args:
            workspace: Workspace fixture.
        """
        _event(workspace, "failure", customer="a", next_attempt=NOW + timedelta(1))
        _event(
            workspace,
            "cancellation",
            customer="a",
            at=datetime(2026, 11, 17, tzinfo=UTC),
        )
        recovery = compute_snapshot(workspace, WEEK, now=NOW)["recovery"]
        assert recovery["lost"]["count"] == 1
        assert recovery["rate"] == 0.0

    def test_exhausted_retries_are_lost(self, workspace: Workspace) -> None:
        """A retry date long past with no payment means lost.

        Args:
            workspace: Workspace fixture.
        """
        _event(
            workspace,
            "failure",
            at=datetime(2026, 11, 2, tzinfo=UTC),
            next_attempt=datetime(2026, 11, 5, tzinfo=UTC),
        )
        period = WEEK.previous().previous()
        recovery = compute_snapshot(workspace, period, now=NOW)["recovery"]
        assert recovery["lost"]["count"] == 1


@pytest.mark.django_db
class TestComparison:
    """Period-over-period changes."""

    def test_compare_against_previous_week(self, workspace: Workspace) -> None:
        """Revenue and new-customer changes use the primary currency.

        Args:
            workspace: Workspace fixture.
        """
        _event(workspace, customer="a", amount="200", segment="new")
        _event(
            workspace,
            customer="b",
            amount="100",
            at=datetime(2026, 11, 10, tzinfo=UTC),
        )
        current = compute_snapshot(workspace, WEEK, now=NOW)
        previous = compute_snapshot(workspace, WEEK.previous(), now=NOW)
        changes = compare(current, previous)
        assert changes["revenue_pct"] == pytest.approx(100.0)
        assert changes["new_customers_diff"] == 1
        assert compare(current, None)["revenue_pct"] is None

    def test_stored_snapshot_survives_purge(self, workspace: Workspace) -> None:
        """Once stored, a period no longer needs its ledger rows.

        Args:
            workspace: Workspace fixture.
        """
        _event(workspace, amount="80")
        store_snapshot(workspace, WEEK, NOW)
        TransactionEvent.objects.all().delete()
        snapshot, partial = stored_or_computed(workspace, WEEK, NOW)
        assert snapshot is not None and not partial
        assert snapshot["totals"]["revenue"] == {"USD": "80.000"}
        assert SalesSnapshot.objects.count() == 1

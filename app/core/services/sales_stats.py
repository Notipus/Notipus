"""Weekly and monthly sales snapshot, computed from the sales ledger.

A snapshot answers the questions a sales or customer-success team asks on
a Monday morning: how much came in, how much of it was new business versus
existing customers, which industries it came from, and which failed
payments recovered, are still at risk, or were lost.

Money is never converted between currencies: every amount is a
``{currency: amount}`` map, and percentages and shares are computed in the
period's *primary* currency (the one most sales were billed in). Revenue
bands use USD thresholds scaled per currency with
:func:`webhooks.utils.currency.usd_threshold_in` - the module's documented
use - and customers billed in a currency without a factor are left out of
the bands rather than compared against a dollar threshold.

Snapshots come in two shapes. :func:`compute_snapshot` with
``include_people=True`` (the dashboard) adds per-customer lists that are
decrypted from the ledger on the fly. Without it (stored
:class:`core.models.SalesSnapshot`, digests) the result holds aggregates
only and is safe to keep after the ledger rows are purged.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.models import Company, SalesSnapshot, TransactionEvent, Workspace
from django.db.models import Min
from django.utils import timezone
from webhooks.utils.currency import format_money, usd_threshold_in
from webhooks.utils.payment_service_domains import is_payment_service_domain

from .sales_ledger import (
    KIND_CANCELLATION,
    KIND_FAILURE,
    KIND_REFUND,
    KIND_SALE,
    KIND_TRIAL_STARTED,
    RETENTION_DAYS,
    SEGMENT_EXISTING,
    SEGMENT_NEW,
    SEGMENT_UNKNOWN,
)

PERIOD_WEEK = SalesSnapshot.PERIOD_WEEK
PERIOD_MONTH = SalesSnapshot.PERIOD_MONTH
PERIODS = (PERIOD_WEEK, PERIOD_MONTH)

INDUSTRY_CONSUMER = "Consumers / personal email"
INDUSTRY_UNKNOWN = "Unknown company"

# Revenue bands per customer and period, as USD thresholds.
BAND_THRESHOLDS_USD: tuple[int, ...] = (100, 1000, 5000)
BAND_LABELS: tuple[str, ...] = ("Under $100", "$100–1k", "$1k–5k", "$5k+")

# A failure with no scheduled retry is still "at risk" for this long; after
# that, with no payment and no retry in sight, it counts as lost.
AT_RISK_WINDOW = timedelta(days=7)
# Grace after a scheduled retry before a silent chain is called lost (the
# success webhook can lag the retry itself).
RETRY_GRACE = timedelta(days=2)

_ZERO = Decimal("0")


@dataclass(frozen=True)
class Period:
    """A calendar week (Mon-Sun) or month in a workspace's timezone.

    Attributes:
        kind: ``"week"`` or ``"month"``.
        start: First day of the period.
        end: First day AFTER the period (exclusive).
        tz: The workspace timezone the boundaries are in.
    """

    kind: str
    start: date
    end: date
    tz: ZoneInfo

    @property
    def start_dt(self) -> datetime:
        """Return the aware datetime the period starts at."""
        return datetime.combine(self.start, time.min, tzinfo=self.tz)

    @property
    def end_dt(self) -> datetime:
        """Return the aware datetime the period ends at (exclusive)."""
        return datetime.combine(self.end, time.min, tzinfo=self.tz)

    @property
    def last_day(self) -> date:
        """Return the last day inside the period."""
        return self.end - timedelta(days=1)

    @property
    def label(self) -> str:
        """Return a human label, e.g. ``Week 46 · Nov 9 – 15, 2026``."""
        if self.kind == PERIOD_MONTH:
            return self.start.strftime("%B %Y")
        week = self.start.isocalendar().week
        last = self.last_day
        if last.month == self.start.month:
            span = f"{self.start:%b} {self.start.day} – {last.day}, {last.year}"
        else:
            span = (
                f"{self.start:%b} {self.start.day} – {last:%b} {last.day}, {last.year}"
            )
        return f"Week {week} · {span}"

    def previous(self) -> Period:
        """Return the period immediately before this one."""
        return period_containing(self.kind, self.start - timedelta(days=1), self.tz)

    def following(self) -> Period:
        """Return the period immediately after this one."""
        return period_containing(self.kind, self.end, self.tz)


def workspace_tz(workspace: Workspace) -> ZoneInfo:
    """Return the workspace's timezone, falling back to UTC.

    Args:
        workspace: The workspace.

    Returns:
        A ZoneInfo; UTC when the stored name is empty or unknown.
    """
    try:
        return ZoneInfo(workspace.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def period_containing(kind: str, day: date, tz: ZoneInfo) -> Period:
    """Return the week or month that contains ``day``.

    Args:
        kind: ``"week"`` or ``"month"``.
        day: Any day inside the wanted period.
        tz: Timezone the period boundaries are in.

    Returns:
        The Period.

    Raises:
        ValueError: If ``kind`` is not a known period.
    """
    if kind == PERIOD_WEEK:
        start = day - timedelta(days=day.weekday())
        return Period(kind, start, start + timedelta(days=7), tz)
    if kind == PERIOD_MONTH:
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        return Period(kind, start, end, tz)
    raise ValueError(f"Unknown period: {kind}")


def current_period(kind: str, tz: ZoneInfo, now: datetime | None = None) -> Period:
    """Return the period that contains ``now`` in ``tz``.

    Args:
        kind: ``"week"`` or ``"month"``.
        tz: Workspace timezone.
        now: Reference time (defaults to the current time).

    Returns:
        The Period.
    """
    local = (now or timezone.now()).astimezone(tz)
    return period_containing(kind, local.date(), tz)


def ledger_covers(period: Period, now: datetime | None = None) -> bool:
    """Return whether the raw ledger still holds the whole period.

    Args:
        period: The period to check.
        now: Reference time.

    Returns:
        True when no part of the period has been purged yet.
    """
    oldest = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    return period.start_dt >= oldest


# --------------------------------------------------------------------------
# Money helpers
# --------------------------------------------------------------------------

MoneyMap = dict[str, Decimal]


def _add(target: MoneyMap, currency: str, amount: Decimal) -> None:
    """Add ``amount`` in ``currency`` to a money map in place."""
    target[currency] = target.get(currency, _ZERO) + amount


def _serialize_money(money: MoneyMap) -> dict[str, str]:
    """Return a JSON-safe copy of a money map, dropping zero entries."""
    return {cur: str(amount) for cur, amount in sorted(money.items()) if amount}


def deserialize_money(money: dict[str, Any] | None) -> MoneyMap:
    """Parse a stored money map back into Decimals.

    Args:
        money: ``{currency: "amount"}`` as stored in a snapshot.

    Returns:
        ``{currency: Decimal}``.
    """
    return {cur: Decimal(str(value)) for cur, value in (money or {}).items()}


def format_money_map(
    money: dict[str, Any] | None, primary: str = "", decimals: int | None = 0
) -> str:
    """Format a money map for display, primary currency first.

    Args:
        money: ``{currency: amount}`` (Decimal or string amounts).
        primary: Currency to list first.
        decimals: Decimal places (0 by default: snapshots are summaries).

    Returns:
        E.g. ``"$1,200 · €300"``, or the primary currency's zero.
    """
    parsed = deserialize_money(money)
    items = [(cur, amt) for cur, amt in parsed.items() if amt]
    if not items:
        return str(format_money(0, primary or "USD", decimals))
    items.sort(key=lambda item: (item[0] != primary, -item[1], item[0]))
    return " · ".join(format_money(amt, cur, decimals) for cur, amt in items)


def _pct_change(current: Decimal, previous: Decimal) -> float | None:
    """Return the percentage change, or None when there is no baseline."""
    if not previous:
        return None
    return float((current - previous) / previous * 100)


# --------------------------------------------------------------------------
# Snapshot computation
# --------------------------------------------------------------------------


def _industry_lookup(domains: set[str]) -> dict[str, str]:
    """Return each domain's current industry from the shared Company table.

    Industry corrections (``ClassificationFeedback``) are already folded
    into ``brand_info["industry"]`` by ``apply_feedback``, so reading it at
    aggregation time picks them up for past periods too.
    """
    if not domains:
        return {}
    result: dict[str, str] = {}
    for domain, brand_info in Company.objects.filter(domain__in=domains).values_list(
        "domain", "brand_info"
    ):
        industry = (brand_info or {}).get("industry")
        if isinstance(industry, str) and industry.strip():
            result[domain] = industry.strip()
    return result


def _customer_identity(workspace: Workspace) -> dict[str, tuple[str, bool]]:
    """Map each customer_ref to its best-known (domain, is_consumer).

    Stripe subscription events (cancellations, for one) carry no email,
    so their rows have no domain. Borrowing it from the same customer's
    invoice rows keeps those events out of the "Unknown company" bucket.
    """
    identity: dict[str, tuple[str, bool]] = {}
    rows = (
        TransactionEvent.objects.filter(workspace=workspace)
        .exclude(company_domain="", is_consumer=False)
        .order_by("occurred_at")
        .values_list("customer_ref", "company_domain", "is_consumer")
    )
    for customer_ref, domain, is_consumer in rows:
        if is_payment_service_domain(domain):
            # Rows recorded before payment services were recognised carry
            # the intermediary's domain (bill.com); it says nothing about
            # who the customer is.
            continue
        identity[customer_ref] = (domain, is_consumer)
    return identity


def _band_index(amount: Decimal, currency: str) -> int | None:
    """Return the revenue band for an amount, or None if it can't be banded."""
    thresholds = [usd_threshold_in(usd, currency) for usd in BAND_THRESHOLDS_USD]
    if any(threshold is None for threshold in thresholds):
        return None
    for index, threshold in enumerate(thresholds):
        if amount < Decimal(str(threshold)):
            return index
    return len(BAND_THRESHOLDS_USD)


@dataclass
class _Customer:
    """Per-customer accumulator for one period."""

    industry: str
    sales: MoneyMap
    refunds: MoneyMap
    segments: set[str]
    name: str = ""

    @property
    def segment(self) -> str:
        """Return the customer's segment for the period."""
        if SEGMENT_NEW in self.segments:
            return SEGMENT_NEW
        if SEGMENT_EXISTING in self.segments:
            return SEGMENT_EXISTING
        return SEGMENT_UNKNOWN

    def net(self) -> MoneyMap:
        """Return sales minus refunds per currency."""
        net = dict(self.sales)
        for cur, amount in self.refunds.items():
            _add(net, cur, -amount)
        return net


def _display_name(details: dict[str, Any] | None, industry: str) -> str:
    """Pick the most useful human label for a customer."""
    details = details or {}
    for key in ("company_name", "name", "email"):
        value = details.get(key)
        if value:
            return str(value)
    return industry


_SNAPSHOT_FIELDS = (
    "kind",
    "segment",
    "amount",
    "currency",
    "company_domain",
    "is_consumer",
    "customer_ref",
    "charge_ref",
    "attempt_count",
    "next_attempt_at",
    "occurred_at",
)


class _IndustryResolver:
    """Resolve a ledger row to the industry it is reported under."""

    def __init__(self, workspace: Workspace, rows: list[TransactionEvent]) -> None:
        """Load customer identities and company industries in two queries.

        Args:
            workspace: The workspace.
            rows: The ledger rows that will be resolved.
        """
        self.identity = _customer_identity(workspace)
        domains = {row.company_domain for row in rows if row.company_domain}
        domains |= {domain for domain, _ in self.identity.values() if domain}
        self.industries = _industry_lookup(domains)

    def __call__(self, row: TransactionEvent) -> str:
        """Return the row's industry bucket.

        Args:
            row: A ledger row.

        Returns:
            The industry, or the consumer / unknown bucket.
        """
        domain, is_consumer = row.company_domain, row.is_consumer
        if is_payment_service_domain(domain):
            domain = ""
        if not domain and not is_consumer:
            domain, is_consumer = self.identity.get(row.customer_ref, ("", False))
        if is_consumer:
            return INDUSTRY_CONSUMER
        if domain:
            return self.industries.get(domain, INDUSTRY_UNKNOWN)
        return INDUSTRY_UNKNOWN


@dataclass
class _Totals:
    """Accumulates one period's ledger rows."""

    include_people: bool
    customers: dict[str, _Customer]
    segment_sales: dict[str, MoneyMap]
    refunds: MoneyMap
    sale_currencies: Counter[str]
    refund_count: int = 0
    cancellations: int = 0
    trials: int = 0

    @classmethod
    def empty(cls, include_people: bool) -> _Totals:
        """Return an empty accumulator."""
        return cls(include_people, {}, defaultdict(dict), {}, Counter())

    def add(self, row: TransactionEvent, industry: str) -> None:
        """Fold one ledger row into the totals.

        Args:
            row: The ledger row.
            industry: The industry bucket the row belongs to.
        """
        if row.kind == KIND_SALE:
            customer = self.customers.setdefault(
                row.customer_ref, _Customer(industry, {}, {}, set())
            )
            _add(customer.sales, row.currency, row.amount)
            customer.segments.add(row.segment)
            if self.include_people and not customer.name:
                customer.name = _display_name(row.details, industry)
            _add(self.segment_sales[row.segment], row.currency, row.amount)
            self.sale_currencies[row.currency] += 1
        elif row.kind == KIND_REFUND:
            self.refund_count += 1
            _add(self.refunds, row.currency, row.amount)
            customer = self.customers.setdefault(
                row.customer_ref, _Customer(industry, {}, {}, set())
            )
            _add(customer.refunds, row.currency, row.amount)
            if self.include_people and not customer.name:
                customer.name = _display_name(row.details, industry)
        elif row.kind == KIND_CANCELLATION:
            self.cancellations += 1
        elif row.kind == KIND_TRIAL_STARTED:
            self.trials += 1

    @property
    def primary(self) -> str:
        """Return the currency most sales were billed in (USD if none)."""
        if not self.sale_currencies:
            return "USD"
        ranked = sorted(self.sale_currencies.items(), key=lambda i: (-i[1], i[0]))
        return ranked[0][0]

    def gross(self) -> MoneyMap:
        """Return total sales per currency."""
        gross: MoneyMap = {}
        for money in self.segment_sales.values():
            for cur, amount in money.items():
                _add(gross, cur, amount)
        return gross

    def net(self) -> MoneyMap:
        """Return sales minus refunds per currency."""
        net = self.gross()
        for cur, amount in self.refunds.items():
            _add(net, cur, -amount)
        return net

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-safe ``totals`` block of a snapshot."""
        segments = Counter(c.segment for c in self.customers.values() if c.sales)
        new, existing, unknown = (
            SEGMENT_NEW,
            SEGMENT_EXISTING,
            SEGMENT_UNKNOWN,
        )
        return {
            "customers": sum(bool(c.sales) for c in self.customers.values()),
            "revenue": _serialize_money(self.net()),
            "gross": _serialize_money(self.gross()),
            "new_customers": segments[new],
            "existing_customers": segments[existing],
            "unknown_customers": segments[unknown],
            "new_revenue": _serialize_money(self.segment_sales.get(new, {})),
            "existing_revenue": _serialize_money(self.segment_sales.get(existing, {})),
            "unknown_revenue": _serialize_money(self.segment_sales.get(unknown, {})),
            "refunds": _serialize_money(self.refunds),
            "refund_count": self.refund_count,
            "cancellations": self.cancellations,
            "trials_started": self.trials,
        }


def _new_industry_entry() -> dict[str, Any]:
    """Return an empty per-industry accumulator."""
    return {
        "customers": 0,
        "new_customers": 0,
        "existing_customers": 0,
        "unknown_customers": 0,
        "revenue": {},
        "new_revenue": {},
        "existing_revenue": {},
        "bands": [0] * (len(BAND_THRESHOLDS_USD) + 1),
        "people": [],
    }


def _add_customer_to_industry(
    entry: dict[str, Any], customer: _Customer, primary: str, include_people: bool
) -> None:
    """Fold one customer's period into their industry's accumulator."""
    if customer.sales:
        entry["customers"] += 1
        entry[f"{customer.segment}_customers"] += 1
    customer_net = customer.net()
    for cur, amount in customer_net.items():
        _add(entry["revenue"], cur, amount)
        if customer.segment != SEGMENT_UNKNOWN:
            _add(entry[f"{customer.segment}_revenue"], cur, amount)
    if customer.sales:
        main_cur, main_amount = max(
            customer.sales.items(), key=lambda item: (item[1], item[0])
        )
        band = _band_index(main_amount, main_cur)
        if band is not None:
            entry["bands"][band] += 1
    if include_people:
        entry["people"].append(
            {
                "name": customer.name,
                "segment": customer.segment,
                "revenue": _serialize_money(customer_net),
                "sort": float(customer_net.get(primary, _ZERO)),
            }
        )


def _industry_rows(totals: _Totals) -> list[dict[str, Any]]:
    """Return the per-industry breakdown, biggest primary revenue first."""
    primary = totals.primary
    by_industry: dict[str, dict[str, Any]] = defaultdict(_new_industry_entry)
    for customer in totals.customers.values():
        _add_customer_to_industry(
            by_industry[customer.industry], customer, primary, totals.include_people
        )

    total_primary = totals.net().get(primary, _ZERO)
    rows: list[dict[str, Any]] = []
    for name, entry in by_industry.items():
        primary_revenue = entry["revenue"].get(primary, _ZERO)
        row: dict[str, Any] = {
            "industry": name,
            "customers": entry["customers"],
            "new_customers": entry["new_customers"],
            "existing_customers": entry["existing_customers"],
            "unknown_customers": entry["unknown_customers"],
            "revenue": _serialize_money(entry["revenue"]),
            "new_revenue": _serialize_money(entry["new_revenue"]),
            "existing_revenue": _serialize_money(entry["existing_revenue"]),
            "share": float(primary_revenue / total_primary * 100)
            if total_primary
            else 0.0,
            "avg_per_customer": str(
                (primary_revenue / entry["customers"]).quantize(Decimal("0.01"))
                if entry["customers"]
                else _ZERO
            ),
            "bands": entry["bands"],
        }
        if totals.include_people:
            row["people"] = sorted(
                entry["people"], key=lambda p: (-p["sort"], p["name"])
            )
        rows.append(row)
    rows.sort(
        key=lambda r: (
            r["industry"] in (INDUSTRY_CONSUMER, INDUSTRY_UNKNOWN),
            -Decimal(r["revenue"].get(primary, "0")),
            r["industry"],
        )
    )
    return rows


def compute_snapshot(
    workspace: Workspace,
    period: Period,
    *,
    include_people: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute the sales snapshot for one period from the ledger.

    Args:
        workspace: The workspace.
        period: The week or month to compute.
        include_people: Add per-customer lists (names come from the
            encrypted ledger column). Never store a snapshot built with
            this flag.
        now: Reference time for payment-recovery status.

    Returns:
        A JSON-serializable snapshot dict.
    """
    now = now or timezone.now()
    fields = [*_SNAPSHOT_FIELDS, *(["details"] if include_people else [])]
    rows = list(
        TransactionEvent.objects.filter(
            workspace=workspace,
            occurred_at__gte=period.start_dt,
            occurred_at__lt=period.end_dt,
        )
        .only(*fields)
        .order_by("occurred_at")
    )

    industry_of = _IndustryResolver(workspace, rows)
    totals = _Totals.empty(include_people)
    for row in rows:
        totals.add(row, industry_of(row))
    primary = totals.primary

    snapshot: dict[str, Any] = {
        "period": {
            "kind": period.kind,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "label": period.label,
        },
        "primary_currency": primary,
        "computed_at": now.isoformat(),
        "totals": totals.as_dict(),
        "industries": _industry_rows(totals),
        "bands": list(BAND_LABELS),
        "recovery": _compute_recovery(
            workspace, period, now=now, include_people=include_people
        ),
    }
    snapshot["highlights"] = _highlights(snapshot, totals.customers, primary)
    return snapshot


_RECOVERY_STATUSES = ("recovered", "at_risk", "lost")


@dataclass
class _Outcomes:
    """Later sales and cancellations that decide a failed chain's fate."""

    sales_by_chain: dict[str, list[datetime]]
    cancels_by_customer: dict[str, list[datetime]]

    @classmethod
    def load(cls, workspace: Workspace, since: datetime) -> _Outcomes:
        """Load every sale and cancellation from ``since`` onwards.

        Args:
            workspace: The workspace.
            since: The earliest failure being followed.

        Returns:
            The indexed outcomes.
        """
        outcomes = cls(defaultdict(list), defaultdict(list))
        later = TransactionEvent.objects.filter(
            workspace=workspace,
            occurred_at__gte=since,
            kind__in=(KIND_SALE, KIND_CANCELLATION),
        ).values_list("kind", "charge_ref", "customer_ref", "occurred_at")
        for kind, charge_ref, customer_ref, occurred_at in later:
            if kind == KIND_SALE:
                outcomes.sales_by_chain[charge_ref].append(occurred_at)
            else:
                outcomes.cancels_by_customer[customer_ref].append(occurred_at)
        return outcomes

    def status(
        self, chain: list[TransactionEvent], now: datetime
    ) -> tuple[str, float | None]:
        """Classify one failed chain.

        Args:
            chain: The chain's failures, oldest first.
            now: Reference time.

        Returns:
            ``(status, hours_to_recover)``; hours only for recovered chains.
        """
        first, last = chain[0], chain[-1]
        paid = [
            t
            for t in self.sales_by_chain.get(first.charge_ref, [])
            if t > first.occurred_at
        ]
        if paid:
            return "recovered", (min(paid) - first.occurred_at).total_seconds() / 3600
        cancels = self.cancels_by_customer.get(first.customer_ref, [])
        if any(t > first.occurred_at for t in cancels):
            return "lost", None
        if last.next_attempt_at is not None:
            alive = last.next_attempt_at + RETRY_GRACE > now
        else:
            alive = last.occurred_at + AT_RISK_WINDOW > now
        return ("at_risk" if alive else "lost"), None


def _attention_item(chain: list[TransactionEvent]) -> dict[str, Any]:
    """Describe an at-risk chain for the dashboard's follow-up list."""
    first, last = chain[0], chain[-1]
    amount = max(f.amount for f in chain)
    return {
        "name": _display_name(first.details, ""),
        "amount": format_money(amount, first.currency, 0),
        "failures": max([len(chain), *(f.attempt_count or 0 for f in chain)]),
        "next_attempt_at": last.next_attempt_at.isoformat()
        if last.next_attempt_at
        else "",
        "reason": (last.details or {}).get("failure_reason", ""),
    }


def _recovery_episodes(
    chain: list[TransactionEvent], payments: list[datetime]
) -> list[list[TransactionEvent]]:
    """Split subscription failures whenever a successful payment settles an episode."""
    episodes: list[list[TransactionEvent]] = []
    current: list[TransactionEvent] = []
    payments = sorted(payments)
    payment_index = 0
    for failure in chain:
        while (
            payment_index < len(payments)
            and payments[payment_index] <= failure.occurred_at
        ):
            paid_at = payments[payment_index]
            if current and paid_at > current[0].occurred_at:
                episodes.append(current)
                current = []
            payment_index += 1
        current.append(failure)
    if current:
        episodes.append(current)
    return episodes


def _compute_recovery(
    workspace: Workspace,
    period: Period,
    *,
    now: datetime,
    include_people: bool,
) -> dict[str, Any]:
    """Follow every payment that failed in the period to its outcome.

    A chain (one invoice, or one subscription for providers that open a new
    transaction per retry) is:

    * recovered - a sale on the same chain after the first failure;
    * lost - the customer cancelled after the failure, or the retry
      schedule ran out with no payment;
    * at risk - otherwise: a retry is scheduled, or the failure is recent.

    A recovered payment is one sale in the totals - never a failure plus
    a sale - because only the sale is revenue.
    """
    failures = list(
        TransactionEvent.objects.filter(
            workspace=workspace,
            kind=KIND_FAILURE,
            occurred_at__gte=period.start_dt,
            occurred_at__lt=period.end_dt,
        )
        .only(
            "charge_ref",
            "customer_ref",
            "amount",
            "currency",
            "attempt_count",
            "next_attempt_at",
            "occurred_at",
            *(["details"] if include_people else []),
        )
        .order_by("occurred_at")
    )
    chains: dict[str, list[TransactionEvent]] = defaultdict(list)
    for failure in failures:
        chains[failure.charge_ref].append(failure)

    counts: Counter[str] = Counter()
    amounts: dict[str, MoneyMap] = {status: {} for status in _RECOVERY_STATUSES}
    hours_to_recover: list[float] = []
    attention: list[dict[str, Any]] = []
    if chains:
        outcomes = _Outcomes.load(workspace, failures[0].occurred_at)
        episodes = [
            episode
            for chain in chains.values()
            for episode in _recovery_episodes(
                chain, outcomes.sales_by_chain.get(chain[0].charge_ref, [])
            )
        ]
        for chain in episodes:
            status, hours = outcomes.status(chain, now)
            counts[status] += 1
            _add(amounts[status], chain[0].currency, max(f.amount for f in chain))
            if hours is not None:
                hours_to_recover.append(hours)
            if status == "at_risk" and include_people:
                attention.append(_attention_item(chain))

    resolved = counts["recovered"] + counts["lost"]
    result: dict[str, Any] = {
        status: {"count": counts[status], "amount": _serialize_money(amounts[status])}
        for status in _RECOVERY_STATUSES
    }
    result["rate"] = (
        round(counts["recovered"] / resolved * 100, 1) if resolved else None
    )
    result["median_hours_to_recover"] = (
        round(statistics.median(hours_to_recover), 1) if hours_to_recover else None
    )
    if include_people:
        attention.sort(key=lambda a: (a["next_attempt_at"] or "9999", a["name"]))
        result["attention"] = attention
    return result


def _highlights(
    snapshot: dict[str, Any], customers: dict[str, _Customer], primary: str
) -> list[str]:
    """Return a few plain-language observations about the period."""
    highlights: list[str] = []
    rows = [
        r
        for r in snapshot["industries"]
        if r["industry"] not in (INDUSTRY_CONSUMER, INDUSTRY_UNKNOWN)
    ]
    total_customers = snapshot["totals"]["customers"]

    candidates = [r for r in rows if r["customers"] >= 2 and r["share"] > 0]
    if candidates and total_customers:
        best = max(candidates, key=lambda r: Decimal(r["avg_per_customer"]))
        customer_share = best["customers"] / total_customers * 100
        if best["share"] > customer_share:
            highlights.append(
                f"{best['industry']} is {customer_share:.0f}% of customers but "
                f"{best['share']:.0f}% of revenue (highest average per customer, "
                f"{format_money(Decimal(best['avg_per_customer']), primary, 0)})."
            )

    with_new = [r for r in rows if r["new_customers"]]
    if with_new:
        top = max(with_new, key=lambda r: (r["new_customers"], r["industry"]))
        highlights.append(
            f"{top['industry']} brought the most new customers "
            f"({top['new_customers']} of {top['customers']})."
        )

    if len(customers) > 10:
        net_primary = sorted(
            (c.net().get(primary, _ZERO) for c in customers.values()), reverse=True
        )
        total = sum(net_primary, _ZERO)
        if total > 0:
            top_share = sum(net_primary[:10], _ZERO) / total * 100
            highlights.append(
                f"Your top 10 customers made up {top_share:.0f}% of revenue."
            )
    return highlights


# --------------------------------------------------------------------------
# Comparison and storage
# --------------------------------------------------------------------------


def stored_or_computed(
    workspace: Workspace, period: Period, now: datetime | None = None
) -> tuple[dict[str, Any] | None, bool]:
    """Return a PII-free snapshot for a period, preferring the stored one.

    Args:
        workspace: The workspace.
        period: The period wanted.
        now: Reference time.

    Returns:
        ``(snapshot, partial)``: the snapshot (None when neither a stored
        one nor ledger data exists) and whether it was computed from a
        ledger that no longer covers the whole period.
    """
    stored = SalesSnapshot.objects.filter(
        workspace=workspace, period=period.kind, start=period.start
    ).first()
    if stored is not None and stored.data:
        return stored.data, False
    partial = not ledger_covers(period, now)
    snapshot = compute_snapshot(workspace, period, now=now)
    if partial and not snapshot["totals"]["customers"]:
        return None, True
    return snapshot, partial


def compare(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Return changes from the previous period, in the primary currency.

    Args:
        current: This period's snapshot.
        previous: The previous period's snapshot, or None.

    Returns:
        ``revenue_pct``, ``new_revenue_pct``, ``existing_revenue_pct``,
        ``new_customers_diff``, ``cancellations_diff`` and per-industry
        ``industry_revenue_pct`` (None where there is no baseline).
    """
    primary = current["primary_currency"]
    if not previous:
        return {
            "revenue_pct": None,
            "new_revenue_pct": None,
            "existing_revenue_pct": None,
            "new_customers_diff": None,
            "cancellations_diff": None,
            "industry_revenue_pct": {},
        }

    def primary_amount(snapshot: dict[str, Any], key: str) -> Decimal:
        return deserialize_money(snapshot["totals"].get(key)).get(primary, _ZERO)

    previous_industries = {
        row["industry"]: deserialize_money(row["revenue"]).get(primary, _ZERO)
        for row in previous.get("industries", [])
    }
    return {
        "revenue_pct": _pct_change(
            primary_amount(current, "revenue"), primary_amount(previous, "revenue")
        ),
        "new_revenue_pct": _pct_change(
            primary_amount(current, "new_revenue"),
            primary_amount(previous, "new_revenue"),
        ),
        "existing_revenue_pct": _pct_change(
            primary_amount(current, "existing_revenue"),
            primary_amount(previous, "existing_revenue"),
        ),
        "new_customers_diff": current["totals"]["new_customers"]
        - previous["totals"]["new_customers"],
        "cancellations_diff": current["totals"]["cancellations"]
        - previous["totals"]["cancellations"],
        "industry_revenue_pct": {
            row["industry"]: _pct_change(
                deserialize_money(row["revenue"]).get(primary, _ZERO),
                previous_industries.get(row["industry"], _ZERO),
            )
            for row in current["industries"]
        },
    }


def store_snapshot(
    workspace: Workspace, period: Period, now: datetime | None = None
) -> SalesSnapshot:
    """Compute a closed period's PII-free snapshot and store it.

    Args:
        workspace: The workspace.
        period: The (closed) period.
        now: Reference time.

    Returns:
        The stored SalesSnapshot (created or refreshed).
    """
    if not ledger_covers(period, now):
        stored = SalesSnapshot.objects.filter(
            workspace=workspace, period=period.kind, start=period.start
        ).first()
        if stored is not None:
            return stored
    data = compute_snapshot(workspace, period, now=now)
    snapshot, _ = SalesSnapshot.objects.update_or_create(
        workspace=workspace,
        period=period.kind,
        start=period.start,
        defaults={"end": period.end, "data": data},
    )
    return snapshot


SNAPSHOT_RETENTION_DAYS = 400


def purge_old_snapshots(now: datetime | None = None) -> int:
    """Delete stored snapshots older than ~13 months.

    Args:
        now: Reference time.

    Returns:
        Number of snapshots deleted.
    """
    cutoff = (now or timezone.now()).date() - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    deleted, _ = SalesSnapshot.objects.filter(end__lt=cutoff).delete()
    return int(deleted)


def archive_closed_periods(
    workspaces: Any = None, *, now: datetime | None = None
) -> int:
    """Preserve closed weeks/months independently of digest delivery preferences.

    Called by the hourly digest job and before daily purging. Refresh only
    periods still fully covered by retention; never replace archived totals
    with a partial ledger. Late-arriving events/corrections refresh those
    periods while the underlying rows remain available.
    """
    now = now or timezone.now()
    workspaces = Workspace.objects.all() if workspaces is None else workspaces
    archived = 0
    for workspace in workspaces:
        first = TransactionEvent.objects.filter(workspace=workspace).aggregate(
            first=Min("occurred_at")
        )["first"]
        if first is None:
            continue
        tz = workspace_tz(workspace)
        first = max(first, now - timedelta(days=RETENTION_DAYS))
        for kind in PERIODS:
            period = period_containing(kind, first.astimezone(tz).date(), tz)
            current = current_period(kind, tz, now)
            while period.start < current.start:
                if ledger_covers(period, now):
                    store_snapshot(workspace, period, now)
                    archived += 1
                period = period.following()
    return archived

"""Regressions for sales history, delivery retries, recovery and event timing."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from core.models import SalesSnapshot, TransactionEvent, Workspace
from core.services.sales_digest import send_digest
from core.services.sales_ledger import event_occurred_at, purge_expired, record_event
from core.services.sales_stats import (
    archive_closed_periods,
    compute_snapshot,
    period_containing,
    store_snapshot,
    stored_or_computed,
)
from django.core.management import call_command
from django.test import RequestFactory
from plugins.destinations.slack import SlackDestinationPlugin
from plugins.sources.chargify import ChargifySourcePlugin
from plugins.sources.stripe import StripeSourcePlugin

pytestmark = pytest.mark.django_db
MONDAY = datetime(2026, 11, 16, 12, tzinfo=UTC)
WEEK = period_containing("week", MONDAY.date(), ZoneInfo("UTC"))
_seq = count()


@pytest.fixture
def workspace() -> Workspace:
    """Create a workspace with digest delivery disabled by default."""
    return Workspace.objects.create(name="Sales regressions")


def _row(
    workspace: Workspace, kind: str = "sale", **overrides: Any
) -> TransactionEvent:
    """Create a transaction, allowing tests to vary occurrence and chain."""
    fields = {
        "workspace": workspace,
        "provider": "chargify",
        "kind": kind,
        "segment": "existing",
        "customer_ref": "a",
        "charge_ref": "subscription-a",
        "amount": Decimal("100"),
        "currency": "USD",
        "occurred_at": MONDAY,
        "dedupe_key": str(next(_seq)),
        "details": {},
    }
    fields.update(overrides)
    return TransactionEvent.objects.create(**fields)


def test_opt_out_history_survives_purge(workspace: Workspace) -> None:
    """The hourly job archives history even when neither digest is enabled."""
    _row(workspace)
    with patch(
        "core.services.sales_digest.timezone.now",
        return_value=MONDAY + timedelta(days=7),
    ):
        call_command("send_sales_digest")
    assert SalesSnapshot.objects.filter(workspace=workspace, start=WEEK.start).exists()
    purge_expired(MONDAY + timedelta(days=50))
    snapshot, partial = stored_or_computed(workspace, WEEK, MONDAY + timedelta(days=50))
    assert snapshot is not None and not partial
    assert snapshot["totals"]["revenue"] == {"USD": "100.000"}


def test_purge_archives_both_periods_without_digest_job(workspace: Workspace) -> None:
    """Daily purging preserves weeks/months before their first rows expire."""
    october = datetime(2026, 10, 5, 12, tzinfo=UTC)
    _row(workspace, occurred_at=october)
    # The monthly snapshot becomes complete and archiveable on November 1.
    purge_expired(datetime(2026, 11, 1, 12, tzinfo=UTC))
    purge_expired(datetime(2026, 11, 12, 12, tzinfo=UTC))
    assert not TransactionEvent.objects.filter(workspace=workspace).exists()
    month = SalesSnapshot.objects.get(workspace=workspace, period="month")
    assert month.data["totals"]["revenue"] == {"USD": "100.000"}
    assert SalesSnapshot.objects.filter(workspace=workspace, period="week").exists()


def test_archiving_refreshes_late_events_without_resetting_delivery(
    workspace: Workspace,
) -> None:
    """Fresh raw data updates closed totals without discarding delivery receipts."""
    _row(workspace)
    now = MONDAY + timedelta(days=7)
    archive_closed_periods(now=now)
    stored = SalesSnapshot.objects.get(
        workspace=workspace, period="week", start=WEEK.start
    )
    stored.digest_delivered_to = ["slack"]
    stored.digest_sent_at = now
    stored.save()
    _row(workspace, amount=Decimal("25"))
    archive_closed_periods(now=now)
    stored.refresh_from_db()
    assert stored.data["totals"]["revenue"] == {"USD": "125.000"}
    assert stored.digest_delivered_to == ["slack"]
    assert stored.digest_sent_at == now


def test_recovery_splits_multiple_cycles(workspace: Workspace) -> None:
    """An earlier paid cycle cannot hide a later unpaid charge."""
    _row(workspace, "failure")
    _row(workspace, occurred_at=MONDAY + timedelta(days=1))
    _row(
        workspace,
        "failure",
        occurred_at=MONDAY + timedelta(days=2),
        next_attempt_at=MONDAY + timedelta(days=4),
    )
    recovery = compute_snapshot(workspace, WEEK, now=MONDAY + timedelta(days=3))[
        "recovery"
    ]
    assert recovery["recovered"]["count"] == 1
    assert recovery["at_risk"]["count"] == 1
    assert recovery["at_risk"]["amount"] == {"USD": "100.000"}


def test_each_paid_cycle_counts_once(workspace: Workspace) -> None:
    """Retries stay together, but two recovered cycles are two payments."""
    for day, kind in [
        (0, "failure"),
        (1, "failure"),
        (2, "sale"),
        (3, "failure"),
        (4, "sale"),
    ]:
        _row(workspace, kind, occurred_at=MONDAY + timedelta(days=day))
    recovery = compute_snapshot(workspace, WEEK, now=MONDAY + timedelta(days=5))[
        "recovery"
    ]
    assert recovery["recovered"]["count"] == 2
    assert recovery["recovered"]["amount"] == {"USD": "200.000"}
    assert recovery["at_risk"]["count"] == 0


def test_signed_chargify_signup_is_idempotent(workspace: Workspace) -> None:
    """A delivery retry reuses the signed body hash when no transaction ID exists."""
    plugin = ChargifySourcePlugin()
    payload = {
        "payload[subscription][customer][id]": "42",
        "payload[subscription][id]": "17",
        "payload[subscription][state]": "active",
        "payload[subscription][currency]": "USD",
        "payload[subscription][total_revenue_in_cents]": "10000",
    }
    with patch.object(
        plugin, "get_customer_data", return_value={"email": "a@example.com"}
    ):
        event = plugin._parse_signup_success(payload)
    event["content_hash"] = "signed-body-one"
    assert record_event(workspace, event, event["customer_data"]) is not None
    assert record_event(workspace, event, event["customer_data"]) is None
    # A different authenticated occurrence must still be recorded.
    event["content_hash"] = "signed-body-two"
    assert record_event(workspace, event, event["customer_data"]) is not None
    assert TransactionEvent.objects.filter(workspace=workspace).count() == 2


def test_digest_retries_only_failed_destination(workspace: Workspace) -> None:
    """Success at Slack cannot permanently acknowledge a Teams failure."""
    _row(workspace)
    good = MagicMock(spec=SlackDestinationPlugin)
    bad = MagicMock(spec=SlackDestinationPlugin)
    bad.send.side_effect = RuntimeError("temporarily unavailable")
    registry = MagicMock()
    registry.get.side_effect = lambda _kind, name: good if name == "slack" else bad
    now = MONDAY + timedelta(days=7)
    with (
        patch("plugins.PluginRegistry.instance", return_value=registry),
        patch(
            "webhooks.services.destination_credentials.collect_destinations",
            return_value=[("slack", {}), ("teams", {})],
        ),
    ):
        assert send_digest(workspace, "week", now=now) == "failed"
        stored = SalesSnapshot.objects.get(workspace=workspace, period="week")
        assert stored.digest_sent_at is None
        assert stored.digest_delivered_to == ["slack"]
        bad.send.side_effect = None
        assert send_digest(workspace, "week", now=now + timedelta(hours=1)) == "sent"
        assert (
            send_digest(workspace, "week", now=now + timedelta(hours=2))
            == "already_sent"
        )
    assert good.send.call_count == 1
    assert bad.send.call_count == 2
    stored.refresh_from_db()
    assert stored.digest_delivered_to == ["slack", "teams"]


def test_false_delivery_acknowledgement_remains_pending(workspace: Workspace) -> None:
    """A false send result is a failure, not a successful receipt."""
    _row(workspace)
    plugin = MagicMock(spec=SlackDestinationPlugin)
    plugin.send.return_value = False
    registry = MagicMock()
    registry.get.return_value = plugin
    with (
        patch("plugins.PluginRegistry.instance", return_value=registry),
        patch(
            "webhooks.services.destination_credentials.collect_destinations",
            return_value=[("slack", {})],
        ),
    ):
        assert (
            send_digest(workspace, "week", now=MONDAY + timedelta(days=7)) == "failed"
        )
    stored = SalesSnapshot.objects.get(workspace=workspace, period="week")
    assert stored.digest_delivered_to == []
    assert stored.digest_sent_at is None


@pytest.mark.parametrize("sale_first", [True, False])
def test_refunds_reconcile_industries_and_totals(
    workspace: Workspace, sale_first: bool
) -> None:
    """Refund-only customers and refund-before-sale ordering preserve industry net."""
    _row(workspace, customer_ref="prior", occurred_at=MONDAY - timedelta(days=7))
    _row(workspace, "refund", customer_ref="prior", amount=Decimal("50"))
    _row(workspace, customer_ref="other", occurred_at=MONDAY + timedelta(days=1))
    if not sale_first:
        _row(
            workspace,
            "refund",
            customer_ref="other",
            amount=Decimal("10"),
            occurred_at=MONDAY + timedelta(hours=1),
        )
    snapshot = compute_snapshot(workspace, WEEK, now=MONDAY + timedelta(days=2))
    industry_net = sum(
        Decimal(i["revenue"].get("USD", "0")) for i in snapshot["industries"]
    )
    assert industry_net == Decimal(snapshot["totals"]["revenue"]["USD"])
    assert snapshot["totals"]["customers"] == 1
    assert snapshot["industries"][0]["customers"] == 1


def test_refund_only_industry_has_no_division_by_zero(workspace: Workspace) -> None:
    """A refund-only period has a negative industry balance but no active buyers."""
    _row(workspace, "refund", amount=Decimal("50"))
    snapshot = compute_snapshot(workspace, WEEK, now=MONDAY + timedelta(days=1))
    assert snapshot["totals"]["customers"] == 0
    assert snapshot["industries"][0]["revenue"] == {"USD": "-50.000"}
    assert Decimal(snapshot["industries"][0]["avg_per_customer"]) == 0
    assert sum(snapshot["industries"][0]["bands"]) == 0


def test_delayed_sale_keeps_provider_time(workspace: Workspace) -> None:
    """Delivery on Monday does not move Sunday's payment into a new week."""
    occurred = MONDAY - timedelta(days=1)
    event = {
        "type": "payment_success",
        "provider": "shopify",
        "customer_id": "c1",
        "external_id": "order-1",
        "created_at": occurred.isoformat(),
        "amount": 100,
        "currency": "USD",
        "metadata": {"order_id": "order-1"},
    }
    with patch("core.services.sales_ledger.timezone.now", return_value=MONDAY):
        saved = record_event(workspace, event)
    assert saved is not None and saved.occurred_at == occurred
    assert compute_snapshot(workspace, WEEK)["totals"]["customers"] == 0
    assert compute_snapshot(workspace, WEEK.previous())["totals"]["customers"] == 1


@pytest.mark.parametrize("value", [None, "invalid", "2026-11-16T12:00:00", True, 1e100])
def test_invalid_occurrence_time_falls_back_safely(value: Any) -> None:
    """Missing, malformed and naive timestamps cannot crash delivery or guess a zone."""
    with patch("core.services.sales_ledger.timezone.now", return_value=MONDAY):
        assert event_occurred_at({"occurred_at": value}) == MONDAY


def test_stripe_uses_event_time_not_invoice_creation(
    request_factory: RequestFactory,
) -> None:
    """The signed outer Stripe event dates a retry, not the invoice's created field."""
    event = SimpleNamespace(
        type="invoice.payment_succeeded",
        id="evt_1",
        created=int(MONDAY.timestamp()),
        request=None,
        data=SimpleNamespace(
            object={
                "id": "in_1",
                "customer": "cus_1",
                "created": int((MONDAY - timedelta(days=5)).timestamp()),
                "currency": "usd",
            }
        ),
    )
    plugin = StripeSourcePlugin()
    with (
        patch.object(plugin, "_construct_verified_event", return_value=event),
        patch.object(plugin, "_handle_stripe_billing", return_value=Decimal("100")),
    ):
        parsed = plugin.parse_webhook(
            request_factory.post("/", data="{}", content_type="application/json")
        )
    assert parsed is not None
    assert event_occurred_at(parsed) == MONDAY


def test_partial_raw_history_cannot_replace_archive(workspace: Workspace) -> None:
    """Refreshing an old report preserves the complete pre-purge aggregates."""
    _row(workspace)
    stored = store_snapshot(workspace, WEEK, MONDAY + timedelta(days=7))
    TransactionEvent.objects.filter(workspace=workspace).delete()
    refreshed = store_snapshot(workspace, WEEK, MONDAY + timedelta(days=50))
    assert refreshed.pk == stored.pk
    assert refreshed.data["totals"]["revenue"] == {"USD": "100.000"}


def test_out_of_order_arrival_does_not_reverse_recovery(workspace: Workspace) -> None:
    """A delayed failure still precedes its already-delivered payment in event time."""
    common = {
        "provider": "stripe",
        "customer_id": "cus_1",
        "external_id": "in_1",
        "currency": "USD",
        "amount": 100,
    }
    sale = {
        **common,
        "type": "payment_success",
        "occurred_at": int((MONDAY + timedelta(days=1)).timestamp()),
    }
    failure = {
        **common,
        "type": "payment_failure",
        "occurred_at": int(MONDAY.timestamp()),
        "metadata": {"attempt_count": 1},
    }
    with patch(
        "core.services.sales_ledger.timezone.now",
        return_value=MONDAY + timedelta(days=2),
    ):
        record_event(workspace, sale)
        saved = record_event(workspace, failure)
    assert saved is not None and saved.occurred_at == MONDAY
    recovery = compute_snapshot(workspace, WEEK, now=MONDAY + timedelta(days=2))[
        "recovery"
    ]
    assert recovery["recovered"]["count"] == 1
    assert recovery["at_risk"]["count"] == 0
    # The later payment also must not be treated as history before this failure.
    assert not failure["metadata"].get("ledger_history", {}).get("prior_sales")

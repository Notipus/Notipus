"""Tests for recording billing events into the sales ledger."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from core.models import TransactionEvent, Workspace
from core.services.sales_ledger import (
    classify_kind,
    classify_segment,
    purge_expired,
    record_event,
)
from django.db import connection
from django.utils import timezone
from plugins.sources.chargify import ChargifySourcePlugin


@pytest.fixture
def workspace() -> Workspace:
    """A plain workspace.

    Returns:
        The workspace.
    """
    return Workspace.objects.create(name="Ledger Co")


def _stripe_invoice(
    event_type: str = "payment_success",
    *,
    invoice: str = "in_1",
    billing_reason: str = "subscription_create",
    amount: float = 99.0,
    **metadata: Any,
) -> dict[str, Any]:
    """Build a parsed Stripe invoice event.

    Args:
        event_type: Normalized event type.
        invoice: Invoice id (the event's external_id).
        billing_reason: Stripe billing_reason.
        amount: Amount in major units.
        **metadata: Extra metadata.

    Returns:
        Parsed event data.
    """
    return {
        "type": event_type,
        "provider": "stripe",
        "customer_id": "cus_1",
        "external_id": invoice,
        "amount": amount,
        "currency": "usd",
        "metadata": {"billing_reason": billing_reason, **metadata},
    }


class TestClassification:
    """New vs existing is only set from signals the provider sent."""

    @pytest.mark.parametrize(
        ("event", "customer", "expected"),
        [
            (_stripe_invoice(), {}, "new"),
            (_stripe_invoice(billing_reason="subscription_cycle"), {}, "existing"),
            (_stripe_invoice(billing_reason="subscription_update"), {}, "existing"),
            (_stripe_invoice(billing_reason="manual"), {}, "unknown"),
            (
                _stripe_invoice(
                    billing_reason="subscription_cycle", is_trial_conversion=True
                ),
                {},
                "new",
            ),
            (
                {"type": "payment_success", "metadata": {"is_renewal": True}},
                {},
                "existing",
            ),
            (
                {
                    "type": "subscription_created",
                    "metadata": {"is_signup_payment": True},
                },
                {},
                "new",
            ),
            ({"type": "payment_success", "metadata": {}}, {"orders_count": 1}, "new"),
            (
                {"type": "payment_success", "metadata": {}},
                {"orders_count": 4},
                "existing",
            ),
            ({"type": "payment_success", "metadata": {}}, {}, "unknown"),
            ({"type": "subscription_renewed", "metadata": {}}, {}, "existing"),
        ],
    )
    def test_segment(
        self, event: dict[str, Any], customer: dict[str, Any], expected: str
    ) -> None:
        """Each provider signal maps to the documented segment.

        Args:
            event: Parsed event.
            customer: Customer data.
            expected: Expected segment.
        """
        kind = classify_kind(event) or TransactionEvent.KIND_SALE
        assert classify_segment(event, customer, kind) == expected

    def test_cancellation_is_always_existing(self) -> None:
        """Only an existing customer can cancel."""
        event = {"type": "subscription_canceled", "metadata": {}}
        assert classify_kind(event) == "cancellation"
        assert classify_segment(event, {}, "cancellation") == "existing"

    @pytest.mark.parametrize(
        ("event_type", "kind"),
        [
            ("payment_success", "sale"),
            ("invoice_paid", "sale"),
            ("payment_failure", "failure"),
            ("payment_action_required", "failure"),
            ("refund_issued", "refund"),
            ("subscription_deleted", "cancellation"),
            ("trial_started", "trial_started"),
            ("order_fulfilled", None),
            ("subscription_updated", None),
        ],
    )
    def test_kind(self, event_type: str, kind: str | None) -> None:
        """Only money-moving and relationship-ending events are recorded.

        Args:
            event_type: Normalized event type.
            kind: Expected ledger kind.
        """
        assert classify_kind({"type": event_type, "metadata": {}}) == kind


@pytest.mark.django_db
class TestRecordEvent:
    """record_event stores, deduplicates and protects ledger rows."""

    def test_records_sale_with_company_domain(self, workspace: Workspace) -> None:
        """A paid invoice becomes one sale row tagged with the company domain.

        Args:
            workspace: Workspace fixture.
        """
        row = record_event(
            workspace,
            _stripe_invoice(),
            {"email": "Jane@Acme-Widgets.example", "first_name": "Jane"},
        )
        assert row is not None
        row.refresh_from_db()
        assert row.kind == "sale"
        assert row.segment == "new"
        assert row.amount == Decimal("99.000")
        assert row.currency == "USD"
        assert row.company_domain == "acme-widgets.example"
        assert not row.is_consumer
        assert row.details["email"] == "jane@acme-widgets.example"
        assert row.details["name"] == "Jane"

    def test_details_are_encrypted_at_rest(self, workspace: Workspace) -> None:
        """Customer details never reach the database as plaintext.

        Args:
            workspace: Workspace fixture.
        """
        row = record_event(workspace, _stripe_invoice(), {"email": "a@corp.example"})
        assert row is not None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT details, customer_ref FROM core_transactionevent WHERE id = %s",
                [row.pk],
            )
            details, customer_ref = cursor.fetchone()
        assert details.startswith("pqc1:")
        assert "corp.example" not in details
        assert "cus_1" not in customer_ref

    def test_paired_stripe_events_count_once(self, workspace: Workspace) -> None:
        """invoice.payment_succeeded and invoice.paid are one sale.

        Args:
            workspace: Workspace fixture.
        """
        record_event(workspace, _stripe_invoice("payment_success"), {})
        duplicate = record_event(workspace, _stripe_invoice("invoice_paid"), {})
        assert duplicate is None
        assert TransactionEvent.objects.filter(kind="sale").count() == 1

    def test_each_failed_attempt_is_kept(self, workspace: Workspace) -> None:
        """Retries of the same invoice are separate failures on one chain.

        Args:
            workspace: Workspace fixture.
        """
        for attempt in (1, 2):
            record_event(
                workspace,
                _stripe_invoice("payment_failure", attempt_count=attempt),
                {},
            )
        rows = TransactionEvent.objects.filter(kind="failure")
        assert rows.count() == 2
        assert len({row.charge_ref for row in rows}) == 1

    def test_free_mailbox_is_a_consumer(self, workspace: Workspace) -> None:
        """A gmail customer is a person, not a company.

        Args:
            workspace: Workspace fixture.
        """
        row = record_event(workspace, _stripe_invoice(), {"email": "x@gmail.com"})
        assert row is not None
        assert row.is_consumer
        assert row.company_domain == ""

    def test_skips_zero_amount_and_irrelevant_events(
        self, workspace: Workspace
    ) -> None:
        """$0 invoices and non-sales events are not recorded.

        Args:
            workspace: Workspace fixture.
        """
        assert record_event(workspace, _stripe_invoice(amount=0), {}) is None
        assert (
            record_event(workspace, {"type": "order_fulfilled", "customer_id": "c"}, {})
            is None
        )
        assert record_event(None, _stripe_invoice(), {}) is None
        assert not TransactionEvent.objects.exists()

    def test_never_raises(self, workspace: Workspace) -> None:
        """A storage failure must not break webhook processing.

        Args:
            workspace: Workspace fixture.
        """
        with patch.object(TransactionEvent, "save", side_effect=RuntimeError("db")):
            assert record_event(workspace, _stripe_invoice(), {}) is None

    def test_purge_expired(self, workspace: Workspace) -> None:
        """Rows older than the retention window are deleted.

        Args:
            workspace: Workspace fixture.
        """
        old = record_event(workspace, _stripe_invoice(invoice="in_old"), {})
        fresh = record_event(workspace, _stripe_invoice(invoice="in_new"), {})
        assert old is not None and fresh is not None
        TransactionEvent.objects.filter(pk=old.pk).update(
            occurred_at=timezone.now() - timedelta(days=36)
        )
        assert purge_expired() == 1
        assert list(TransactionEvent.objects.values_list("pk", flat=True)) == [fresh.pk]

    def test_router_records_before_dispatch(self, workspace: Workspace) -> None:
        """The webhook router writes the ledger for every dispatched event.

        Args:
            workspace: Workspace fixture.
        """
        from webhooks import webhook_router

        with patch.object(webhook_router, "_process_immediately") as process:
            process.return_value = None
            webhook_router._dispatch_event(
                {**_stripe_invoice(), "provider": "chargify"},
                {"email": "a@corp.example"},
                "chargify",
                workspace,
                str(workspace.uuid),
            )
        assert TransactionEvent.objects.filter(workspace=workspace).count() == 1


class TestChargifyRenewalFlag:
    """Chargify renewals keep the fact that they are renewals."""

    def test_renewal_events_are_marked(self) -> None:
        """renewal_success is flagged; plain payment_success is not."""
        renewal = ChargifySourcePlugin._mark_renewal(
            "renewal_success", {"metadata": {}}
        )
        payment = ChargifySourcePlugin._mark_renewal(
            "payment_success", {"metadata": {}}
        )
        assert renewal["metadata"]["is_renewal"] is True
        assert "is_renewal" not in payment["metadata"]


@pytest.mark.django_db
class TestShopifyPrivacyRequests:
    """Shopify's mandatory privacy webhooks cover the sales ledger."""

    @staticmethod
    def _shopify_sale(workspace: Workspace, order: str, customer: str, email: str):
        """Record a Shopify sale for a customer.

        Args:
            workspace: Workspace fixture.
            order: Order id.
            customer: Shopify customer id.
            email: Customer email.

        Returns:
            The stored row.
        """
        return record_event(
            workspace,
            {
                "type": "payment_success",
                "provider": "shopify",
                "customer_id": customer,
                "external_id": order,
                "amount": 40,
                "currency": "USD",
                "metadata": {},
            },
            {"email": email, "orders_count": 1},
        )

    def test_data_request_and_customer_redact(self, workspace: Workspace) -> None:
        """A customer's rows are exported, then erased; others survive.

        Args:
            workspace: Workspace fixture.
        """
        from webhooks.services.shopify_compliance import (
            collect_customer_data,
            redact_customer,
        )

        self._shopify_sale(workspace, "1", "111", "ann@corp.example")
        self._shopify_sale(workspace, "2", "222", "bob@corp.example")

        exported = collect_customer_data(workspace, "111", None)["sales_ledger"]
        assert [row["details"]["email"] for row in exported] == ["ann@corp.example"]

        result = redact_customer(workspace, None, "ANN@corp.example")
        assert result["ledger"] == 1
        remaining = TransactionEvent.objects.get()
        assert remaining.details["email"] == "bob@corp.example"

    def test_shop_redact_keeps_other_providers(self, workspace: Workspace) -> None:
        """shop/redact erases Shopify rows only.

        Args:
            workspace: Workspace fixture.
        """
        from webhooks.services.shopify_compliance import redact_shop

        self._shopify_sale(workspace, "1", "111", "ann@corp.example")
        record_event(workspace, _stripe_invoice(), {"email": "x@corp.example"})

        assert redact_shop(workspace)["ledger"] == 1
        assert list(TransactionEvent.objects.values_list("provider", flat=True)) == [
            "stripe"
        ]

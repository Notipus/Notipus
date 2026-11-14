"""Regression tests for Notipus's own Stripe billing hardening.

Covers: clover-shaped invoices and subscriptions, payment-failure
re-sync, primary-subscription selection during sync, Stripe errors
during sync, and webhook redelivery side effects.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import stripe
from core.models import Workspace
from webhooks.services.billing import BillingService
from webhooks.utils.stripe_fields import (
    invoice_subscription_id,
    subscription_period_end,
)

CUSTOMER_ID = "cus_hardening"


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create a paid workspace with a recorded primary subscription.

    Args:
        db: pytest-django database fixture.

    Returns:
        Persisted Workspace linked to a Stripe customer and subscription.
    """
    return Workspace.objects.create(
        name="Hardening Workspace",
        stripe_customer_id=CUSTOMER_ID,
        stripe_subscription_id="sub_primary",
        subscription_plan="pro",
        subscription_status="active",
    )


@pytest.fixture
def stripe_subs() -> Iterator[MagicMock]:
    """Patch the billing service's StripeAPI.

    Yields:
        The mocked ``get_customer_subscriptions`` method; set its
        ``return_value`` to the subscriptions Stripe should report.
    """
    with patch("webhooks.services.billing.StripeAPI") as api_cls:
        api_cls.return_value.get_customer_subscriptions.return_value = []
        yield api_cls.return_value.get_customer_subscriptions


def _clover_invoice(**overrides: Any) -> dict[str, Any]:
    """Build an invoice shaped like the 2025-12-15.clover API version.

    Args:
        **overrides: Fields to override on the invoice.

    Returns:
        Invoice dict with the subscription only under ``parent``.
    """
    invoice: dict[str, Any] = {
        "id": "in_clover",
        "customer": CUSTOMER_ID,
        "amount_paid": 2900,
        "amount_due": 2900,
        "currency": "usd",
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": "sub_primary"},
        },
    }
    invoice.update(overrides)
    return invoice


def _sub(sub_id: str, status: str, product: str = "Pro") -> dict[str, Any]:
    """Build a subscription dict as returned by get_customer_subscriptions.

    Args:
        sub_id: Subscription id.
        status: Stripe subscription status.
        product: Product name on the first item.

    Returns:
        Subscription dict.
    """
    return {"id": sub_id, "status": status, "items": [{"product_name": product}]}


class TestCloverFieldExtraction:
    """Invoice subscription id and period end on clover payloads."""

    def test_invoice_subscription_id_reads_parent(self) -> None:
        """The clover location parent.subscription_details is read."""
        assert invoice_subscription_id(_clover_invoice()) == "sub_primary"

    def test_invoice_subscription_id_legacy_and_expanded(self) -> None:
        """The legacy field still works, as id or expanded object."""
        assert invoice_subscription_id({"subscription": "sub_a"}) == "sub_a"
        assert invoice_subscription_id({"subscription": {"id": "sub_b"}}) == "sub_b"
        assert invoice_subscription_id({"customer": CUSTOMER_ID}) is None

    def test_invoice_subscription_id_on_stripe_object(self) -> None:
        """Stripe SDK objects (not dicts) are read too."""
        invoice = stripe.Invoice.construct_from(_clover_invoice(), "sk_test")
        assert invoice_subscription_id(invoice) == "sub_primary"

    def test_period_end_reads_item_level_field(self) -> None:
        """On clover, current_period_end lives on subscription items."""
        payload = {"id": "sub_x", "items": {"data": [{"current_period_end": 1800}]}}
        assert subscription_period_end(payload) == 1800
        assert BillingService._extract_billing_anchor(payload) == 1800
        sdk_sub = stripe.Subscription.construct_from(payload, "sk_test")
        assert subscription_period_end(sdk_sub) == 1800

    def test_period_end_prefers_top_level_field(self) -> None:
        """Legacy payloads keep using the top-level field."""
        payload = {
            "current_period_end": 1700,
            "items": {"data": [{"current_period_end": 1800}]},
        }
        assert subscription_period_end(payload) == 1700

    def test_customer_subscriptions_carry_item_period_end(self) -> None:
        """get_customer_subscriptions surfaces the clover period end."""
        from core.services.stripe import StripeAPI

        sdk_sub = stripe.Subscription.construct_from(
            {
                "id": "sub_x",
                "status": "active",
                "items": {
                    "object": "list",
                    "data": [{"id": "si_1", "current_period_end": 1900}],
                },
            },
            "sk_test",
        )
        listing = MagicMock()
        listing.auto_paging_iter.return_value = [sdk_sub]
        with patch("core.services.stripe.stripe.Subscription.list") as list_mock:
            list_mock.return_value = listing
            subs = StripeAPI(api_key="sk_test").get_customer_subscriptions(CUSTOMER_ID)

        assert subs[0]["current_period_end"] == 1900


class TestCloverInvoiceHandlers:
    """Invoice handlers act on clover-shaped invoices."""

    def test_paid_clover_invoice_activates(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """A paid clover invoice for the primary subscription is applied."""
        Workspace.objects.filter(id=workspace.id).update(
            subscription_status="past_due", payment_method_added=False
        )
        stripe_subs.return_value = [_sub("sub_primary", "active")]

        BillingService.handle_invoice_paid(_clover_invoice())

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"
        assert workspace.payment_method_added is True

    def test_failed_clover_invoice_resyncs_to_past_due(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """A current payment failure lands as past_due via Stripe's status."""
        stripe_subs.return_value = [_sub("sub_primary", "past_due")]

        BillingService.handle_payment_failed(_clover_invoice())

        workspace.refresh_from_db()
        assert workspace.subscription_status == "past_due"
        stripe_subs.assert_called()

    def test_late_failure_does_not_suspend_paid_workspace(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """A retried failure after the customer paid leaves it active."""
        stripe_subs.return_value = [_sub("sub_primary", "active")]

        BillingService.handle_payment_failed(_clover_invoice())

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"

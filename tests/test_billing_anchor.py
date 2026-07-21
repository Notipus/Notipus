"""Tests for billing_cycle_anchor on paid invoices.

Regression coverage for the invoice-paid handlers anchoring the billing
cycle on the wrong timestamp. For a renewal ``invoice.paid``, the
invoice's top-level ``period_end`` is the end of the just-billed period
(≈ now), not the NEXT renewal. Per ``_extract_billing_anchor``'s
contract, the anchor must track the subscription's
``current_period_end`` (the next renewal), which the post-write sync
supplies. These tests prove the stored anchor ends up on the next
renewal, not the billed-period end.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.models import Workspace
from webhooks.services.billing import BillingService

CUSTOMER_ID = "cus_anchor123"
SUBSCRIPTION_ID = "sub_primary"

# The just-billed period's end, sent as the invoice's top-level
# period_end. This is roughly "now" for a renewal and must NOT become
# the billing anchor.
BILLED_PERIOD_END = 1704067200
# The subscription's current_period_end: the NEXT renewal, ~30 days
# after the billed period ended. This is the correct anchor.
NEXT_RENEWAL = 1706745600


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create a workspace whose billing rides on SUBSCRIPTION_ID.

    The stored anchor starts on the just-billed period so a test can
    observe whether the handler leaves it there (bug) or advances it to
    the next renewal (fixed).

    Args:
        db: pytest-django database fixture.

    Returns:
        Persisted Workspace linked to the Stripe customer and subscription.
    """
    return Workspace.objects.create(
        name="Anchor Workspace",
        stripe_customer_id=CUSTOMER_ID,
        stripe_subscription_id=SUBSCRIPTION_ID,
        subscription_status="active",
        subscription_plan="pro",
        billing_cycle_anchor=BILLED_PERIOD_END,
    )


def _mock_stripe_api(subscriptions: list[dict[str, Any]]) -> Any:
    """Patch billing.StripeAPI to return the given subscriptions.

    Args:
        subscriptions: Subscription dicts the fake API returns for
            ``get_customer_subscriptions``.

    Returns:
        Context manager patching webhooks.services.billing.StripeAPI.
    """
    mock_api = MagicMock()
    mock_api.get_customer_subscriptions.return_value = subscriptions
    return patch("webhooks.services.billing.StripeAPI", return_value=mock_api)


def _renewal_invoice() -> dict[str, Any]:
    """Build a paid renewal invoice for the workspace's subscription.

    Its top-level ``period_end`` is the just-billed period's end — the
    value the old code wrongly wrote as the anchor.

    Returns:
        Invoice payload as delivered by Stripe's invoice.paid webhook.
    """
    return {
        "customer": CUSTOMER_ID,
        "subscription": SUBSCRIPTION_ID,
        "amount_paid": 2900,
        "period_end": BILLED_PERIOD_END,
    }


def _active_subscription() -> dict[str, Any]:
    """Build the live subscription the sync reads back from Stripe.

    Its ``current_period_end`` is the next renewal — the correct anchor.

    Returns:
        Subscription dict as returned by ``get_customer_subscriptions``.
    """
    return {
        "id": SUBSCRIPTION_ID,
        "status": "active",
        "current_period_end": NEXT_RENEWAL,
    }


class TestInvoicePaidBillingAnchor:
    """The paid-invoice handlers anchor on the subscription's next renewal."""

    @pytest.mark.django_db
    def test_invoice_paid_anchors_on_subscription_next_renewal(
        self, workspace: Workspace
    ) -> None:
        """After a renewal invoice.paid, the stored anchor is the
        subscription's current_period_end (next renewal), not the
        invoice's just-billed period_end."""
        with _mock_stripe_api([_active_subscription()]):
            BillingService.handle_invoice_paid(_renewal_invoice())

        workspace.refresh_from_db()
        assert workspace.billing_cycle_anchor == NEXT_RENEWAL
        assert workspace.billing_cycle_anchor != BILLED_PERIOD_END

    @pytest.mark.django_db
    def test_payment_success_anchors_on_subscription_next_renewal(
        self, workspace: Workspace
    ) -> None:
        """handle_payment_success shares the same handler, so it too
        advances the anchor to the next renewal rather than the billed
        period end."""
        with _mock_stripe_api([_active_subscription()]):
            BillingService.handle_payment_success(_renewal_invoice())

        workspace.refresh_from_db()
        assert workspace.billing_cycle_anchor == NEXT_RENEWAL

    @pytest.mark.django_db
    def test_invoice_paid_does_not_regress_a_future_anchor(
        self, workspace: Workspace
    ) -> None:
        """A subscription handler that already advanced the anchor to the
        next renewal must not be regressed by a later invoice.paid whose
        period_end is the just-billed (earlier) period."""
        Workspace.objects.filter(id=workspace.id).update(
            billing_cycle_anchor=NEXT_RENEWAL
        )

        with _mock_stripe_api([_active_subscription()]):
            BillingService.handle_invoice_paid(_renewal_invoice())

        workspace.refresh_from_db()
        assert workspace.billing_cycle_anchor == NEXT_RENEWAL

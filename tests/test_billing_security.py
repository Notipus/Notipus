"""Security regression tests for the billing implementation.

Covers two invariants:

1. BillingService dispatch is exclusive to the global billing endpoint.
   Tenant notification endpoints (/webhook/customer/<uuid>/stripe/)
   validate against a *tenant-supplied* webhook secret, so a tenant can
   sign arbitrary events from their own Stripe account. If those events
   reached BillingService, forged checkout metadata could set any
   workspace to any plan (privilege escalation / cross-tenant takeover).

2. Workspaces without an entitled subscription (suspended, past_due,
   cancelled, expired trial) do not receive webhook service.
"""

import json
from datetime import timedelta
from typing import Any
from unittest.mock import Mock, patch

import pytest
from core.models import Integration, Workspace
from django.test import Client
from django.utils import timezone
from plugins.sources.stripe import StripeSourcePlugin


def _mock_stripe_event(event_type: str, obj: dict[str, Any]) -> Mock:
    """Build a mock Stripe event envelope for construct_event patching."""
    event = Mock()
    event.id = "evt_test_1"
    event.type = event_type
    event.data.object = obj
    event.data.previous_attributes = None
    event.request = None
    return event


class TestBillingDispatchGating:
    """BillingService must be unreachable from tenant-controlled plugins."""

    def test_default_plugin_never_dispatches_billing(self) -> None:
        """A plugin without the billing flag must not touch BillingService."""
        plugin = StripeSourcePlugin(webhook_secret="whsec_tenant")

        checkout_session = {
            "id": "cs_test_1",
            "customer": "cus_attacker",
            "subscription": "sub_attacker",
            "amount_total": 0,
            "metadata": {"workspace_id": "1", "plan_name": "Enterprise Plan"},
        }
        event = _mock_stripe_event("checkout.session.completed", checkout_session)

        request = Mock()
        request.content_type = "application/json"
        request.POST = None
        request.headers = {"Stripe-Signature": "sig"}
        request.body = b"{}"

        with (
            patch(
                "plugins.sources.stripe.stripe.Webhook.construct_event",
                return_value=event,
            ),
            patch("webhooks.services.billing.BillingService") as mock_billing,
        ):
            result = plugin.parse_webhook(request)

        # The event still parses (notifications work) but billing is untouched
        assert result is not None
        assert result["type"] == "checkout_completed"
        assert not mock_billing.mock_calls

    def test_billing_plugin_dispatches_billing(self) -> None:
        """The billing endpoint's plugin (flag=True) does dispatch."""
        plugin = StripeSourcePlugin(
            webhook_secret="whsec_notipus", process_billing_events=True
        )

        checkout_session = {
            "id": "cs_test_1",
            "customer": "cus_real",
            "amount_total": 2900,
            "metadata": {"workspace_id": "1", "plan_name": "basic"},
        }
        event = _mock_stripe_event("checkout.session.completed", checkout_session)

        request = Mock()
        request.content_type = "application/json"
        request.POST = None
        request.headers = {"Stripe-Signature": "sig"}
        request.body = b"{}"

        with (
            patch(
                "plugins.sources.stripe.stripe.Webhook.construct_event",
                return_value=event,
            ),
            patch(
                "webhooks.services.billing.BillingService.handle_checkout_completed"
            ) as mock_handler,
        ):
            plugin.parse_webhook(request)

        mock_handler.assert_called_once()

    @pytest.mark.django_db
    def test_forged_tenant_webhook_cannot_upgrade_workspace(self) -> None:
        """End-to-end: forged checkout metadata via a tenant endpoint is inert.

        The attacker controls their own Stripe account (and thus produces
        correctly-signed events) and their own workspace's integration.
        The victim workspace must remain untouched.
        """
        victim = Workspace.objects.create(
            name="Victim Workspace",
            subscription_plan="free",
            subscription_status="active",
        )
        attacker = Workspace.objects.create(
            name="Attacker Workspace",
            subscription_plan="free",
            subscription_status="active",
        )
        Integration.objects.create(
            workspace=attacker,
            integration_type="stripe_customer",
            webhook_secret="whsec_attacker_controlled",
            is_active=True,
        )

        checkout_session = {
            "id": "cs_forged",
            "customer": "cus_attacker_account",
            "subscription": "sub_attacker_account",
            "amount_total": 0,
            "metadata": {
                "workspace_id": str(victim.id),
                "plan_name": "Enterprise Plan",
            },
        }
        event = _mock_stripe_event("checkout.session.completed", checkout_session)

        client = Client()
        with (
            # Signature validation passes: the attacker knows their secret
            patch(
                "plugins.sources.stripe.stripe.Webhook.construct_event",
                return_value=event,
            ),
        ):
            response = client.post(
                f"/webhook/customer/{attacker.uuid}/stripe/",
                data=json.dumps({"type": "checkout.session.completed"}),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=valid_for_attacker_secret",
            )

        assert response.status_code == 200

        victim.refresh_from_db()
        assert victim.subscription_plan == "free"
        assert victim.subscription_status == "active"
        assert victim.stripe_customer_id == ""
        assert victim.payment_method_added is False


@pytest.mark.django_db
class TestWorkspaceAccessEnforcement:
    """Non-entitled workspaces must not receive webhook service."""

    def _make_workspace(self, **kwargs: Any) -> Workspace:
        workspace = Workspace.objects.create(name="Access Test Workspace", **kwargs)
        Integration.objects.create(
            workspace=workspace,
            integration_type="stripe_customer",
            webhook_secret="whsec_test",
            is_active=True,
        )
        return workspace

    def _post_webhook(self, workspace: Workspace) -> Any:
        event = _mock_stripe_event(
            "invoice.paid",
            {
                "id": "in_1",
                "customer": "cus_end_customer",
                "amount_paid": 1000,
                "currency": "usd",
                "status": "paid",
                "created": 1700000000,
            },
        )
        client = Client()
        with patch(
            "plugins.sources.stripe.stripe.Webhook.construct_event",
            return_value=event,
        ):
            return client.post(
                f"/webhook/customer/{workspace.uuid}/stripe/",
                data=json.dumps({"type": "invoice.paid"}),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=sig",
            )

    def test_active_workspace_is_served(self) -> None:
        workspace = self._make_workspace(
            subscription_plan="basic", subscription_status="active"
        )
        assert self._post_webhook(workspace).status_code == 200

    def test_live_trial_is_served(self) -> None:
        workspace = self._make_workspace(
            subscription_plan="basic",
            subscription_status="trial",
            trial_end_date=timezone.now() + timedelta(days=7),
        )
        assert self._post_webhook(workspace).status_code == 200

    def test_expired_trial_is_rejected(self) -> None:
        workspace = self._make_workspace(
            subscription_plan="enterprise",
            subscription_status="trial",
            trial_end_date=timezone.now() - timedelta(days=1),
        )
        response = self._post_webhook(workspace)
        assert response.status_code == 403
        assert response.json()["error"] == "SubscriptionInactive"

    def test_cancelled_workspace_is_rejected(self) -> None:
        workspace = self._make_workspace(
            subscription_plan="pro", subscription_status="cancelled"
        )
        assert self._post_webhook(workspace).status_code == 403

    def test_past_due_workspace_is_rejected(self) -> None:
        workspace = self._make_workspace(
            subscription_plan="pro", subscription_status="past_due"
        )
        assert self._post_webhook(workspace).status_code == 403

    def test_suspended_workspace_is_rejected(self) -> None:
        workspace = self._make_workspace(
            subscription_plan="pro", subscription_status="suspended"
        )
        assert self._post_webhook(workspace).status_code == 403


@pytest.mark.django_db
class TestHasActiveAccess:
    """Unit tests for the Workspace.has_active_access property."""

    def test_active_has_access(self) -> None:
        workspace = Workspace.objects.create(
            name="WS", subscription_status="active", subscription_plan="basic"
        )
        assert workspace.has_active_access is True

    def test_live_trial_has_access(self) -> None:
        workspace = Workspace.objects.create(
            name="WS",
            subscription_status="trial",
            subscription_plan="basic",
            trial_end_date=timezone.now() + timedelta(days=1),
        )
        assert workspace.has_active_access is True

    def test_expired_trial_has_no_access(self) -> None:
        workspace = Workspace.objects.create(
            name="WS",
            subscription_status="trial",
            subscription_plan="basic",
            trial_end_date=timezone.now() - timedelta(seconds=1),
        )
        assert workspace.has_active_access is False

    @pytest.mark.parametrize("status", ["suspended", "past_due", "cancelled"])
    def test_inactive_statuses_have_no_access(self, status: str) -> None:
        workspace = Workspace.objects.create(
            name="WS", subscription_status=status, subscription_plan="basic"
        )
        assert workspace.has_active_access is False

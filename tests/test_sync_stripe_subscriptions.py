"""Tests for Stripe subscription sync functionality.

Tests verify that the sync_workspace_from_stripe method and
sync_stripe_subscriptions management command work correctly.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from core.models import Workspace
from django.core.management import call_command
from webhooks.services.billing import STRIPE_STATUS_MAPPING, BillingService


@pytest.fixture
def workspace(db) -> Workspace:
    """Create a test workspace with Stripe customer ID."""
    return Workspace.objects.create(
        name="Test Workspace",
        stripe_customer_id="cus_test123",
        subscription_status="active",
        subscription_plan="free",
    )


@pytest.fixture
def mock_subscription_data() -> dict:
    """Create mock subscription data from Stripe API."""
    return {
        "id": "sub_test123",
        "status": "active",
        "current_period_start": 1700000000,
        "current_period_end": 1702592000,
        "items": [
            {
                "price_id": "price_test123",
                "product_name": "Notipus Pro Plan",
                "unit_amount": 9900,
                "currency": "usd",
                "quantity": 1,
            }
        ],
    }


class TestStripeStatusMapping:
    """Tests for STRIPE_STATUS_MAPPING constant."""

    def test_active_status_maps_to_active(self) -> None:
        """Verify active Stripe status maps to active."""
        assert STRIPE_STATUS_MAPPING["active"] == "active"

    def test_trialing_status_maps_to_trial(self) -> None:
        """Verify trialing Stripe status maps to trial."""
        assert STRIPE_STATUS_MAPPING["trialing"] == "trial"

    def test_canceled_status_maps_to_cancelled(self) -> None:
        """Verify canceled Stripe status maps to cancelled."""
        assert STRIPE_STATUS_MAPPING["canceled"] == "cancelled"

    def test_past_due_status_maps_to_past_due(self) -> None:
        """Verify past_due Stripe status maps to past_due."""
        assert STRIPE_STATUS_MAPPING["past_due"] == "past_due"


class TestGetActiveSubscription:
    """Tests for BillingService._get_active_subscription method."""

    def test_returns_active_subscription_first(self) -> None:
        """Verify active subscriptions are preferred over cancelled."""
        subscriptions = [
            {"status": "canceled", "id": "sub_1"},
            {"status": "active", "id": "sub_2"},
            {"status": "trialing", "id": "sub_3"},
        ]
        result = BillingService._get_active_subscription(subscriptions)
        assert result["id"] == "sub_2"

    def test_returns_trialing_subscription(self) -> None:
        """Verify trialing subscriptions are selected."""
        subscriptions = [
            {"status": "canceled", "id": "sub_1"},
            {"status": "trialing", "id": "sub_2"},
        ]
        result = BillingService._get_active_subscription(subscriptions)
        assert result["id"] == "sub_2"

    def test_returns_past_due_subscription(self) -> None:
        """Verify past_due subscriptions are selected over cancelled."""
        subscriptions = [
            {"status": "canceled", "id": "sub_1"},
            {"status": "past_due", "id": "sub_2"},
        ]
        result = BillingService._get_active_subscription(subscriptions)
        assert result["id"] == "sub_2"

    def test_returns_first_if_no_active(self) -> None:
        """Verify first subscription is returned if none are active."""
        subscriptions = [
            {"status": "canceled", "id": "sub_1"},
            {"status": "incomplete_expired", "id": "sub_2"},
        ]
        result = BillingService._get_active_subscription(subscriptions)
        assert result["id"] == "sub_1"


class TestExtractPlanNameFromSubscription:
    """Tests for BillingService._extract_plan_name_from_subscription method."""

    def test_extracts_plan_name_from_product_name(self) -> None:
        """Verify plan name is extracted and normalized."""
        subscription = {
            "items": [{"product_name": "Notipus Pro Plan"}],
        }
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result == "pro"

    def test_returns_none_for_empty_items(self) -> None:
        """Verify None is returned when items are empty."""
        subscription = {"items": []}
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result is None

    def test_returns_none_for_missing_items(self) -> None:
        """Verify None is returned when items key is missing."""
        subscription = {}
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result is None

    def test_returns_none_for_empty_product_name(self) -> None:
        """Verify None is returned when product_name is empty."""
        subscription = {"items": [{"product_name": ""}]}
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result is None

    def test_handles_basic_plan(self) -> None:
        """Verify basic plan name is extracted correctly."""
        subscription = {
            "items": [{"product_name": "Notipus Basic Plan"}],
        }
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result == "basic"

    def test_handles_enterprise_plan(self) -> None:
        """Verify enterprise plan name is extracted correctly."""
        subscription = {
            "items": [{"product_name": "Notipus Enterprise Plan"}],
        }
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result == "enterprise"

    def test_prefers_plan_name_from_metadata(self) -> None:
        """Verify plan_name from Product metadata is preferred over product_name."""
        subscription = {
            "items": [
                {
                    "product_name": "Pro Plan",
                    "plan_name": "pro",  # From Product metadata
                }
            ],
        }
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result == "pro"

    def test_uses_plan_name_even_without_product_name(self) -> None:
        """Verify plan_name works even if product_name is missing."""
        subscription = {
            "items": [
                {
                    "plan_name": "basic",  # From Product metadata
                }
            ],
        }
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result == "basic"

    def test_falls_back_to_product_name_when_no_plan_name(self) -> None:
        """Verify falls back to normalizing product_name when plan_name is absent."""
        subscription = {
            "items": [
                {
                    "product_name": "Pro Plan",
                    # No plan_name from metadata
                }
            ],
        }
        result = BillingService._extract_plan_name_from_subscription(subscription)
        assert result == "pro"


@pytest.mark.django_db
class TestSyncWorkspaceFromStripe:
    """Tests for BillingService.sync_workspace_from_stripe method."""

    def test_returns_false_for_missing_workspace(self) -> None:
        """Verify sync returns False when workspace not found."""
        result = BillingService.sync_workspace_from_stripe("cus_nonexistent")
        assert result is False

    def test_returns_true_for_no_subscriptions(self, workspace: Workspace) -> None:
        """Verify sync returns True when customer has no subscriptions."""
        with patch("webhooks.services.billing.StripeAPI") as mock_stripe_api_class:
            mock_api = MagicMock()
            mock_api.get_customer_subscriptions.return_value = []
            mock_stripe_api_class.return_value = mock_api

            result = BillingService.sync_workspace_from_stripe("cus_test123")
            assert result is True

    def test_updates_workspace_status(
        self, workspace: Workspace, mock_subscription_data: dict
    ) -> None:
        """Verify workspace status is updated from Stripe."""
        with patch("webhooks.services.billing.StripeAPI") as mock_stripe_api_class:
            mock_api = MagicMock()
            mock_api.get_customer_subscriptions.return_value = [mock_subscription_data]
            mock_stripe_api_class.return_value = mock_api

            result = BillingService.sync_workspace_from_stripe("cus_test123")

            assert result is True
            workspace.refresh_from_db()
            assert workspace.subscription_status == "active"
            assert workspace.subscription_plan == "pro"

    def test_updates_billing_cycle_anchor(
        self, workspace: Workspace, mock_subscription_data: dict
    ) -> None:
        """Verify billing_cycle_anchor is updated."""
        with patch("webhooks.services.billing.StripeAPI") as mock_stripe_api_class:
            mock_api = MagicMock()
            mock_api.get_customer_subscriptions.return_value = [mock_subscription_data]
            mock_stripe_api_class.return_value = mock_api

            BillingService.sync_workspace_from_stripe("cus_test123")

            workspace.refresh_from_db()
            assert workspace.billing_cycle_anchor == 1702592000

    def test_handles_trialing_status(self, workspace: Workspace) -> None:
        """Verify trialing status updates trial_end_date."""
        subscription_data = {
            "id": "sub_test123",
            "status": "trialing",
            "current_period_end": 1702592000,
            "items": [{"product_name": "Notipus Pro Plan"}],
        }

        with patch("webhooks.services.billing.StripeAPI") as mock_stripe_api_class:
            mock_api = MagicMock()
            mock_api.get_customer_subscriptions.return_value = [subscription_data]
            mock_stripe_api_class.return_value = mock_api

            BillingService.sync_workspace_from_stripe("cus_test123")

            workspace.refresh_from_db()
            assert workspace.subscription_status == "trial"
            assert workspace.trial_end_date is not None

    def test_handles_stripe_api_error(self, workspace: Workspace) -> None:
        """Verify sync handles Stripe API errors gracefully."""
        import stripe

        with patch("webhooks.services.billing.StripeAPI") as mock_stripe_api_class:
            mock_api = MagicMock()
            mock_api.get_customer_subscriptions.side_effect = stripe.StripeError(
                "API Error"
            )
            mock_stripe_api_class.return_value = mock_api

            result = BillingService.sync_workspace_from_stripe("cus_test123")
            assert result is False

    def test_propagates_database_error(self, workspace: Workspace) -> None:
        """Verify unexpected errors propagate instead of being swallowed."""
        from django.db import OperationalError

        with patch("webhooks.services.billing.Workspace.objects.filter") as mock_filter:
            mock_filter.side_effect = OperationalError("connection lost")

            with pytest.raises(OperationalError):
                BillingService.sync_workspace_from_stripe("cus_test123")


@pytest.mark.django_db
class TestSyncStripeSubscriptionsCommand:
    """Tests for sync_stripe_subscriptions management command."""

    def test_dry_run_does_not_modify_database(self, workspace: Workspace) -> None:
        """Verify dry run mode doesn't modify the database."""
        original_status = workspace.subscription_status

        mock_sub = MagicMock()
        mock_sub.customer = "cus_test123"
        mock_sub.status = "active"
        mock_sub.items = MagicMock()
        mock_sub.items.data = []

        mock_response = MagicMock()
        mock_response.data = [mock_sub]
        mock_response.has_more = False

        with patch("stripe.api_key", "sk_test"):
            with patch("stripe.api_version", "2025-01-01"):
                with patch("stripe.Account.retrieve") as mock_account:
                    mock_account.return_value = MagicMock(id="acct_test")
                    with patch("stripe.Subscription.list") as mock_list:
                        mock_list.return_value = mock_response

                        out = StringIO()
                        call_command(
                            "sync_stripe_subscriptions",
                            "--dry-run",
                            stdout=out,
                        )

        workspace.refresh_from_db()
        assert workspace.subscription_status == original_status
        assert "DRY RUN" in out.getvalue()

    def test_handles_no_subscriptions(self, workspace: Workspace) -> None:
        """Verify command handles empty subscription list."""
        mock_response = MagicMock()
        mock_response.data = []
        mock_response.has_more = False

        with patch("stripe.api_key", "sk_test"):
            with patch("stripe.api_version", "2025-01-01"):
                with patch("stripe.Account.retrieve") as mock_account:
                    mock_account.return_value = MagicMock(id="acct_test")
                    with patch("stripe.Subscription.list") as mock_list:
                        mock_list.return_value = mock_response

                        out = StringIO()
                        call_command("sync_stripe_subscriptions", stdout=out)

        assert "Found 0 subscription(s)" in out.getvalue()

    def test_reports_no_matching_workspace(self) -> None:
        """Verify command reports when no workspace matches customer."""
        mock_sub = MagicMock()
        mock_sub.id = "sub_orphan"
        mock_sub.customer = "cus_orphan"  # No matching workspace
        mock_sub.status = "active"

        mock_response = MagicMock()
        mock_response.data = [mock_sub]
        mock_response.has_more = False

        with patch("stripe.api_key", "sk_test"):
            with patch("stripe.api_version", "2025-01-01"):
                with patch("stripe.Account.retrieve") as mock_account:
                    mock_account.return_value = MagicMock(id="acct_test")
                    with patch("stripe.Subscription.list") as mock_list:
                        mock_list.return_value = mock_response

                        out = StringIO()
                        call_command("sync_stripe_subscriptions", stdout=out)

        assert "SKIP: No workspace" in out.getvalue()

    def test_syncs_workspace_successfully(self, workspace: Workspace) -> None:
        """Verify command syncs workspace status from Stripe."""
        mock_price = MagicMock()
        mock_price.product = MagicMock()
        mock_price.product.name = "Notipus Pro Plan"

        mock_item = MagicMock()
        mock_item.price = mock_price

        mock_items = MagicMock()
        mock_items.data = [mock_item]

        mock_sub = MagicMock()
        mock_sub.id = "sub_test"
        mock_sub.customer = "cus_test123"
        mock_sub.status = "active"
        mock_sub.items = mock_items

        mock_response = MagicMock()
        mock_response.data = [mock_sub]
        mock_response.has_more = False

        with patch("stripe.api_key", "sk_test"):
            with patch("stripe.api_version", "2025-01-01"):
                with patch("stripe.Account.retrieve") as mock_account:
                    mock_account.return_value = MagicMock(id="acct_test")
                    with patch("stripe.Subscription.list") as mock_list:
                        mock_list.return_value = mock_response
                        with patch.object(
                            BillingService,
                            "sync_workspace_from_stripe",
                            return_value=True,
                        ):
                            out = StringIO()
                            call_command("sync_stripe_subscriptions", stdout=out)

        output = out.getvalue()
        assert "SYNCED" in output or "already in sync" in output

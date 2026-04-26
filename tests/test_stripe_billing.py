"""Tests for Stripe billing implementation.

This module contains tests for the Stripe billing features including:
- Checkout session creation
- Customer portal session creation
- Price fetching from Stripe
- Webhook handlers for checkout and billing events
"""

from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from core.services.stripe import StripeAPI, _safe_getattr
from webhooks.services.billing import BillingService


class TestSafeGetattr:
    """Tests for the _safe_getattr helper function.

    The Stripe SDK's __getattr__ raises KeyError instead of AttributeError
    for missing attributes on certain object types. This test class ensures
    _safe_getattr handles both exceptions correctly.
    """

    def test_returns_attribute_value_when_exists(self) -> None:
        """Test that _safe_getattr returns attribute value when it exists."""
        obj = MagicMock()
        obj.test_attr = "test_value"
        result = _safe_getattr(obj, "test_attr")
        assert result == "test_value"

    def test_returns_default_on_attribute_error(self) -> None:
        """Test that _safe_getattr returns default when attribute doesn't exist."""

        class SimpleObject:
            pass

        obj = SimpleObject()
        result = _safe_getattr(obj, "missing_attr", "default_value")
        assert result == "default_value"

    def test_returns_none_as_default(self) -> None:
        """Test that _safe_getattr returns None as default when not specified."""

        class SimpleObject:
            pass

        obj = SimpleObject()
        result = _safe_getattr(obj, "missing_attr")
        assert result is None

    def test_returns_default_on_key_error(self) -> None:
        """Test that _safe_getattr handles KeyError from Stripe-like objects.

        This simulates the Stripe SDK behavior where __getattr__ raises
        KeyError instead of AttributeError for missing attributes.
        """

        class StripeStyleObject:
            """Simulates Stripe SDK object that raises KeyError."""

            def __getattr__(self, name: str) -> Any:
                raise KeyError(name)

        obj = StripeStyleObject()
        result = _safe_getattr(obj, "current_period_start", "default")
        assert result == "default"

    def test_returns_none_on_key_error_without_default(self) -> None:
        """Test that _safe_getattr returns None on KeyError when no default."""

        class StripeStyleObject:
            """Simulates Stripe SDK object that raises KeyError."""

            def __getattr__(self, name: str) -> Any:
                raise KeyError(name)

        obj = StripeStyleObject()
        result = _safe_getattr(obj, "current_period_start")
        assert result is None


class TestStripeAPICheckout:
    """Tests for Stripe Checkout Session functionality."""

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    @pytest.fixture
    def mock_workspace(self) -> MagicMock:
        """Create a mock workspace for testing.

        Returns:
            Mock workspace with standard attributes.
        """
        workspace = MagicMock()
        workspace.id = 1
        workspace.uuid = "test-uuid-1234"
        workspace.name = "Test Workspace"
        workspace.stripe_customer_id = "cus_test123"
        workspace.members.exists.return_value = True
        first_member = MagicMock()
        first_member.user = MagicMock(email="test@example.com")
        workspace.members.first.return_value = first_member
        return workspace

    @patch("core.services.stripe.stripe.checkout.Session.create")
    def test_create_checkout_session_success(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful checkout session creation.

        Args:
            mock_create: Mock for Stripe checkout session create.
            stripe_api: StripeAPI fixture.
        """
        mock_session = Mock()
        mock_session.id = "cs_test123"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test123"
        mock_session.customer = "cus_test123"
        mock_session.status = "open"
        mock_create.return_value = mock_session

        result = stripe_api.create_checkout_session(
            customer_id="cus_test123",
            price_id="price_test123",
        )

        assert result is not None
        assert result["id"] == "cs_test123"
        assert result["url"] == "https://checkout.stripe.com/pay/cs_test123"
        assert result["customer"] == "cus_test123"
        mock_create.assert_called_once()

    @patch("core.services.stripe.stripe.checkout.Session.create")
    def test_create_checkout_session_with_metadata(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test checkout session creation with metadata.

        Args:
            mock_create: Mock for Stripe checkout session create.
            stripe_api: StripeAPI fixture.
        """
        mock_session = Mock()
        mock_session.id = "cs_test123"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test123"
        mock_session.customer = "cus_test123"
        mock_session.status = "open"
        mock_create.return_value = mock_session

        metadata = {"organization_id": "1", "plan_name": "pro"}
        result = stripe_api.create_checkout_session(
            customer_id="cus_test123",
            price_id="price_test123",
            metadata=metadata,
        )

        assert result is not None
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["metadata"] == metadata

    @patch("core.services.stripe.stripe.checkout.Session.create")
    def test_create_checkout_session_stripe_error(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test checkout session creation with Stripe error.

        Args:
            mock_create: Mock for Stripe checkout session create.
            stripe_api: StripeAPI fixture.
        """
        from stripe import StripeError

        mock_create.side_effect = StripeError("Test error")

        result = stripe_api.create_checkout_session(
            customer_id="cus_test123",
            price_id="price_test123",
        )

        assert result is None

    @patch("core.services.stripe.stripe.checkout.Session.create")
    def test_create_checkout_session_with_trial_period(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test checkout session creation with trial period.

        Verifies that trial_period_days is passed to Stripe subscription_data.

        Args:
            mock_create: Mock for Stripe checkout session create.
            stripe_api: StripeAPI fixture.
        """
        mock_session = Mock()
        mock_session.id = "cs_test123"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test123"
        mock_session.customer = "cus_test123"
        mock_session.status = "open"
        mock_create.return_value = mock_session

        result = stripe_api.create_checkout_session(
            customer_id="cus_test123",
            price_id="price_test123",
            trial_period_days=14,
        )

        assert result is not None
        call_kwargs = mock_create.call_args[1]
        assert "subscription_data" in call_kwargs
        assert call_kwargs["subscription_data"]["trial_period_days"] == 14

    @patch("core.services.stripe.stripe.checkout.Session.create")
    def test_create_checkout_session_with_trial_and_metadata(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test checkout session with both trial period and metadata.

        Verifies that subscription_data contains both trial_period_days and metadata.

        Args:
            mock_create: Mock for Stripe checkout session create.
            stripe_api: StripeAPI fixture.
        """
        mock_session = Mock()
        mock_session.id = "cs_test123"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test123"
        mock_session.customer = "cus_test123"
        mock_session.status = "open"
        mock_create.return_value = mock_session

        metadata = {"workspace_id": "123"}
        result = stripe_api.create_checkout_session(
            customer_id="cus_test123",
            price_id="price_test123",
            metadata=metadata,
            trial_period_days=14,
        )

        assert result is not None
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["subscription_data"]["trial_period_days"] == 14
        assert call_kwargs["subscription_data"]["metadata"] == metadata
        assert call_kwargs["metadata"] == metadata

    @patch("core.services.stripe.stripe.checkout.Session.create")
    def test_create_checkout_session_passes_idempotency_key(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """When given an idempotency_key, it must reach stripe.checkout.Session.create.

        Stripe's idempotency window is 24h; this is what collapses
        double-clicks/back-button replays into one checkout session.
        """
        mock_session = Mock()
        mock_session.id = "cs_test123"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test123"
        mock_session.customer = "cus_test123"
        mock_session.status = "open"
        mock_create.return_value = mock_session

        stripe_api.create_checkout_session(
            customer_id="cus_test123",
            price_id="price_test123",
            idempotency_key="checkout-abc-pro-2026-04-25",
        )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs.get("idempotency_key") == "checkout-abc-pro-2026-04-25"

    @patch("core.services.stripe.stripe.checkout.Session.create")
    def test_create_checkout_session_omits_idempotency_key_when_not_provided(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Backwards-compatible: omitting idempotency_key sends no key."""
        mock_session = Mock()
        mock_session.id = "cs_test123"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test123"
        mock_session.customer = "cus_test123"
        mock_session.status = "open"
        mock_create.return_value = mock_session

        stripe_api.create_checkout_session(
            customer_id="cus_test123",
            price_id="price_test123",
        )

        call_kwargs = mock_create.call_args.kwargs
        assert "idempotency_key" not in call_kwargs


class TestStripeAPIPortal:
    """Tests for Stripe Customer Portal functionality."""

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    @patch("core.services.stripe.stripe.billing_portal.Session.create")
    def test_create_portal_session_success(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful portal session creation.

        Args:
            mock_create: Mock for Stripe billing portal session create.
            stripe_api: StripeAPI fixture.
        """
        mock_session = Mock()
        mock_session.id = "bps_test123"
        mock_session.url = "https://billing.stripe.com/session/bps_test123"
        mock_session.customer = "cus_test123"
        mock_create.return_value = mock_session

        result = stripe_api.create_portal_session(customer_id="cus_test123")

        assert result is not None
        assert result["id"] == "bps_test123"
        assert result["url"] == "https://billing.stripe.com/session/bps_test123"
        mock_create.assert_called_once()

    @patch("core.services.stripe.stripe.billing_portal.Session.create")
    def test_create_portal_session_with_return_url(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test portal session creation with custom return URL.

        Args:
            mock_create: Mock for Stripe billing portal session create.
            stripe_api: StripeAPI fixture.
        """
        mock_session = Mock()
        mock_session.id = "bps_test123"
        mock_session.url = "https://billing.stripe.com/session/bps_test123"
        mock_session.customer = "cus_test123"
        mock_create.return_value = mock_session

        return_url = "https://example.com/billing/"
        result = stripe_api.create_portal_session(
            customer_id="cus_test123", return_url=return_url
        )

        assert result is not None
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["return_url"] == return_url

    @patch("core.services.stripe.stripe.billing_portal.Session.create")
    def test_create_portal_session_stripe_error(
        self, mock_create: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test portal session creation with Stripe error.

        Args:
            mock_create: Mock for Stripe billing portal session create.
            stripe_api: StripeAPI fixture.
        """
        from stripe import StripeError

        mock_create.side_effect = StripeError("Test error")

        result = stripe_api.create_portal_session(customer_id="cus_test123")

        assert result is None


class TestStripeAPIPrices:
    """Tests for Stripe price fetching functionality."""

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    @patch("core.services.stripe.stripe.Price.list")
    def test_list_prices_success(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful price listing.

        Args:
            mock_list: Mock for Stripe price list.
            stripe_api: StripeAPI fixture.
        """
        mock_product = Mock()
        mock_product.id = "prod_test123"
        mock_product.name = "Pro Plan"
        mock_product.description = "Professional plan"
        mock_product.active = True
        mock_product.metadata = {"features": '["Feature 1", "Feature 2"]'}

        mock_price = Mock()
        mock_price.id = "price_test123"
        mock_price.product = mock_product
        mock_price.unit_amount = 9900
        mock_price.currency = "usd"
        mock_price.recurring = Mock(interval="month", interval_count=1)

        mock_list.return_value = Mock(data=[mock_price])

        result = stripe_api.list_prices()

        assert len(result) == 1
        assert result[0]["id"] == "price_test123"
        assert result[0]["product_name"] == "Pro Plan"
        assert result[0]["unit_amount"] == 9900

    @patch("core.services.stripe.stripe.Price.list")
    def test_list_prices_filters_inactive_products(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test that inactive products are filtered out.

        Args:
            mock_list: Mock for Stripe price list.
            stripe_api: StripeAPI fixture.
        """
        mock_product = Mock()
        mock_product.id = "prod_test123"
        mock_product.active = False

        mock_price = Mock()
        mock_price.id = "price_test123"
        mock_price.product = mock_product

        mock_list.return_value = Mock(data=[mock_price])

        result = stripe_api.list_prices(active_only=True)

        assert len(result) == 0

    @patch("core.services.stripe.stripe.Price.list")
    def test_list_prices_stripe_error(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test price listing with Stripe error.

        Args:
            mock_list: Mock for Stripe price list.
            stripe_api: StripeAPI fixture.
        """
        from stripe import StripeError

        mock_list.side_effect = StripeError("Test error")

        result = stripe_api.list_prices()

        assert result == []


@pytest.mark.django_db
class TestStripeAPIGetOrCreateCustomer:
    """Tests for get_or_create_customer functionality.

    Uses real Workspace rows because the implementation acquires a
    row-level lock via `select_for_update()` to make concurrent calls safe.
    """

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    @pytest.fixture
    def workspace(self) -> Any:
        """Create a real Workspace row with no Stripe customer yet."""
        from core.models import Workspace

        return Workspace.objects.create(
            name="Test Workspace",
            subscription_plan="free",
            subscription_status="active",
        )

    @patch("core.services.stripe.stripe.Customer.modify")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_creates_new_customer_when_none_exists(
        self,
        mock_create: MagicMock,
        mock_modify: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """Customer creation when workspace has no Stripe customer."""
        mock_customer = Mock()
        mock_customer.id = "cus_new123"
        mock_customer.to_dict.return_value = {"id": "cus_new123"}
        mock_create.return_value = mock_customer

        result = stripe_api.get_or_create_customer(workspace)

        assert result is not None
        assert result["id"] == "cus_new123"
        mock_create.assert_called_once()
        workspace.refresh_from_db()
        assert workspace.stripe_customer_id == "cus_new123"

    @patch("core.services.stripe.stripe.Customer.modify")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_passes_idempotency_key_derived_from_workspace_uuid(
        self,
        mock_create: MagicMock,
        mock_modify: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """The Stripe call must carry an idempotency_key derived from the
        workspace UUID, so a network retry within Stripe's 24h window
        cannot create a second Customer for the same workspace.
        """
        mock_customer = Mock()
        mock_customer.id = "cus_new123"
        mock_customer.to_dict.return_value = {"id": "cus_new123"}
        mock_create.return_value = mock_customer

        stripe_api.get_or_create_customer(workspace)

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs.get("idempotency_key") == (
            f"workspace-customer-{workspace.uuid}"
        )

    @patch("core.services.stripe.stripe.Customer.modify")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_idempotent_create_payload_omits_mutable_fields(
        self,
        mock_create: MagicMock,
        mock_modify: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """Stripe rejects an idempotency-key replay whose params differ from
        the original call. The workspace name and member email can change
        between calls, so they must NOT travel through the idempotent create
        — only the immutable workspace identifiers, which never change.
        """
        mock_customer = Mock()
        mock_customer.id = "cus_new123"
        mock_customer.to_dict.return_value = {"id": "cus_new123"}
        mock_create.return_value = mock_customer

        stripe_api.get_or_create_customer(workspace)

        call_kwargs = mock_create.call_args.kwargs
        # Only the idempotency key + metadata payload is allowed.
        assert "name" not in call_kwargs
        assert "email" not in call_kwargs
        metadata = call_kwargs.get("metadata", {})
        assert metadata.get("workspace_uuid") == str(workspace.uuid)
        assert metadata.get("workspace_id") == str(workspace.id)

    @patch("core.services.stripe.stripe.Customer.modify")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_applies_name_and_email_via_customer_modify(
        self,
        mock_create: MagicMock,
        mock_modify: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """Mutable display fields must still reach Stripe — just via a
        non-idempotent follow-up call so a workspace rename doesn't trip
        Stripe's idempotency-mismatch check on the next create attempt.
        """
        from core.models import WorkspaceMember
        from django.contrib.auth.models import User

        user = User.objects.create_user(
            username="alice", email="alice@example.com", password="x"
        )
        WorkspaceMember.objects.create(
            user=user, workspace=workspace, role="owner", is_active=True
        )

        mock_customer = Mock()
        mock_customer.id = "cus_new123"
        mock_customer.to_dict.return_value = {"id": "cus_new123"}
        mock_create.return_value = mock_customer

        stripe_api.get_or_create_customer(workspace)

        mock_modify.assert_called_once()
        modify_args, modify_kwargs = mock_modify.call_args
        assert modify_args[0] == "cus_new123"
        assert modify_kwargs.get("name") == workspace.name
        assert modify_kwargs.get("email") == "alice@example.com"

    @patch("core.services.stripe.stripe.Customer.retrieve")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_does_not_create_when_workspace_already_has_customer(
        self,
        mock_create: MagicMock,
        mock_retrieve: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """Existing Stripe customer is retrieved, not re-created."""
        workspace.stripe_customer_id = "cus_existing123"
        workspace.save(update_fields=["stripe_customer_id"])

        existing_customer = Mock()
        existing_customer.id = "cus_existing123"
        existing_customer.deleted = False
        existing_customer.to_dict.return_value = {"id": "cus_existing123"}
        mock_retrieve.return_value = existing_customer

        result = stripe_api.get_or_create_customer(workspace)

        assert result is not None
        assert result["id"] == "cus_existing123"
        mock_retrieve.assert_called_once_with("cus_existing123")
        mock_create.assert_not_called()

    @patch("core.services.stripe.stripe.Customer.modify")
    @patch("core.services.stripe.stripe.Customer.retrieve")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_overwrites_stripe_customer_id_when_existing_one_was_deleted(
        self,
        mock_create: MagicMock,
        mock_retrieve: MagicMock,
        mock_modify: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """If the workspace points at a Stripe customer that has been deleted,
        the row must be updated to the freshly created one. Otherwise every
        future call would try to retrieve the dead id, fall through, and
        create yet another customer.
        """
        from core.models import Workspace

        workspace.stripe_customer_id = "cus_dead"
        workspace.save(update_fields=["stripe_customer_id"])

        deleted_customer = Mock()
        deleted_customer.id = "cus_dead"
        deleted_customer.deleted = True
        deleted_customer.to_dict.return_value = {"id": "cus_dead", "deleted": True}
        mock_retrieve.return_value = deleted_customer

        new_customer = Mock()
        new_customer.id = "cus_new"
        new_customer.to_dict.return_value = {"id": "cus_new"}
        mock_create.return_value = new_customer

        result = stripe_api.get_or_create_customer(workspace)

        assert result is not None
        assert result["id"] == "cus_new"
        # The DB row must have been overwritten — not still pointing at cus_dead.
        assert Workspace.objects.get(pk=workspace.pk).stripe_customer_id == "cus_new"

    @patch("core.services.stripe.stripe.Customer.modify")
    @patch("core.services.stripe.stripe.Customer.retrieve")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_concurrent_callers_create_only_one_customer(
        self,
        mock_create: MagicMock,
        mock_retrieve: MagicMock,
        mock_modify: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """Two concurrent get_or_create_customer calls on the same
        workspace must not both call stripe.Customer.create.

        Simulates the race where two requests both load a stale
        workspace instance with empty stripe_customer_id, then race to
        create a customer. The select_for_update lock + re-check should
        cause the second caller to see the first caller's write and
        skip the create.
        """
        from core.models import Workspace

        # First caller wins: creates customer + writes back to DB.
        mock_customer = Mock()
        mock_customer.id = "cus_first"
        mock_customer.to_dict.return_value = {"id": "cus_first"}
        mock_create.return_value = mock_customer

        existing_customer = Mock()
        existing_customer.id = "cus_first"
        existing_customer.deleted = False
        existing_customer.to_dict.return_value = {"id": "cus_first"}
        mock_retrieve.return_value = existing_customer

        # Both callers receive the same in-memory workspace with
        # stripe_customer_id="". (The first call will write through.)
        result_a = stripe_api.get_or_create_customer(workspace)

        # Reload a stale copy that mimics a concurrent request that
        # loaded the workspace row before the first request committed.
        stale = Workspace.objects.get(pk=workspace.pk)
        stale.stripe_customer_id = ""  # simulate the stale view
        result_b = stripe_api.get_or_create_customer(stale)

        assert result_a is not None and result_a["id"] == "cus_first"
        assert result_b is not None and result_b["id"] == "cus_first"
        # Stripe.Customer.create called exactly once across both callers.
        assert mock_create.call_count == 1

    @patch("core.services.stripe.stripe.Customer.retrieve")
    @patch("core.services.stripe.stripe.Customer.modify")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_returns_persisted_customer_when_phase3_diverges(
        self,
        mock_create: MagicMock,
        mock_modify: MagicMock,
        mock_retrieve: MagicMock,
        stripe_api: StripeAPI,
        workspace: Any,
    ) -> None:
        """If a concurrent racer wrote a different stripe_customer_id
        between Phase 1 and Phase 3, get_or_create_customer must return
        the customer that was actually persisted on the workspace, not
        the one we created in Phase 2 — otherwise callers (checkout,
        portal) would use a customer id that doesn't match the DB.

        In practice the idempotency key keeps the two ids equal, but
        this guards against future drift in the key derivation.
        """
        from core.models import Workspace

        # Phase 2: simulate a concurrent racer that wins by writing a
        # different id to the DB *during* our Phase 2 create call —
        # before we acquire the Phase 3 lock. By the time Phase 3 reads
        # the row under select_for_update, fresh.stripe_customer_id is
        # "cus_winner" (not empty, not equal to the original
        # existing_customer_id of ""), so our overwrite condition is
        # False and we should fall through to retrieving the winner.
        ours = Mock()
        ours.id = "cus_ours"
        ours.to_dict.return_value = {"id": "cus_ours"}

        def racer_writes_during_create(**_: Any) -> Mock:
            Workspace.objects.filter(pk=workspace.pk).update(
                stripe_customer_id="cus_winner"
            )
            return ours

        mock_create.side_effect = racer_writes_during_create

        winner = Mock()
        winner.id = "cus_winner"
        winner.to_dict.return_value = {"id": "cus_winner"}
        mock_retrieve.return_value = winner

        result = stripe_api.get_or_create_customer(workspace)

        assert result is not None and result["id"] == "cus_winner"
        # Phase 3 fall-through must re-fetch the persisted customer
        # rather than returning the Phase 2 customer we created.
        mock_retrieve.assert_called_once_with("cus_winner")


class TestBillingServiceWebhooks:
    """Tests for billing service webhook handlers."""

    def test_handle_checkout_completed_success(self) -> None:
        """Test successful checkout completed handler."""
        session_data: dict[str, Any] = {
            "customer": "cus_test123",
            "subscription": "sub_test123",
            "metadata": {
                "organization_id": "1",
                "plan_name": "pro",
            },
        }

        with patch.object(
            BillingService, "_get_customer_id", return_value="cus_test123"
        ):
            with patch("core.models.Workspace.objects.filter") as mock_filter:
                mock_filter.return_value.update.return_value = 1
                # This should not raise
                BillingService.handle_checkout_completed(session_data)
                mock_filter.assert_called()

    def test_handle_checkout_completed_missing_customer(self) -> None:
        """Test checkout completed with missing customer ID."""
        session_data: dict[str, Any] = {
            "subscription": "sub_test123",
            "metadata": {"plan_name": "pro"},
        }

        # Should not raise, just log error
        BillingService.handle_checkout_completed(session_data)

    def test_handle_trial_ending(self) -> None:
        """Test trial ending handler."""
        subscription_data: dict[str, Any] = {
            "customer": "cus_test123",
            "trial_end": 1704067200,
        }

        with patch("core.models.Workspace.objects.filter") as mock_filter:
            mock_ws = MagicMock(name="Test Workspace")
            mock_filter.return_value.first.return_value = mock_ws
            # Should not raise
            BillingService.handle_trial_ending(subscription_data)

    def test_handle_invoice_paid(self) -> None:
        """Test invoice paid handler."""
        invoice_data: dict[str, Any] = {
            "customer": "cus_test123",
            "period_end": 1704067200,
        }

        with patch("core.models.Workspace.objects.filter") as mock_filter:
            mock_filter.return_value.update.return_value = 1
            BillingService.handle_invoice_paid(invoice_data)
            mock_filter.assert_called_once_with(stripe_customer_id="cus_test123")

    def test_handle_payment_action_required(self) -> None:
        """Test payment action required handler."""
        invoice_data: dict[str, Any] = {
            "customer": "cus_test123",
            "hosted_invoice_url": "https://invoice.stripe.com/i/test123",
        }

        with patch("core.models.Workspace.objects.filter") as mock_filter:
            mock_ws = MagicMock(name="Test Workspace")
            mock_filter.return_value.first.return_value = mock_ws
            # Should not raise
            BillingService.handle_payment_action_required(invoice_data)


class TestStripeAPIInvoices:
    """Tests for Stripe invoice fetching functionality."""

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    @patch("core.services.stripe.stripe.Invoice.list")
    def test_get_invoices_success(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful invoice retrieval.

        Args:
            mock_list: Mock for Stripe invoice list.
            stripe_api: StripeAPI fixture.
        """
        mock_invoice = Mock()
        mock_invoice.id = "in_test123"
        mock_invoice.number = "INV-001"
        mock_invoice.status = "paid"
        mock_invoice.amount_due = 9900
        mock_invoice.amount_paid = 9900
        mock_invoice.currency = "usd"
        mock_invoice.created = 1704067200
        mock_invoice.period_start = 1704067200
        mock_invoice.period_end = 1706745600
        mock_invoice.hosted_invoice_url = "https://invoice.stripe.com/i/test123"
        mock_invoice.invoice_pdf = "https://invoice.stripe.com/i/test123/pdf"

        mock_list.return_value = Mock(data=[mock_invoice])

        result = stripe_api.get_invoices("cus_test123")

        assert len(result) == 1
        assert result[0]["id"] == "in_test123"
        assert result[0]["number"] == "INV-001"
        assert result[0]["amount_paid"] == 9900

    @patch("core.services.stripe.stripe.Invoice.list")
    def test_get_invoices_stripe_error(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test invoice retrieval with Stripe error.

        Args:
            mock_list: Mock for Stripe invoice list.
            stripe_api: StripeAPI fixture.
        """
        from stripe import StripeError

        mock_list.side_effect = StripeError("Test error")

        result = stripe_api.get_invoices("cus_test123")

        assert result == []


class TestStripeAPISubscriptions:
    """Tests for Stripe subscription fetching functionality."""

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    @staticmethod
    def _build_mock_subscription(sub_id: str = "sub_test123") -> Mock:
        mock_product = Mock()
        mock_product.name = "Pro Plan"

        mock_price = Mock()
        mock_price.id = "price_test123"
        mock_price.product = mock_product
        mock_price.unit_amount = 9900
        mock_price.currency = "usd"

        mock_item = Mock()
        mock_item.price = mock_price
        mock_item.quantity = 1

        sub = Mock()
        sub.id = sub_id
        sub.status = "active"
        sub.current_period_start = 1704067200
        sub.current_period_end = 1706745600
        sub.cancel_at_period_end = False
        sub.canceled_at = None
        sub.items = Mock(data=[mock_item])
        return sub

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_get_customer_subscriptions_success(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful subscription retrieval."""
        mock_subscription = self._build_mock_subscription()
        list_response = Mock()
        list_response.auto_paging_iter.return_value = iter([mock_subscription])
        mock_list.return_value = list_response

        result = stripe_api.get_customer_subscriptions("cus_test123")

        assert len(result) == 1
        assert result[0]["id"] == "sub_test123"
        assert result[0]["status"] == "active"

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_get_customer_subscriptions_paginates_across_pages(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """The duplicate-subscription guard relies on this method seeing
        every subscription, not just the first Stripe page. A customer
        with many canceled subs plus one off-page live one must still
        return that live one. Verifies auto_paging_iter is being used.
        """
        page_one = [self._build_mock_subscription(f"sub_{i}") for i in range(100)]
        page_two = [self._build_mock_subscription("sub_off_page")]
        list_response = Mock()
        list_response.auto_paging_iter.return_value = iter(page_one + page_two)
        mock_list.return_value = list_response

        result = stripe_api.get_customer_subscriptions("cus_test123")

        assert len(result) == 101
        assert result[-1]["id"] == "sub_off_page"

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_get_customer_subscriptions_stripe_error(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test subscription retrieval with Stripe error."""
        from stripe import StripeError

        mock_list.side_effect = StripeError("Test error")

        result = stripe_api.get_customer_subscriptions("cus_test123")

        assert result == []

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_get_customer_subscriptions_raises_when_raise_on_error(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Callers that need to fail closed (e.g. the duplicate-subscription
        guard in checkout) pass raise_on_error=True so a Stripe outage is
        not silently mapped to "no subscriptions".
        """
        from stripe import StripeError

        mock_list.side_effect = StripeError("Service unavailable")

        with pytest.raises(StripeError):
            stripe_api.get_customer_subscriptions("cus_test123", raise_on_error=True)

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_has_live_subscription_returns_true_on_first_match(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """has_live_subscription must short-circuit on the first live
        status it finds — no need to query the rest, no need to page
        through canceled subs. Verifies only one Stripe call is made
        when the very first probe (active) hits.
        """
        list_response = Mock()
        list_response.data = [Mock()]
        mock_list.return_value = list_response

        assert stripe_api.has_live_subscription("cus_test123") is True
        # Short-circuited after the first ("active") query
        mock_list.assert_called_once_with(
            customer="cus_test123", status="active", limit=1
        )

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_has_live_subscription_checks_all_live_statuses(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """When no subscriptions exist for any live status, the helper
        must probe each of active/trialing/past_due before returning
        False — past_due in particular is easy to forget and would
        let an unpaid customer start a second subscription.
        """
        empty = Mock()
        empty.data = []
        mock_list.return_value = empty

        assert stripe_api.has_live_subscription("cus_test123") is False
        statuses = [c.kwargs["status"] for c in mock_list.call_args_list]
        assert statuses == ["active", "trialing", "past_due"]

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_has_live_subscription_returns_false_on_error_by_default(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Default behavior matches get_customer_subscriptions: swallow
        the Stripe error and return False so non-critical callers don't
        crash on transient outages."""
        from stripe import StripeError

        mock_list.side_effect = StripeError("Service unavailable")

        assert stripe_api.has_live_subscription("cus_test123") is False

    @patch("core.services.stripe.stripe.Subscription.list")
    def test_has_live_subscription_raises_when_raise_on_error(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """The checkout duplicate-subscription guard relies on this: a
        silent False on Stripe outage would be indistinguishable from
        "no live sub" and let a second subscription through. With
        raise_on_error=True the caller can route the user to the portal
        instead of charging them a second time.
        """
        from stripe import StripeError

        mock_list.side_effect = StripeError("Service unavailable")

        with pytest.raises(StripeError):
            stripe_api.has_live_subscription("cus_test123", raise_on_error=True)


class TestExtractSubscriptionItems:
    """Tests for StripeAPI._extract_subscription_items with dict-style access.

    The Stripe SDK objects can behave differently depending on context.
    These tests ensure dict-style access works correctly.
    """

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    def test_extracts_items_from_dict_style_subscription(
        self, stripe_api: StripeAPI
    ) -> None:
        """Test extraction from subscription with dict-style access.

        Args:
            stripe_api: StripeAPI fixture.
        """
        # Simulate Stripe SDK object that behaves like a dict
        subscription = {
            "id": "sub_test123",
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_test123",
                            "product": {
                                "name": "Pro Plan",
                                "metadata": {"plan_name": "pro"},
                            },
                            "unit_amount": 9900,
                            "currency": "usd",
                        },
                        "quantity": 1,
                    }
                ]
            },
        }

        result = stripe_api._extract_subscription_items(subscription)

        assert len(result) == 1
        assert result[0]["price_id"] == "price_test123"
        assert result[0]["product_name"] == "Pro Plan"
        assert result[0]["plan_name"] == "pro"
        assert result[0]["unit_amount"] == 9900
        assert result[0]["currency"] == "usd"
        assert result[0]["quantity"] == 1

    def test_extracts_plan_name_from_metadata(self, stripe_api: StripeAPI) -> None:
        """Test that plan_name is extracted from Product metadata.

        Args:
            stripe_api: StripeAPI fixture.
        """
        subscription = {
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_123",
                            "product": {
                                "name": "Enterprise Plan",
                                "metadata": {"plan_name": "enterprise"},
                            },
                        },
                        "quantity": 1,
                    }
                ]
            },
        }

        result = stripe_api._extract_subscription_items(subscription)

        assert result[0]["plan_name"] == "enterprise"

    def test_returns_empty_list_when_no_items(self, stripe_api: StripeAPI) -> None:
        """Test returns empty list when subscription has no items.

        Args:
            stripe_api: StripeAPI fixture.
        """
        subscription: dict[str, Any] = {"id": "sub_test123"}

        result = stripe_api._extract_subscription_items(subscription)

        assert result == []

    def test_returns_empty_list_when_items_data_empty(
        self, stripe_api: StripeAPI
    ) -> None:
        """Test returns empty list when items.data is empty.

        Args:
            stripe_api: StripeAPI fixture.
        """
        subscription = {"items": {"data": []}}

        result = stripe_api._extract_subscription_items(subscription)

        assert result == []

    @patch("core.services.stripe.stripe.Product.retrieve")
    def test_fetches_product_when_not_expanded(
        self, mock_retrieve: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test that Product is fetched when only ID is provided.

        Args:
            mock_retrieve: Mock for stripe.Product.retrieve.
            stripe_api: StripeAPI fixture.
        """
        mock_product = Mock()
        mock_product.name = "Basic Plan"
        mock_product.metadata = {"plan_name": "basic"}
        mock_retrieve.return_value = mock_product

        subscription = {
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_123",
                            "product": "prod_123",  # Just the product ID
                        },
                        "quantity": 1,
                    }
                ]
            },
        }

        result = stripe_api._extract_subscription_items(subscription)

        mock_retrieve.assert_called_once_with("prod_123")
        assert result[0]["product_name"] == "Basic Plan"
        assert result[0]["plan_name"] == "basic"


class TestStripeAPIArchive:
    """Tests for Stripe product and price archiving functionality."""

    @pytest.fixture
    def stripe_api(self) -> StripeAPI:
        """Create a StripeAPI instance for testing.

        Returns:
            Configured StripeAPI instance.
        """
        return StripeAPI()

    @patch("core.services.stripe.stripe.Product.modify")
    def test_archive_product_success(
        self, mock_modify: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful product archiving.

        Args:
            mock_modify: Mock for Stripe product modify.
            stripe_api: StripeAPI fixture.
        """
        mock_modify.return_value = Mock()

        result = stripe_api.archive_product("prod_test123")

        assert result is True
        mock_modify.assert_called_once_with("prod_test123", active=False)

    @patch("core.services.stripe.stripe.Product.modify")
    def test_archive_product_stripe_error(
        self, mock_modify: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test product archiving with Stripe error.

        Args:
            mock_modify: Mock for Stripe product modify.
            stripe_api: StripeAPI fixture.
        """
        from stripe import StripeError

        mock_modify.side_effect = StripeError("Test error")

        result = stripe_api.archive_product("prod_test123")

        assert result is False

    @patch("core.services.stripe.stripe.Price.modify")
    def test_archive_price_success(
        self, mock_modify: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful price archiving.

        Args:
            mock_modify: Mock for Stripe price modify.
            stripe_api: StripeAPI fixture.
        """
        mock_modify.return_value = Mock()

        result = stripe_api.archive_price("price_test123")

        assert result is True
        mock_modify.assert_called_once_with("price_test123", active=False)

    @patch("core.services.stripe.stripe.Price.modify")
    def test_archive_price_stripe_error(
        self, mock_modify: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test price archiving with Stripe error.

        Args:
            mock_modify: Mock for Stripe price modify.
            stripe_api: StripeAPI fixture.
        """
        from stripe import StripeError

        mock_modify.side_effect = StripeError("Test error")

        result = stripe_api.archive_price("price_test123")

        assert result is False

    @patch("core.services.stripe.stripe.Price.list")
    def test_list_prices_for_product_success(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test successful price listing for a product.

        Args:
            mock_list: Mock for Stripe price list.
            stripe_api: StripeAPI fixture.
        """
        mock_price = Mock()
        mock_price.id = "price_test123"
        mock_price.product = "prod_test123"
        mock_price.unit_amount = 2900
        mock_price.currency = "usd"
        mock_price.lookup_key = "basic_monthly"
        mock_price.active = True
        mock_price.recurring = Mock(interval="month", interval_count=1)

        mock_list.return_value = Mock(data=[mock_price])

        result = stripe_api.list_prices_for_product("prod_test123")

        assert len(result) == 1
        assert result[0]["id"] == "price_test123"
        assert result[0]["unit_amount"] == 2900
        assert result[0]["recurring"]["interval"] == "month"
        mock_list.assert_called_once_with(
            product="prod_test123", limit=100, active=True
        )

    @patch("core.services.stripe.stripe.Price.list")
    def test_list_prices_for_product_stripe_error(
        self, mock_list: MagicMock, stripe_api: StripeAPI
    ) -> None:
        """Test price listing with Stripe error.

        Args:
            mock_list: Mock for Stripe price list.
            stripe_api: StripeAPI fixture.
        """
        from stripe import StripeError

        mock_list.side_effect = StripeError("Test error")

        result = stripe_api.list_prices_for_product("prod_test123")

        assert result == []


@pytest.mark.django_db
class TestCheckoutViewActiveSubscriptionGuard:
    """The checkout() view must refuse to create a second subscription for
    a workspace that already has a live one in Stripe. The previous absence
    of this guard is what let one customer accumulate three parallel Pro
    subscriptions, all billing monthly.
    """

    @pytest.fixture
    def setup(self) -> Any:
        """Build a logged-in user + workspace + plan, return them."""
        from core.models import Plan, Workspace, WorkspaceMember
        from django.contrib.auth.models import User
        from django.test import Client

        user = User.objects.create_user(username="vik", password="x")
        workspace = Workspace.objects.create(
            name="Vik Workspace",
            subscription_plan="free",
            subscription_status="active",
            stripe_customer_id="cus_existing",
        )
        WorkspaceMember.objects.create(
            user=user, workspace=workspace, role="owner", is_active=True
        )
        # The "pro" Plan may already exist via migrations; ensure it has a
        # Stripe price id so the view doesn't bail before the guard.
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 99,
                "is_active": True,
                "stripe_price_id_monthly": "price_pro_monthly",
            },
        )
        client = Client()
        client.force_login(user)
        return client, workspace

    @patch("core.services.stripe.StripeAPI.get_price_by_lookup_key")
    @patch("core.services.stripe.StripeAPI.create_checkout_session")
    @patch("core.services.stripe.StripeAPI.has_live_subscription")
    @patch("core.services.stripe.StripeAPI.get_or_create_customer")
    def test_redirects_to_billing_portal_when_active_subscription_exists(
        self,
        mock_get_or_create: MagicMock,
        mock_has_live: MagicMock,
        mock_create_session: MagicMock,
        mock_get_price: MagicMock,
        setup: Any,
    ) -> None:
        """Active sub on Stripe -> redirect to billing portal, no new session."""
        from django.urls import reverse

        client, _workspace = setup
        mock_get_or_create.return_value = {"id": "cus_existing"}
        mock_has_live.return_value = True
        mock_get_price.return_value = {"id": "price_pro_monthly"}

        response = client.get(reverse("core:checkout", args=["pro"]))

        assert response.status_code == 302
        assert reverse("core:billing_portal") in response.url
        mock_create_session.assert_not_called()

    @patch("core.services.stripe.StripeAPI.get_price_by_lookup_key")
    @patch("core.services.stripe.StripeAPI.create_checkout_session")
    @patch("core.services.stripe.StripeAPI.has_live_subscription")
    @patch("core.services.stripe.StripeAPI.get_or_create_customer")
    def test_proceeds_to_checkout_when_no_active_subscription(
        self,
        mock_get_or_create: MagicMock,
        mock_has_live: MagicMock,
        mock_create_session: MagicMock,
        mock_get_price: MagicMock,
        setup: Any,
    ) -> None:
        """No live sub -> checkout session is created and user redirected to it."""
        from django.urls import reverse

        client, _workspace = setup
        mock_get_or_create.return_value = {"id": "cus_existing"}
        mock_has_live.return_value = False
        mock_get_price.return_value = {"id": "price_pro_monthly"}
        mock_create_session.return_value = {
            "id": "cs_new",
            "url": "https://checkout.stripe.com/x",
        }

        response = client.get(reverse("core:checkout", args=["pro"]))

        assert response.status_code == 302
        assert response.url == "https://checkout.stripe.com/x"
        mock_create_session.assert_called_once()

    @patch("core.services.stripe.StripeAPI.get_price_by_lookup_key")
    @patch("core.services.stripe.StripeAPI.create_checkout_session")
    @patch("core.services.stripe.StripeAPI.has_live_subscription")
    @patch("core.services.stripe.StripeAPI.get_or_create_customer")
    def test_redirects_to_billing_portal_on_stripe_error_in_subscription_check(
        self,
        mock_get_or_create: MagicMock,
        mock_has_live: MagicMock,
        mock_create_session: MagicMock,
        mock_get_price: MagicMock,
        setup: Any,
    ) -> None:
        """If Stripe is unavailable when checking for an existing live
        subscription, the view must NOT fall through to creating a new one
        — that's how the original duplicate-billing bug surfaces. It must
        redirect the user to the billing portal with an explanatory error
        instead of charging them a second time.
        """
        from django.urls import reverse
        from stripe import StripeError

        client, _workspace = setup
        mock_get_or_create.return_value = {"id": "cus_existing"}
        mock_has_live.side_effect = StripeError("Service unavailable")
        mock_get_price.return_value = {"id": "price_pro_monthly"}

        response = client.get(reverse("core:checkout", args=["pro"]))

        assert response.status_code == 302
        assert reverse("core:billing_portal") in response.url
        mock_create_session.assert_not_called()

    @patch("core.services.stripe.StripeAPI.get_price_by_lookup_key")
    @patch("core.services.stripe.StripeAPI.create_checkout_session")
    @patch("core.services.stripe.StripeAPI.has_live_subscription")
    @patch("core.services.stripe.StripeAPI.get_or_create_customer")
    def test_skips_live_subscription_probe_for_brand_new_customer(
        self,
        mock_get_or_create: MagicMock,
        mock_has_live: MagicMock,
        mock_create_session: MagicMock,
        mock_get_price: MagicMock,
    ) -> None:
        """A workspace that didn't have a stripe_customer_id before this
        request can't possibly have an existing subscription — the
        customer was just created. Skipping the probe saves 1-3 Stripe
        API calls on every first checkout and lowers latency.
        """
        from core.models import Plan, Workspace, WorkspaceMember
        from django.contrib.auth.models import User
        from django.test import Client
        from django.urls import reverse

        # Brand-new workspace: no stripe_customer_id yet.
        user = User.objects.create_user(username="newcomer", password="x")
        workspace = Workspace.objects.create(
            name="New Workspace",
            subscription_plan="free",
            subscription_status="active",
            stripe_customer_id="",
        )
        WorkspaceMember.objects.create(
            user=user, workspace=workspace, role="owner", is_active=True
        )
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 99,
                "is_active": True,
                "stripe_price_id_monthly": "price_pro_monthly",
            },
        )
        client = Client()
        client.force_login(user)

        # get_or_create_customer "creates" a new customer; the probe must
        # not be called even though there's now a customer["id"].
        mock_get_or_create.return_value = {"id": "cus_just_created"}
        mock_get_price.return_value = {"id": "price_pro_monthly"}
        mock_create_session.return_value = {
            "id": "cs_new",
            "url": "https://checkout.stripe.com/x",
        }

        response = client.get(reverse("core:checkout", args=["pro"]))

        assert response.status_code == 302
        assert response.url == "https://checkout.stripe.com/x"
        mock_has_live.assert_not_called()
        mock_create_session.assert_called_once()

    @patch("core.services.stripe.StripeAPI.get_price_by_lookup_key")
    @patch("core.services.stripe.StripeAPI.create_checkout_session")
    @patch("core.services.stripe.StripeAPI.has_live_subscription")
    @patch("core.services.stripe.StripeAPI.get_or_create_customer")
    def test_passes_idempotency_key_to_checkout_session(
        self,
        mock_get_or_create: MagicMock,
        mock_has_live: MagicMock,
        mock_create_session: MagicMock,
        mock_get_price: MagicMock,
        setup: Any,
    ) -> None:
        """Checkout view must pass a stable idempotency_key derived from
        workspace UUID + plan + today's date so a double-click within
        Stripe's 24h window collapses to a single session."""
        from django.urls import reverse

        client, workspace = setup
        mock_get_or_create.return_value = {"id": "cus_existing"}
        mock_has_live.return_value = False
        mock_get_price.return_value = {"id": "price_pro_monthly"}
        mock_create_session.return_value = {
            "id": "cs_new",
            "url": "https://checkout.stripe.com/x",
        }

        client.get(reverse("core:checkout", args=["pro"]))

        kwargs = mock_create_session.call_args.kwargs
        idempotency_key = kwargs.get("idempotency_key", "")
        assert str(workspace.uuid) in idempotency_key
        assert "pro" in idempotency_key
        assert idempotency_key.startswith("checkout-")

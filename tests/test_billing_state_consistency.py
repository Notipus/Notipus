"""Tests for billing/subscription state consistency (issue #81).

Covers the audit findings on Workspace state drifting from Stripe:
trial_end_date sourced from the wrong field, inconsistent
billing_cycle_anchor conventions, `incomplete` granting trial access,
non-atomic write-then-sync races, over-broad cancellation/past_due
logic, and arbitrary plan names reaching subscription_plan.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.models import Workspace
from webhooks.services.billing import (
    STRIPE_STATUS_MAPPING,
    BillingService,
    StripeSyncLockTimeout,
    stripe_sync_lock,
)

CUSTOMER_ID = "cus_test123"

PERIOD_START = 1700000000
PERIOD_END = 1702592000
EXTENDED_TRIAL_END = 1710003600  # support-extended trial, > PERIOD_END


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create an active workspace linked to a Stripe customer.

    Args:
        db: pytest-django database fixture.

    Returns:
        Persisted Workspace with a Stripe customer id.
    """
    return Workspace.objects.create(
        name="Test Workspace",
        stripe_customer_id=CUSTOMER_ID,
        subscription_status="active",
        subscription_plan="free",
    )


def _mock_stripe_api(subscriptions: list[dict[str, Any]]) -> Any:
    """Patch billing.StripeAPI to return the given subscriptions.

    Args:
        subscriptions: Subscription dicts the fake API returns.

    Returns:
        Context manager patching webhooks.services.billing.StripeAPI.
    """
    mock_api = MagicMock()
    mock_api.get_customer_subscriptions.return_value = subscriptions
    return patch("webhooks.services.billing.StripeAPI", return_value=mock_api)


class FakeLock:
    """Non-reentrant fake Redis lock recording acquire/release events."""

    def __init__(self, events: list[str]) -> None:
        """Initialize the lock.

        Args:
            events: Shared list that acquire/release events are appended to.
        """
        self.events = events
        self.held = False

    def acquire(self) -> bool:
        """Acquire the lock; fails (returns False) while already held.

        Returns:
            True when acquired, False when held by another caller.
        """
        if self.held:
            self.events.append("acquire-blocked")
            return False
        self.held = True
        self.events.append("acquire")
        return True

    def release(self) -> None:
        """Release the lock."""
        self.held = False
        self.events.append("release")


class FakeRedisClient:
    """Fake Redis client exposing only the lock() API used by billing."""

    def __init__(self, lock: FakeLock) -> None:
        """Initialize with the single lock instance to hand out.

        Args:
            lock: FakeLock returned for every lock() call.
        """
        self._lock = lock
        self.lock_names: list[str] = []

    def lock(self, name: str, **_kwargs: Any) -> FakeLock:
        """Return the shared fake lock, recording the key used.

        Args:
            name: Redis lock key.
            **_kwargs: Ignored redis-py lock options.

        Returns:
            The shared FakeLock.
        """
        self.lock_names.append(name)
        return self._lock


class _FakeCacheClientWrapper:
    """Mimics django-redis cache.client wrapper."""

    def __init__(self, client: FakeRedisClient) -> None:
        """Store the fake client.

        Args:
            client: Fake Redis client to expose.
        """
        self._client = client

    def get_client(self) -> FakeRedisClient:
        """Return the underlying fake Redis client.

        Returns:
            The FakeRedisClient.
        """
        return self._client


class FakeCache:
    """Mimics the django-redis cache object shape used by billing."""

    def __init__(self, client: FakeRedisClient) -> None:
        """Wrap the fake Redis client.

        Args:
            client: Fake Redis client backing this cache.
        """
        self.client = _FakeCacheClientWrapper(client)


@pytest.fixture
def fake_redis() -> Any:
    """Install a fake Redis-backed cache for the billing lock.

    Yields:
        Tuple of (FakeLock, FakeRedisClient) for assertions.
    """
    events: list[str] = []
    lock = FakeLock(events)
    client = FakeRedisClient(lock)
    with patch("webhooks.services.billing.cache", FakeCache(client)):
        yield lock, client


class TestTrialEndDateFromStripe:
    """Finding 1: trial_end_date must come from Stripe's trial_end."""

    @pytest.mark.django_db
    def test_trial_extension_reflected_in_trial_end_date(
        self, workspace: Workspace
    ) -> None:
        """Support extends a trial: trial_end_date reflects trial_end,
        not current_period_end, so the expiry job doesn't churn the
        customer days early."""
        subscription = {
            "id": "sub_trial",
            "status": "trialing",
            "current_period_end": PERIOD_END,
            "trial_end": EXTENDED_TRIAL_END,
            "items": [{"product_name": "Notipus Pro Plan"}],
        }

        with _mock_stripe_api([subscription]):
            assert BillingService.sync_workspace_from_stripe(CUSTOMER_ID) is True

        workspace.refresh_from_db()
        assert workspace.subscription_status == "trial"
        assert workspace.trial_end_date == datetime.fromtimestamp(
            EXTENDED_TRIAL_END, tz=timezone.utc
        )

    @pytest.mark.django_db
    def test_trial_end_falls_back_to_period_end_when_absent(
        self, workspace: Workspace
    ) -> None:
        """Without an explicit trial_end, the period end is still used."""
        subscription = {
            "id": "sub_trial",
            "status": "trialing",
            "current_period_end": PERIOD_END,
            "trial_end": None,
            "items": [{"product_name": "Notipus Pro Plan"}],
        }

        with _mock_stripe_api([subscription]):
            BillingService.sync_workspace_from_stripe(CUSTOMER_ID)

        workspace.refresh_from_db()
        assert workspace.trial_end_date == datetime.fromtimestamp(
            PERIOD_END, tz=timezone.utc
        )


class TestBillingCycleAnchorConsistency:
    """Finding 2: all handlers anchor on current_period_end."""

    @pytest.mark.django_db
    def test_late_subscription_created_does_not_regress_anchor(
        self, workspace: Workspace
    ) -> None:
        """A retried subscription.created arriving after
        subscription.updated must not rewind billing_cycle_anchor to the
        (past) period start."""
        # subscription.updated already recorded the next renewal
        Workspace.objects.filter(id=workspace.id).update(
            billing_cycle_anchor=PERIOD_END
        )

        with patch.object(BillingService, "sync_workspace_from_stripe"):
            BillingService.handle_subscription_created(
                {
                    "id": "sub_main",
                    "customer": CUSTOMER_ID,
                    "status": "active",
                    "current_period_start": PERIOD_START,
                    "current_period_end": PERIOD_END,
                }
            )

        workspace.refresh_from_db()
        assert workspace.billing_cycle_anchor == PERIOD_END

    @pytest.mark.django_db
    def test_created_without_period_end_leaves_anchor_untouched(
        self, workspace: Workspace
    ) -> None:
        """A payload lacking current_period_end must not clobber an
        existing anchor with None or a past timestamp."""
        Workspace.objects.filter(id=workspace.id).update(
            billing_cycle_anchor=PERIOD_END
        )

        with patch.object(BillingService, "sync_workspace_from_stripe"):
            BillingService.handle_subscription_created(
                {
                    "id": "sub_main",
                    "customer": CUSTOMER_ID,
                    "status": "active",
                    "current_period_start": PERIOD_START,
                }
            )

        workspace.refresh_from_db()
        assert workspace.billing_cycle_anchor == PERIOD_END

    def test_extract_billing_anchor_uses_period_end(self) -> None:
        """The shared helper returns current_period_end, never start."""
        sub_data = {
            "current_period_start": PERIOD_START,
            "current_period_end": PERIOD_END,
        }
        assert BillingService._extract_billing_anchor(sub_data) == PERIOD_END
        assert BillingService._extract_billing_anchor({}) is None


class TestIncompleteStatusMapping:
    """Findings 3+4: incomplete must not grant trial/active access."""

    def test_incomplete_maps_to_suspended(self) -> None:
        """Stripe 'incomplete' (first invoice unpaid) maps to suspended,
        a non-trial, non-active state."""
        assert STRIPE_STATUS_MAPPING["incomplete"] == "suspended"

    def test_incomplete_expired_maps_to_cancelled(self) -> None:
        """Stripe 'incomplete_expired' remains terminal."""
        assert STRIPE_STATUS_MAPPING["incomplete_expired"] == "cancelled"

    @pytest.mark.django_db
    def test_incomplete_subscription_not_active_not_trial(
        self, workspace: Workspace
    ) -> None:
        """subscription.created with status=incomplete must not leave the
        workspace active or on trial — the customer never paid."""
        with patch.object(BillingService, "sync_workspace_from_stripe"):
            BillingService.handle_subscription_created(
                {
                    "id": "sub_incomplete",
                    "customer": CUSTOMER_ID,
                    "status": "incomplete",
                    "current_period_end": PERIOD_END,
                }
            )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "suspended"
        assert workspace.is_active is False
        assert workspace.is_trial is False

    @pytest.mark.django_db
    def test_sync_with_incomplete_subscription_suspends_workspace(
        self, workspace: Workspace
    ) -> None:
        """Sync from Stripe also lands on suspended (not trial) when the
        only subscription is incomplete."""
        subscription = {
            "id": "sub_incomplete",
            "status": "incomplete",
            "current_period_end": PERIOD_END,
            "items": [{"product_name": "Notipus Pro Plan"}],
        }

        with _mock_stripe_api([subscription]):
            BillingService.sync_workspace_from_stripe(CUSTOMER_ID)

        workspace.refresh_from_db()
        assert workspace.subscription_status == "suspended"
        assert workspace.is_active is False
        assert workspace.is_trial is False

    @pytest.mark.django_db
    def test_created_respects_actual_status(self, workspace: Workspace) -> None:
        """subscription.created passes the real Stripe status through the
        mapping instead of hardcoding active."""
        with patch.object(BillingService, "sync_workspace_from_stripe"):
            BillingService.handle_subscription_created(
                {
                    "id": "sub_trial",
                    "customer": CUSTOMER_ID,
                    "status": "trialing",
                    "current_period_end": PERIOD_END,
                }
            )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "trial"


class TestStripeSyncLock:
    """Finding 5: write-then-sync handlers serialize per customer."""

    @pytest.mark.django_db
    def test_handler_acquires_and_releases_lock_around_sync(
        self, workspace: Workspace, fake_redis: Any
    ) -> None:
        """The whole write-then-sync body runs inside the lock."""
        lock, client = fake_redis

        def record_sync(customer_id: str) -> bool:
            lock.events.append("sync")
            return True

        with patch.object(
            BillingService, "sync_workspace_from_stripe", side_effect=record_sync
        ):
            BillingService.handle_subscription_updated(
                {
                    "id": "sub_main",
                    "customer": CUSTOMER_ID,
                    "status": "active",
                    "current_period_end": PERIOD_END,
                }
            )

        assert lock.events == ["acquire", "sync", "release"]
        assert client.lock_names == [f"stripe_sync_lock:{CUSTOMER_ID}"]

    @pytest.mark.django_db
    def test_concurrent_updated_webhooks_are_serialized(
        self, workspace: Workspace, fake_redis: Any
    ) -> None:
        """While webhook A holds the lock, webhook B cannot interleave —
        it raises so the view 5xxs and Stripe redelivers after A frees
        the lock, instead of A's stale sync overwriting B's newer plan."""
        lock, _client = fake_redis

        def sync_while_holding_lock(customer_id: str) -> bool:
            # Simulate webhook B arriving while A is still inside its
            # write-then-sync critical section.
            with pytest.raises(StripeSyncLockTimeout):
                BillingService.handle_subscription_updated(
                    {
                        "id": "sub_main",
                        "customer": CUSTOMER_ID,
                        "status": "past_due",
                        "current_period_end": PERIOD_END,
                    }
                )
            return True

        with patch.object(
            BillingService,
            "sync_workspace_from_stripe",
            side_effect=sync_while_holding_lock,
        ):
            BillingService.handle_subscription_updated(
                {
                    "id": "sub_main",
                    "customer": CUSTOMER_ID,
                    "status": "active",
                    "current_period_end": PERIOD_END,
                }
            )

        # Webhook B never wrote: A's state won, B will be redelivered.
        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"
        assert "acquire-blocked" in lock.events

    @pytest.mark.django_db
    def test_lock_timeout_leaves_workspace_untouched(
        self, workspace: Workspace, fake_redis: Any
    ) -> None:
        """A handler that cannot get the lock performs no writes."""
        lock, _client = fake_redis
        lock.held = True  # someone else holds it

        with pytest.raises(StripeSyncLockTimeout):
            BillingService.handle_subscription_updated(
                {
                    "id": "sub_main",
                    "customer": CUSTOMER_ID,
                    "status": "canceled",
                    "current_period_end": PERIOD_END,
                }
            )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"

    def test_lock_fails_open_without_redis(self) -> None:
        """With a non-Redis cache backend (tests, local dev), the lock
        no-ops instead of blocking webhook processing."""
        with stripe_sync_lock(CUSTOMER_ID):
            pass  # DummyCache in test settings: must not raise


class TestSubscriptionDeletedScoping:
    """Finding 6: deletion only cancels the matching subscription."""

    @pytest.mark.django_db
    def test_deleting_addon_subscription_leaves_workspace_active(
        self, workspace: Workspace
    ) -> None:
        """Deleting one of two subscriptions (an add-on) must not cancel
        the workspace whose billing rides on the main subscription."""
        Workspace.objects.filter(id=workspace.id).update(
            stripe_subscription_id="sub_main"
        )

        with patch.object(BillingService, "sync_workspace_from_stripe") as mock_sync:
            BillingService.handle_subscription_deleted(
                {"id": "sub_addon", "customer": CUSTOMER_ID}
            )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"
        # Still reconciles against Stripe for good measure
        mock_sync.assert_called_once_with(CUSTOMER_ID)

    @pytest.mark.django_db
    def test_deleting_own_subscription_cancels_workspace(
        self, workspace: Workspace
    ) -> None:
        """Deleting the workspace's own subscription cancels it."""
        Workspace.objects.filter(id=workspace.id).update(
            stripe_subscription_id="sub_main"
        )

        with _mock_stripe_api([]):
            BillingService.handle_subscription_deleted(
                {"id": "sub_main", "customer": CUSTOMER_ID}
            )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "cancelled"

    @pytest.mark.django_db
    def test_deletion_resync_restores_remaining_live_subscription(
        self, workspace: Workspace
    ) -> None:
        """Even when the recorded subscription id matches, the follow-up
        sync reinstates the workspace if Stripe still shows another live
        subscription for the customer."""
        Workspace.objects.filter(id=workspace.id).update(
            stripe_subscription_id="sub_main"
        )
        remaining = {
            "id": "sub_other",
            "status": "active",
            "current_period_end": PERIOD_END,
            "items": [{"product_name": "Notipus Pro Plan"}],
        }

        with _mock_stripe_api([remaining]):
            BillingService.handle_subscription_deleted(
                {"id": "sub_main", "customer": CUSTOMER_ID}
            )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"
        assert workspace.stripe_subscription_id == "sub_other"


class TestPaymentFailedScoping:
    """Finding 7: only the workspace's own invoice sets past_due."""

    @pytest.mark.django_db
    def test_one_off_failed_invoice_does_not_set_past_due(
        self, workspace: Workspace
    ) -> None:
        """An ad-hoc/portal invoice (no subscription) failing must not
        push the workspace into past_due — nothing would ever restore
        it when the invoice is paid, voided, or forgotten."""
        BillingService.handle_payment_failed({"customer": CUSTOMER_ID})

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"

    @pytest.mark.django_db
    def test_failed_invoice_for_other_subscription_ignored(
        self, workspace: Workspace
    ) -> None:
        """A failed invoice for a different subscription on the same
        customer does not affect the workspace."""
        Workspace.objects.filter(id=workspace.id).update(
            stripe_subscription_id="sub_main"
        )

        BillingService.handle_payment_failed(
            {"customer": CUSTOMER_ID, "subscription": "sub_addon"}
        )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"

    @pytest.mark.django_db
    def test_failed_invoice_for_own_subscription_sets_past_due(
        self, workspace: Workspace
    ) -> None:
        """The workspace's own subscription invoice failing does mark it
        past_due."""
        Workspace.objects.filter(id=workspace.id).update(
            stripe_subscription_id="sub_main"
        )

        BillingService.handle_payment_failed(
            {"customer": CUSTOMER_ID, "subscription": "sub_main"}
        )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "past_due"

    @pytest.mark.django_db
    def test_expanded_subscription_object_is_matched(
        self, workspace: Workspace
    ) -> None:
        """An expanded subscription object (dict) is matched by its id."""
        Workspace.objects.filter(id=workspace.id).update(
            stripe_subscription_id="sub_main"
        )

        BillingService.handle_payment_failed(
            {"customer": CUSTOMER_ID, "subscription": {"id": "sub_main"}}
        )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "past_due"


class TestCheckoutPlanNormalization:
    """Finding 8: metadata.plan_name is normalized, never verbatim."""

    @pytest.mark.django_db
    def test_display_plan_name_normalizes_to_choice_key(
        self, workspace: Workspace
    ) -> None:
        """plan_name="Enterprise Plan" persists as "enterprise"."""
        with patch.object(BillingService, "sync_workspace_from_stripe"):
            BillingService.handle_checkout_completed(
                {
                    "customer": CUSTOMER_ID,
                    "subscription": "sub_new",
                    "metadata": {"plan_name": "Enterprise Plan"},
                }
            )

        workspace.refresh_from_db()
        assert workspace.subscription_plan == "enterprise"
        assert workspace.stripe_subscription_id == "sub_new"

    @pytest.mark.django_db
    def test_unrecognized_plan_name_is_rejected_with_warning(
        self, workspace: Workspace, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Arbitrary metadata strings never reach subscription_plan."""
        with (
            patch.object(BillingService, "sync_workspace_from_stripe"),
            caplog.at_level(logging.WARNING, logger="webhooks.services.billing"),
        ):
            BillingService.handle_checkout_completed(
                {
                    "customer": CUSTOMER_ID,
                    "subscription": "sub_new",
                    "metadata": {"plan_name": "totally-made-up"},
                }
            )

        workspace.refresh_from_db()
        assert workspace.subscription_plan == "free"  # unchanged
        assert any(
            "unrecognized plan_name" in record.message.lower()
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Enterprise Plan", "enterprise"),
            ("Notipus Pro Plan", "pro"),
            ("PRO", "pro"),
            ("basic", "basic"),
            ("free", "free"),
            ("Hacker Plan", None),
            ("price_1NvT4OJADkUcvxXxx", None),
        ],
    )
    def test_normalize_plan_name(self, raw: str, expected: str | None) -> None:
        """Normalization maps display strings to plan keys and rejects
        anything unknown."""
        assert BillingService._normalize_plan_name(raw) == expected

    def test_sync_plan_extraction_rejects_unknown_product_names(self) -> None:
        """The sync path also refuses to persist unknown plan strings."""
        subscription = {
            "id": "sub_x",
            "items": [{"product_name": "Some Random Product"}],
        }
        assert BillingService._extract_plan_name_from_subscription(subscription) is None

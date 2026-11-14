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


class TestSyncPrimarySubscription:
    """Sync derives state from the stored primary plan subscription."""

    def test_addon_does_not_mask_cancelled_primary(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """A live add-on can't keep a workspace whose plan was cancelled."""
        stripe_subs.return_value = [
            _sub("sub_addon", "active", product="Extra seats"),
            _sub("sub_primary", "canceled"),
        ]

        BillingService.handle_subscription_deleted(
            {"id": "sub_primary", "customer": CUSTOMER_ID}
        )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "cancelled"
        assert workspace.stripe_subscription_id == "sub_primary"

    def test_stored_live_subscription_wins(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """With two live plan subscriptions, the stored one is used."""
        stripe_subs.return_value = [
            _sub("sub_other", "active", product="Basic"),
            _sub("sub_primary", "past_due", product="Pro"),
        ]

        assert BillingService.sync_workspace_from_stripe(CUSTOMER_ID) is True

        workspace.refresh_from_db()
        assert workspace.subscription_status == "past_due"
        assert workspace.subscription_plan == "pro"
        assert workspace.stripe_subscription_id == "sub_primary"

    def test_resubscribe_replaces_ended_stored_subscription(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """A new live plan subscription replaces a cancelled stored one."""
        stripe_subs.return_value = [
            _sub("sub_primary", "canceled", product="Pro"),
            _sub("sub_new", "active", product="Basic"),
        ]

        BillingService.sync_workspace_from_stripe(CUSTOMER_ID)

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"
        assert workspace.subscription_plan == "basic"
        assert workspace.stripe_subscription_id == "sub_new"


class TestStripeErrorsDuringSync:
    """Stripe outages fail the webhook instead of reporting success."""

    def test_sync_requests_raising_and_propagates(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """The sync asks for raise_on_error and lets StripeError escape."""
        stripe_subs.side_effect = stripe.APIConnectionError("down")

        with pytest.raises(stripe.StripeError):
            BillingService.sync_workspace_from_stripe(CUSTOMER_ID)

        assert stripe_subs.call_args.kwargs["raise_on_error"] is True

    def test_stripe_outage_fails_handler(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """A handler whose sync hits a Stripe outage raises (webhook 5xx)."""
        stripe_subs.side_effect = stripe.APIConnectionError("down")

        with pytest.raises(stripe.StripeError):
            BillingService.handle_subscription_updated(
                {"id": "sub_primary", "customer": CUSTOMER_ID, "status": "active"}
            )


class TestCheckoutCompletedStatus:
    """checkout.session.completed writes the status the session proves."""

    def _complete(self, workspace: Workspace, **session: Any) -> None:
        """Run the checkout handler with the sync stubbed out.

        Args:
            workspace: Workspace the session belongs to.
            **session: Extra session fields.
        """
        payload = {
            "customer": CUSTOMER_ID,
            "metadata": {"workspace_id": str(workspace.id)},
            **session,
        }
        with (
            patch.object(BillingService, "sync_workspace_from_stripe"),
            patch.object(BillingService, "_warn_on_duplicate_live_subscriptions"),
        ):
            BillingService.handle_checkout_completed(payload)

    def test_trial_checkout_is_not_marked_active(self, workspace: Workspace) -> None:
        """A no-payment trial checkout leaves status to the sync."""
        Workspace.objects.filter(id=workspace.id).update(subscription_status="trial")

        self._complete(
            workspace, subscription="sub_primary", payment_status="no_payment_required"
        )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "trial"
        assert workspace.payment_method_added is True

    def test_expanded_subscription_status_is_used(self, workspace: Workspace) -> None:
        """An expanded subscription's real status is mapped and written."""
        self._complete(
            workspace,
            subscription={"id": "sub_new", "status": "trialing"},
            payment_status="paid",
        )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "trial"
        assert workspace.stripe_subscription_id == "sub_new"

    def test_paid_checkout_is_active(self, workspace: Workspace) -> None:
        """A paid session proves an active subscription."""
        Workspace.objects.filter(id=workspace.id).update(subscription_status="trial")

        self._complete(workspace, subscription="sub_primary", payment_status="paid")

        workspace.refresh_from_db()
        assert workspace.subscription_status == "active"


LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "billing-hardening-dedup",
    }
}


class TestRedeliveredSideEffects:
    """Redelivered billing events don't repeat non-idempotent side effects."""

    @pytest.fixture(autouse=True)
    def _locmem_cache(self) -> Iterator[None]:
        """Use a real (local-memory) cache so dedup markers persist."""
        from django.core.cache import cache
        from django.test import override_settings

        with override_settings(CACHES=LOCMEM_CACHE):
            cache.clear()
            yield
            cache.clear()

    def _trial_event(self) -> dict[str, Any]:
        """Build a trial_will_end subscription payload.

        Returns:
            Subscription dict for the test workspace.
        """
        return {"id": "sub_primary", "customer": CUSTOMER_ID, "trial_end": 1900000000}

    def test_trial_ending_email_sent_once(self, workspace: Workspace) -> None:
        """A redelivered trial_will_end doesn't re-send the email."""
        with patch("webhooks.services.billing.send_trial_ending_alert") as send:
            BillingService.handle_trial_ending(self._trial_event())
            BillingService.handle_trial_ending(self._trial_event())

        assert send.call_count == 1

    def test_new_trial_end_sends_again(self, workspace: Workspace) -> None:
        """An extended trial (new trial_end) is a new occurrence."""
        with patch("webhooks.services.billing.send_trial_ending_alert") as send:
            BillingService.handle_trial_ending(self._trial_event())
            BillingService.handle_trial_ending(
                {**self._trial_event(), "trial_end": 1900600000}
            )

        assert send.call_count == 2

    def test_failed_attempt_is_not_marked(self, workspace: Workspace) -> None:
        """A send that raised is retried on redelivery."""
        with patch("webhooks.services.billing.send_trial_ending_alert") as send:
            send.side_effect = [RuntimeError("smtp down"), None]
            with pytest.raises(RuntimeError):
                BillingService.handle_trial_ending(self._trial_event())
            BillingService.handle_trial_ending(self._trial_event())

        assert send.call_count == 2

    def test_cancellation_tracked_once(
        self, workspace: Workspace, stripe_subs: MagicMock
    ) -> None:
        """A redelivered deletion doesn't count a second cancellation."""
        stripe_subs.return_value = [_sub("sub_primary", "canceled")]
        event = {"id": "sub_primary", "customer": CUSTOMER_ID}

        with patch("webhooks.services.billing.analytics.track_workspace_event") as t:
            BillingService.handle_subscription_deleted(event)
            BillingService.handle_subscription_deleted(event)

        cancels = [c for c in t.call_args_list if c.args[1] == "subscription_cancelled"]
        assert len(cancels) == 1


@pytest.fixture
def checkout_client(db: None) -> Iterator[tuple[Any, Workspace]]:
    """Logged-in owner of a workspace with an existing Stripe customer.

    Uses a local-memory cache so the checkout nonce persists between
    requests.

    Yields:
        (client, workspace).
    """
    from core.models import Plan, WorkspaceMember
    from django.contrib.auth.models import User
    from django.core.cache import cache
    from django.test import Client, override_settings

    user = User.objects.create_user(username="checkout-owner", password="x")
    ws = Workspace.objects.create(
        name="Checkout Workspace",
        stripe_customer_id=CUSTOMER_ID,
        subscription_plan="free",
        subscription_status="active",
    )
    WorkspaceMember.objects.create(user=user, workspace=ws, role="owner")
    for name, price in (("basic", 29), ("pro", 99)):
        Plan.objects.update_or_create(
            name=name,
            defaults={
                "display_name": name.title(),
                "price_monthly": price,
                "is_active": True,
                "stripe_price_id_monthly": f"price_{name}_monthly",
            },
        )
    client = Client()
    client.force_login(user)
    with override_settings(CACHES=LOCMEM_CACHE):
        cache.clear()
        yield client, ws
        cache.clear()


def start_checkout(client: Any, plan: str, interval: str = "monthly") -> Any:
    """Start a checkout the way the upgrade page's form does.

    Args:
        client: Logged-in test client.
        plan: Plan name.
        interval: Billing interval.

    Returns:
        The response.
    """
    from django.urls import reverse

    return client.post(reverse("core:checkout", args=[plan]), {"interval": interval})


@pytest.fixture
def checkout_stripe() -> Iterator[MagicMock]:
    """Patch the StripeAPI methods the checkout view calls.

    Yields:
        The patched StripeAPI class; configure ``return_value`` methods.
    """
    with patch("core.services.stripe.StripeAPI") as api_cls:
        api = api_cls.return_value
        api.get_or_create_customer.return_value = {"id": CUSTOMER_ID}
        api.has_live_subscription.return_value = False
        api.get_price_by_lookup_key.return_value = None
        api.reuse_or_expire_open_checkout_sessions.return_value = (None, [])
        api.create_checkout_session.return_value = {
            "id": "cs_1",
            "url": "https://checkout.stripe.com/c/cs_1",
            "status": "open",
        }
        yield api_cls


class TestRepeatCheckout:
    """A repeat checkout attempt always lands on a working session."""

    def test_open_session_for_same_plan_is_reused(
        self, checkout_client: tuple[Any, Workspace], checkout_stripe: MagicMock
    ) -> None:
        """The customer is sent back to their open session, not a new one."""
        client, _ws = checkout_client
        api = checkout_stripe.return_value
        api.reuse_or_expire_open_checkout_sessions.return_value = (
            "https://checkout.stripe.com/c/cs_open",
            [],
        )

        response = start_checkout(client, "pro")

        assert response.status_code == 302
        assert response.url == "https://checkout.stripe.com/c/cs_open"
        api.create_checkout_session.assert_not_called()

    def test_double_click_shares_idempotency_key(
        self, checkout_client: tuple[Any, Workspace], checkout_stripe: MagicMock
    ) -> None:
        """Back-to-back attempts reuse one key so Stripe dedups them."""
        client, _ws = checkout_client
        api = checkout_stripe.return_value

        start_checkout(client, "pro")
        start_checkout(client, "pro")

        keys = [
            c.kwargs["idempotency_key"] for c in api.create_checkout_session.mock_calls
        ]
        assert len(keys) == 2 and keys[0] == keys[1]

    def test_expired_session_key_is_not_reused(
        self, checkout_client: tuple[Any, Workspace], checkout_stripe: MagicMock
    ) -> None:
        """After its session is expired, the next attempt gets a new key."""
        client, _ws = checkout_client
        api = checkout_stripe.return_value

        start_checkout(client, "pro")
        api.reuse_or_expire_open_checkout_sessions.return_value = (
            None,
            [("pro", "monthly")],
        )
        start_checkout(client, "basic")
        api.reuse_or_expire_open_checkout_sessions.return_value = (
            None,
            [("basic", "monthly")],
        )
        start_checkout(client, "pro")

        keys = [
            c.kwargs["idempotency_key"] for c in api.create_checkout_session.mock_calls
        ]
        assert keys[0] != keys[2]
        assert keys[0].endswith("-pro-monthly") and keys[2].endswith("-pro-monthly")

    def test_replayed_dead_session_is_retried(
        self, checkout_client: tuple[Any, Workspace], checkout_stripe: MagicMock
    ) -> None:
        """A replayed expired session is replaced by a fresh one."""
        client, _ws = checkout_client
        api = checkout_stripe.return_value
        api.create_checkout_session.side_effect = [
            {
                "id": "cs_old",
                "url": "https://checkout.stripe.com/old",
                "status": "expired",
            },
            {
                "id": "cs_new",
                "url": "https://checkout.stripe.com/new",
                "status": "open",
            },
        ]

        response = start_checkout(client, "pro")

        assert response.url == "https://checkout.stripe.com/new"
        first, second = api.create_checkout_session.mock_calls
        assert first.kwargs["idempotency_key"] != second.kwargs["idempotency_key"]

    def test_reuse_or_expire_keeps_matching_session(self) -> None:
        """Only the open session for the same plan and interval survives."""
        from core.services.stripe import StripeAPI

        def _session(sid: str, plan: str, interval: str) -> Any:
            return stripe.checkout.Session.construct_from(
                {
                    "id": sid,
                    "url": f"https://checkout.stripe.com/{sid}",
                    "metadata": {"plan_name": plan, "interval": interval},
                },
                "sk_test",
            )

        sessions = [
            _session("cs_a", "basic", "monthly"),
            _session("cs_b", "pro", "monthly"),
            _session("cs_c", "pro", "yearly"),
        ]
        with patch("core.services.stripe.stripe.checkout.Session") as session_cls:
            session_cls.list.return_value.auto_paging_iter.return_value = sessions
            url, expired = StripeAPI(
                api_key="sk_test"
            ).reuse_or_expire_open_checkout_sessions(CUSTOMER_ID, "pro", "monthly")

        assert url == "https://checkout.stripe.com/cs_b"
        assert expired == [("basic", "monthly"), ("pro", "yearly")]
        expired_ids = [c.args[0] for c in session_cls.expire.call_args_list]
        assert expired_ids == ["cs_a", "cs_c"]


class TestDuplicateSubscriptionGuard:
    """Unpaid, paused and incomplete subscriptions block a new checkout."""

    @pytest.mark.parametrize("status", ["unpaid", "paused", "incomplete"])
    def test_non_live_but_recoverable_status_counts(self, status: str) -> None:
        """has_live_subscription finds subscriptions in these statuses."""
        from core.services.stripe import StripeAPI

        def _list(**kwargs: Any) -> MagicMock:
            return MagicMock(
                data=[{"id": "sub_x"}] if kwargs["status"] == status else []
            )

        with patch("core.services.stripe.stripe.Subscription.list", side_effect=_list):
            assert StripeAPI(api_key="sk_test").has_live_subscription(CUSTOMER_ID)

    def test_unpaid_customer_sent_to_portal(
        self, checkout_client: tuple[Any, Workspace]
    ) -> None:
        """A customer with an unpaid subscription goes to the portal."""
        from core.services.stripe import StripeAPI

        client, _ws = checkout_client

        def _list(**kwargs: Any) -> MagicMock:
            unpaid = kwargs["status"] == "unpaid"
            return MagicMock(data=[{"id": "sub_x"}] if unpaid else [])

        with (
            patch.object(
                StripeAPI, "get_or_create_customer", return_value={"id": CUSTOMER_ID}
            ),
            patch.object(StripeAPI, "create_checkout_session") as create,
            patch("core.services.stripe.stripe.Subscription.list", side_effect=_list),
        ):
            response = start_checkout(client, "pro")

        assert response.status_code == 302
        assert response.url.endswith("/billing/portal/")
        create.assert_not_called()


class TestCheckoutRequiresPost:
    """Checkout is a state-changing POST protected by CSRF."""

    def test_get_is_rejected(
        self, checkout_client: tuple[Any, Workspace], checkout_stripe: MagicMock
    ) -> None:
        """A GET (link, image, prefetch) can't start a checkout."""
        from django.urls import reverse

        client, _ws = checkout_client
        response = client.get(reverse("core:checkout", args=["pro"]))

        assert response.status_code == 405
        checkout_stripe.return_value.get_or_create_customer.assert_not_called()

    def test_post_without_csrf_token_is_rejected(
        self, checkout_client: tuple[Any, Workspace], checkout_stripe: MagicMock
    ) -> None:
        """A cross-site POST without the CSRF token is refused."""
        from django.test import Client
        from django.urls import reverse

        _client, ws = checkout_client
        user = ws.members.get().user
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(user)

        response = strict.post(reverse("core:checkout", args=["pro"]))

        assert response.status_code == 403
        checkout_stripe.return_value.get_or_create_customer.assert_not_called()

    def test_upgrade_page_posts_checkout_form(
        self, checkout_client: tuple[Any, Workspace], checkout_stripe: MagicMock
    ) -> None:
        """The upgrade page renders checkout as a POST form with a token."""
        from django.urls import reverse

        client, _ws = checkout_client
        checkout_stripe.return_value.list_prices.return_value = []
        html = client.get(reverse("core:upgrade_plan")).content.decode()

        action = reverse("core:checkout", args=["pro"])
        assert f'action="{action}"' in html
        assert 'method="post"' in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="interval"' in html
        assert f'href="{action}' not in html


class TestDisableBilling:
    """DISABLE_BILLING turns off every billing restriction, not just the hook."""

    @pytest.fixture
    def billing_disabled(self, settings: Any) -> None:
        """Run the test as a self-hosted deployment without billing."""
        settings.DISABLE_BILLING = True

    def test_expired_trial_keeps_access(
        self, workspace: Workspace, billing_disabled: None
    ) -> None:
        """A long-expired trial (or lapsed status) still has access."""
        from datetime import timedelta

        from django.utils import timezone

        workspace.subscription_status = "trial"
        workspace.trial_end_date = timezone.now() - timedelta(days=30)
        assert workspace.has_active_access is True
        workspace.subscription_status = "cancelled"
        assert workspace.has_active_access is True

    def test_expired_trial_blocked_when_billing_enabled(
        self, workspace: Workspace
    ) -> None:
        """Hosted behaviour is unchanged: an expired trial loses access."""
        from datetime import timedelta

        from django.utils import timezone

        workspace.subscription_status = "trial"
        workspace.trial_end_date = timezone.now() - timedelta(days=30)
        assert workspace.has_active_access is False

    def test_free_quota_is_unlimited(
        self, workspace: Workspace, billing_disabled: None
    ) -> None:
        """The free-plan monthly limit doesn't apply."""
        from webhooks.services.rate_limiter import RateLimiter

        workspace.subscription_plan = "free"
        limiter = RateLimiter()
        assert limiter.get_organization_limit(workspace) == RateLimiter.UNLIMITED
        with patch.object(limiter, "_cache_get_with_health", return_value=(5000, True)):
            allowed, _info = limiter.check_rate_limit(workspace)
        assert allowed is True

    def test_webhook_for_expired_trial_is_accepted(
        self, workspace: Workspace, billing_disabled: None
    ) -> None:
        """The webhook access check no longer 403s an expired trial."""
        from datetime import timedelta

        from django.utils import timezone
        from webhooks.webhook_router import _check_workspace_access

        workspace.subscription_status = "trial"
        workspace.trial_end_date = timezone.now() - timedelta(days=30)
        assert _check_workspace_access(workspace) is None

    @pytest.mark.parametrize(
        ("view_name", "args"),
        [
            ("core:billing_dashboard", []),
            ("core:upgrade_plan", []),
            ("core:billing_portal", []),
            ("core:payment_methods", []),
            ("core:billing_history", []),
            ("core:select_plan", []),
            ("core:checkout_success", []),
            ("core:checkout_cancel", []),
        ],
    )
    def test_billing_pages_redirect_to_dashboard(
        self,
        checkout_client: tuple[Any, Workspace],
        billing_disabled: None,
        view_name: str,
        args: list[str],
    ) -> None:
        """Billing pages send the user to the dashboard."""
        from django.urls import reverse

        client, _ws = checkout_client
        response = client.get(reverse(view_name, args=args))

        assert response.status_code == 302
        assert response.url == reverse("core:dashboard")

    def test_checkout_post_redirects_to_dashboard(
        self,
        checkout_client: tuple[Any, Workspace],
        checkout_stripe: MagicMock,
        billing_disabled: None,
    ) -> None:
        """Checkout can't be started at all."""
        from django.urls import reverse

        client, _ws = checkout_client
        response = start_checkout(client, "pro")

        assert response.url == reverse("core:dashboard")
        checkout_stripe.return_value.get_or_create_customer.assert_not_called()

    def test_billing_nav_hidden(
        self, checkout_client: tuple[Any, Workspace], billing_disabled: None
    ) -> None:
        """Dashboard and settings show no billing links."""
        from django.urls import reverse

        client, _ws = checkout_client
        for page in ("core:dashboard", "core:workspace_settings"):
            response = client.get(reverse(page))
            assert response.status_code == 200, page
            html = response.content.decode()
            assert reverse("core:billing_dashboard") not in html, page
            assert reverse("core:upgrade_plan") not in html, page

    def test_billing_nav_shown_when_enabled(
        self, checkout_client: tuple[Any, Workspace]
    ) -> None:
        """Hosted deployments keep the billing links."""
        from django.urls import reverse

        client, _ws = checkout_client
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert reverse("core:billing_dashboard") in response.content.decode()


class TestAuditCommandAmount:
    """audit_duplicate_subscriptions prints the subscription's real amount."""

    def _sdk_sub(self, unit_amount: int, currency: str) -> Any:
        """Build a Stripe SDK subscription with one priced item.

        Args:
            unit_amount: Price in minor units.
            currency: Lower-case currency code.

        Returns:
            A stripe.Subscription object.
        """
        return stripe.Subscription.construct_from(
            {
                "id": "sub_dup",
                "status": "active",
                "created": 1700000000,
                "items": {
                    "object": "list",
                    "data": [
                        {
                            "id": "si_1",
                            "price": {
                                "id": "price_1",
                                "unit_amount": unit_amount,
                                "currency": currency,
                            },
                        }
                    ],
                },
            },
            "sk_test",
        )

    def test_amount_is_read_from_items(self) -> None:
        """A USD price prints in major units, not "?"."""
        from core.management.commands.audit_duplicate_subscriptions import Command

        assert Command._compact_sub(self._sdk_sub(2900, "usd"))["amount"] == (
            "29.00 USD"
        )

    def test_zero_decimal_currency(self) -> None:
        """JPY amounts are not divided by 100."""
        from core.management.commands.audit_duplicate_subscriptions import Command

        assert Command._compact_sub(self._sdk_sub(1000, "jpy"))["amount"] == (
            "1000 JPY"
        )

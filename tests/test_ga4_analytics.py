"""Tests for server-side GA4 analytics (core.analytics).

Covers the Measurement Protocol payload building, the PII filter
(hashed user ids, page-location sanitizing, email redaction), the
page-view middleware, and the SaaS funnel events emitted by views,
signals, and the Stripe billing sync.
"""

import time
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from core import analytics
from core.models import Plan, Workspace, WorkspaceMember
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

GA4_TEST_SETTINGS = {
    "GA4_MEASUREMENT_ID": "G-TEST123",
    "GA4_API_SECRET": "test-secret",
}


@pytest.fixture
def ga4(request: pytest.FixtureRequest) -> Generator[MagicMock, None, None]:
    """Enable GA4 settings and capture submitted payloads.

    Yields:
        Mock standing in for analytics._submit; payloads are inspected
        via ``ga4.call_args_list``.
    """
    with override_settings(**GA4_TEST_SETTINGS):
        with patch.object(analytics, "_submit") as mock_submit:
            yield mock_submit


def _payloads(mock_submit: MagicMock) -> list[dict[str, Any]]:
    """Return all payloads captured by the ga4 fixture."""
    return [call.args[0] for call in mock_submit.call_args_list]


def _events_named(mock_submit: MagicMock, name: str) -> list[dict[str, Any]]:
    """Return all captured events with the given GA4 event name."""
    return [
        event
        for payload in _payloads(mock_submit)
        for event in payload["events"]
        if event["name"] == name
    ]


@pytest.fixture
def user(db: None) -> User:
    """Create a test user."""
    return User.objects.create_user(
        username="tracked@example.com",
        email="tracked@example.com",
        password="test-password-123",
    )


@pytest.fixture
def logged_in_client(client: Client, user: User) -> Client:
    """Return a test client with the test user logged in."""
    client.force_login(user)
    return client


class TestPiiFilter:
    """The PII filter: nothing personally identifying reaches Google."""

    def test_hashed_user_id_is_stable_and_not_raw(self, user: User) -> None:
        """The GA4 user id is a stable hash, never the pk or email."""
        hashed = analytics.hashed_user_id(user)
        assert hashed == analytics.hashed_user_id(user)
        assert len(hashed) == 64
        assert str(user.pk) != hashed
        assert user.email not in hashed

    def test_hashed_user_id_uses_dedicated_salt(self, user: User) -> None:
        """Changing the salt changes the hash (salt actually applies)."""
        baseline = analytics.hashed_user_id(user)
        with override_settings(GA4_USER_ID_SALT="other-salt"):
            assert analytics.hashed_user_id(user) != baseline

    def test_sanitize_page_location_drops_secret_params(self) -> None:
        """Tokens and emails are stripped; campaign params survive."""
        url = (
            "https://notipus.com/invite/?token=secret123"
            "&email=a@b.com&utm_source=news&utm_campaign=launch"
        )
        sanitized = analytics.sanitize_page_location(url)
        assert "secret123" not in sanitized
        assert "a@b.com" not in sanitized
        assert "utm_source=news" in sanitized
        assert "utm_campaign=launch" in sanitized
        assert sanitized.startswith("https://notipus.com/invite/")

    def test_event_params_redact_email_shapes(self) -> None:
        """Email-shaped strings in params are redacted before sending."""
        params = analytics._build_event_params(
            {"plan": "pro", "note": "contact me at user@example.com"}, None
        )
        assert params["plan"] == "pro"
        assert "user@example.com" not in params["note"]
        assert "[redacted]" in params["note"]


class TestTrackEvent:
    """track_event payload construction."""

    def test_noop_when_unconfigured(self) -> None:
        """No payload is submitted when GA4 settings are empty."""
        request = RequestFactory().get("/")
        with patch.object(analytics, "_submit") as mock_submit:
            analytics.track_event(request, "sign_up", {"method": "email"})
        mock_submit.assert_not_called()

    def test_payload_shape(self, ga4: MagicMock, user: User) -> None:
        """Client id, hashed user id, session id and engagement land."""
        request = RequestFactory().get("/")
        request.COOKIES[analytics.CLIENT_ID_COOKIE] = "12345.67890"
        request.session = {}  # type: ignore[assignment]
        request.user = user

        analytics.track_event(request, "select_plan", {"plan": "pro"})

        (payload,) = _payloads(ga4)
        assert payload["client_id"] == "12345.67890"
        assert payload["user_id"] == analytics.hashed_user_id(user)
        (event,) = payload["events"]
        assert event["name"] == "select_plan"
        assert event["params"]["plan"] == "pro"
        assert event["params"]["engagement_time_msec"] == 100
        assert event["params"]["session_id"]

    def test_prefers_ga_cookie_for_stitching(self, ga4: MagicMock) -> None:
        """A gtag.js _ga cookie wins over our first-party cookie."""
        request = RequestFactory().get("/")
        request.COOKIES["_ga"] = "GA1.1.111.222"
        request.COOKIES[analytics.CLIENT_ID_COOKIE] = "999.888"

        analytics.track_event(request, "page_view")

        (payload,) = _payloads(ga4)
        assert payload["client_id"] == "111.222"

    def test_workspace_event_uses_workspace_uuid(
        self, ga4: MagicMock, db: None
    ) -> None:
        """Webhook-driven events use the workspace uuid as client id."""
        workspace = Workspace.objects.create(name="Acme")
        analytics.track_workspace_event(
            workspace, "plan_change", {"previous_plan": "free", "new_plan": "pro"}
        )

        (payload,) = _payloads(ga4)
        assert payload["client_id"] == str(workspace.uuid)
        assert "user_id" not in payload
        (event,) = payload["events"]
        assert event["params"]["new_plan"] == "pro"


class TestMiddleware:
    """GA4Middleware: cookie minting and server-side page views."""

    def test_page_view_tracked_and_cookie_set(self, ga4: MagicMock, db: None) -> None:
        """An HTML GET mints the client-id cookie and sends page_view."""
        client = Client()
        response = client.get(reverse("account_login"), HTTP_USER_AGENT="Mozilla/5.0")
        assert response.status_code == 200
        assert analytics.CLIENT_ID_COOKIE in response.cookies

        (event,) = _events_named(ga4, "page_view")
        assert event["params"]["page_location"].endswith(reverse("account_login"))

    def test_bot_requests_not_tracked(self, ga4: MagicMock, db: None) -> None:
        """Crawlers get neither page views nor a client-id cookie."""
        client = Client()
        bot = client.get(reverse("account_login"), HTTP_USER_AGENT="Googlebot/2.1")
        no_ua = client.get(reverse("account_login"))  # no UA at all
        assert _events_named(ga4, "page_view") == []
        assert analytics.CLIENT_ID_COOKIE not in bot.cookies
        assert analytics.CLIENT_ID_COOKIE not in no_ua.cookies

    def test_untracked_responses_get_no_cookie(self, ga4: MagicMock, db: None) -> None:
        """No Set-Cookie on responses that aren't tracked page views."""
        client = Client()
        response = client.post(
            reverse("account_login"), {}, HTTP_USER_AGENT="Mozilla/5.0"
        )
        assert analytics.CLIENT_ID_COOKIE not in response.cookies

    def test_should_track_page_view_filters(self) -> None:
        """Non-GET, non-HTML, error and excluded paths are skipped."""
        factory = RequestFactory()
        should_track = analytics.GA4Middleware._should_track_page_view

        def html_response(status: int = 200) -> Any:
            response = MagicMock()
            response.status_code = status
            response.get.return_value = "text/html; charset=utf-8"
            return response

        browser = {"HTTP_USER_AGENT": "Mozilla/5.0"}
        assert should_track(factory.get("/dashboard/", **browser), html_response())
        assert not should_track(factory.post("/dashboard/", **browser), html_response())
        assert not should_track(
            factory.get("/dashboard/", **browser), html_response(status=404)
        )
        assert not should_track(
            factory.get("/webhook/stripe/", **browser), html_response()
        )
        assert not should_track(factory.get("/admin/core/", **browser), html_response())

        json_response = MagicMock()
        json_response.status_code = 200
        json_response.get.return_value = "application/json"
        assert not should_track(factory.get("/dashboard/", **browser), json_response)


class TestAuthEvents:
    """sign_up and login events from the auth signals."""

    def test_login_signal_tracks_login(self, ga4: MagicMock, user: User) -> None:
        """Django's user_logged_in signal produces a login event."""
        request = RequestFactory().get("/")
        request.session = {}  # type: ignore[assignment]
        request.user = user
        analytics.set_login_method(request, "passkey")

        user_logged_in.send(sender=User, request=request, user=user)

        (event,) = _events_named(ga4, "login")
        assert event["params"]["method"] == "passkey"

    def test_allauth_signup_signal_tracks_sign_up(
        self, ga4: MagicMock, user: User
    ) -> None:
        """allauth's user_signed_up signal produces a sign_up event."""
        from allauth.account.signals import user_signed_up

        request = RequestFactory().get("/")
        request.session = {}  # type: ignore[assignment]
        request.user = user

        user_signed_up.send(sender=User, request=request, user=user)

        (event,) = _events_named(ga4, "sign_up")
        assert event["params"]["method"] == "email"


class TestFunnelEvents:
    """Funnel events emitted by the billing and workspace views."""

    def test_select_plan_tracked(
        self, ga4: MagicMock, logged_in_client: Client, db: None
    ) -> None:
        """A valid plan selection sends select_plan with the plan name."""
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 29,
                "is_active": True,
            },
        )
        response = logged_in_client.post(reverse("core:select_plan"), {"plan": "pro"})
        assert response.status_code == 302

        (event,) = _events_named(ga4, "select_plan")
        assert event["params"]["plan"] == "pro"

    def test_invalid_plan_not_tracked(
        self, ga4: MagicMock, logged_in_client: Client, db: None
    ) -> None:
        """Rejected plan selections must not produce funnel events."""
        logged_in_client.post(reverse("core:select_plan"), {"plan": "bogus"})
        assert _events_named(ga4, "select_plan") == []

    def test_workspace_created_tracked(
        self, ga4: MagicMock, logged_in_client: Client, db: None
    ) -> None:
        """Creating a workspace sends workspace_created with the plan."""
        response = logged_in_client.post(
            reverse("core:create_workspace"), {"name": "Acme"}
        )
        assert response.status_code == 302

        (event,) = _events_named(ga4, "workspace_created")
        assert event["params"]["plan"] == "free"

    def test_checkout_cancel_tracked(
        self, ga4: MagicMock, logged_in_client: Client, db: None
    ) -> None:
        """Landing on the checkout cancel page sends checkout_cancelled."""
        response = logged_in_client.get(reverse("core:checkout_cancel"))
        assert response.status_code == 200
        assert len(_events_named(ga4, "checkout_cancelled")) == 1

    def test_purchase_tracked_once(
        self, ga4: MagicMock, logged_in_client: Client, user: User, db: None
    ) -> None:
        """checkout_success sends purchase once, even when refreshed."""
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 29,
                "is_active": True,
            },
        )
        workspace = Workspace.objects.create(name="Acme", stripe_customer_id="cus_123")
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

        with patch("core.services.stripe.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.retrieve_checkout_session.return_value = {
                "customer": "cus_123",
                "metadata": {"plan_name": "pro"},
            }
            url = reverse("core:checkout_success")
            first = logged_in_client.get(url, {"session_id": "cs_test_1"})
            second = logged_in_client.get(url, {"session_id": "cs_test_1"})

        assert first.status_code == 200
        assert second.status_code == 200
        (event,) = _events_named(ga4, "purchase")
        assert event["params"]["transaction_id"] == "cs_test_1"
        assert event["params"]["value"] == 29.0
        assert event["params"]["currency"] == "USD"
        assert event["params"]["plan"] == "pro"
        assert event["params"]["interval"] == "monthly"

    def test_yearly_purchase_reports_yearly_value(
        self, ga4: MagicMock, logged_in_client: Client, user: User, db: None
    ) -> None:
        """A yearly checkout session reports the yearly price, not 12x less."""
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 99,
                "price_yearly": 990,
                "is_active": True,
            },
        )
        workspace = Workspace.objects.create(name="Acme", stripe_customer_id="cus_123")
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

        with patch("core.services.stripe.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.retrieve_checkout_session.return_value = {
                "customer": "cus_123",
                "metadata": {"plan_name": "pro", "interval": "yearly"},
            }
            response = logged_in_client.get(
                reverse("core:checkout_success"), {"session_id": "cs_test_y"}
            )

        assert response.status_code == 200
        (event,) = _events_named(ga4, "purchase")
        assert event["params"]["value"] == 990.0
        assert event["params"]["interval"] == "yearly"

    def test_corrupted_interval_metadata_reports_monthly(
        self, ga4: MagicMock, logged_in_client: Client, user: User, db: None
    ) -> None:
        """An unexpected interval value is normalized, keeping analytics
        cardinality bounded and the fallback on the monthly price."""
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 99,
                "is_active": True,
            },
        )
        workspace = Workspace.objects.create(name="Acme", stripe_customer_id="cus_123")
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

        with patch("core.services.stripe.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.retrieve_checkout_session.return_value = {
                "customer": "cus_123",
                "metadata": {"plan_name": "pro", "interval": "weekly"},
            }
            logged_in_client.get(
                reverse("core:checkout_success"), {"session_id": "cs_test_w"}
            )

        (event,) = _events_named(ga4, "purchase")
        assert event["params"]["interval"] == "monthly"
        assert event["params"]["value"] == 99.0

    def test_purchase_prefers_amount_resolved_at_checkout(
        self, ga4: MagicMock, logged_in_client: Client, user: User, db: None
    ) -> None:
        """The amount stored in session metadata at checkout time wins.

        A yearly price that lives only in Stripe leaves the Plan row's
        price_yearly NULL — the metadata amount still reports the real
        purchased value instead of 0.
        """
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 99,
                "price_yearly": None,
                "is_active": True,
            },
        )
        workspace = Workspace.objects.create(name="Acme", stripe_customer_id="cus_123")
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

        with patch("core.services.stripe.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.retrieve_checkout_session.return_value = {
                "customer": "cus_123",
                "metadata": {
                    "plan_name": "pro",
                    "interval": "yearly",
                    "amount": "990.00",
                },
            }
            response = logged_in_client.get(
                reverse("core:checkout_success"), {"session_id": "cs_test_amt"}
            )

        assert response.status_code == 200
        (event,) = _events_named(ga4, "purchase")
        assert event["params"]["value"] == 990.0


class TestBillingSyncEvents:
    """Server-side events from the Stripe webhook sync path."""

    def test_plan_change_tracked_on_sync(self, ga4: MagicMock, db: None) -> None:
        """A plan change during Stripe sync sends plan_change."""
        from webhooks.services.billing import BillingService

        workspace = Workspace.objects.create(
            name="Acme", stripe_customer_id="cus_42", subscription_plan="free"
        )

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = [
                {
                    "id": "sub_1",
                    "status": "active",
                    "items": [{"plan_name": "Pro"}],
                }
            ]
            assert BillingService.sync_workspace_from_stripe("cus_42")

        (event,) = _events_named(ga4, "plan_change")
        assert event["params"]["previous_plan"] == "free"
        assert event["params"]["new_plan"] == "pro"
        assert _payloads(ga4)[0]["client_id"] == str(workspace.uuid)

    def test_no_plan_change_event_when_plan_unchanged(
        self, ga4: MagicMock, db: None
    ) -> None:
        """Re-syncing the same plan must not emit plan_change."""
        from webhooks.services.billing import BillingService

        Workspace.objects.create(
            name="Acme", stripe_customer_id="cus_42", subscription_plan="pro"
        )

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = [
                {
                    "id": "sub_1",
                    "status": "active",
                    "items": [{"plan_name": "Pro"}],
                }
            ]
            assert BillingService.sync_workspace_from_stripe("cus_42")

        assert _events_named(ga4, "plan_change") == []

    def test_subscription_cancelled_tracked_on_deletion(
        self, ga4: MagicMock, db: None
    ) -> None:
        """Cancelling the workspace's subscription sends the event."""
        from webhooks.services.billing import BillingService

        workspace = Workspace.objects.create(
            name="Acme",
            stripe_customer_id="cus_42",
            subscription_plan="pro",
            stripe_subscription_id="sub_main",
        )

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_subscription_deleted(
                {"id": "sub_main", "customer": "cus_42"}
            )

        workspace.refresh_from_db()
        assert workspace.subscription_status == "cancelled"
        (event,) = _events_named(ga4, "subscription_cancelled")
        assert event["params"]["plan"] == "pro"

    def test_addon_deletion_does_not_track_cancellation(
        self, ga4: MagicMock, db: None
    ) -> None:
        """Deleting a non-primary subscription must not emit the event."""
        from webhooks.services.billing import BillingService

        Workspace.objects.create(
            name="Acme",
            stripe_customer_id="cus_42",
            subscription_plan="pro",
            stripe_subscription_id="sub_main",
        )

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_subscription_deleted(
                {"id": "sub_addon", "customer": "cus_42"}
            )

        assert _events_named(ga4, "subscription_cancelled") == []


def _paid_workspace() -> Workspace:
    """Create a workspace with a recorded Stripe subscription."""
    return Workspace.objects.create(
        name="Acme",
        stripe_customer_id="cus_42",
        subscription_plan="pro",
        stripe_subscription_id="sub_main",
    )


def _invoice(**overrides: Any) -> dict[str, Any]:
    """Build a Stripe invoice payload for the test workspace."""
    payload: dict[str, Any] = {
        "id": "in_1",
        "customer": "cus_42",
        "subscription": "sub_main",
        "amount_paid": 2900,
        "amount_due": 2900,
        "currency": "usd",
    }
    payload.update(overrides)
    return payload


class TestPaymentEvents:
    """Server-side payment events from the Stripe invoice webhooks."""

    def test_payment_succeeded_tracked_on_paid_invoice(
        self, ga4: MagicMock, db: None
    ) -> None:
        """A real payment on the workspace's subscription sends the event."""
        from webhooks.services.billing import BillingService

        workspace = _paid_workspace()

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_payment_success(_invoice())

        (event,) = _events_named(ga4, "payment_succeeded")
        assert event["params"]["plan"] == "pro"
        assert event["params"]["currency"] == "USD"
        assert event["params"]["value"] == 29.0
        assert event["params"]["transaction_id"] == "in_1"
        payload = next(
            p for p in _payloads(ga4) if p["events"][0]["name"] == "payment_succeeded"
        )
        assert payload["client_id"] == str(workspace.uuid)

    @override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "ga4-payment-dedup-test",
            }
        }
    )
    def test_payment_succeeded_deduped_across_stripe_events(
        self, ga4: MagicMock, db: None
    ) -> None:
        """invoice.payment_succeeded + invoice.paid count one payment once."""
        from django.core.cache import cache
        from webhooks.services.billing import BillingService

        cache.clear()
        _paid_workspace()

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_payment_success(_invoice())
            BillingService.handle_invoice_paid(_invoice())

        assert len(_events_named(ga4, "payment_succeeded")) == 1

    def test_zero_amount_trial_invoice_not_tracked(
        self, ga4: MagicMock, db: None
    ) -> None:
        """The $0 invoice at trial start must not count as a payment."""
        from webhooks.services.billing import BillingService

        _paid_workspace()

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_payment_success(_invoice(amount_paid=0))

        assert _events_named(ga4, "payment_succeeded") == []

    def test_other_subscription_invoice_not_tracked(
        self, ga4: MagicMock, db: None
    ) -> None:
        """A paid invoice for an add-on subscription must not emit the event."""
        from webhooks.services.billing import BillingService

        _paid_workspace()
        BillingService.handle_payment_success(_invoice(subscription="sub_addon"))

        assert _events_named(ga4, "payment_succeeded") == []

    def test_payment_failed_tracked(self, ga4: MagicMock, db: None) -> None:
        """A failed payment on the workspace's subscription sends the event."""
        from webhooks.services.billing import BillingService

        workspace = _paid_workspace()
        BillingService.handle_payment_failed(_invoice(attempt_count=2))

        workspace.refresh_from_db()
        assert workspace.subscription_status == "past_due"
        (event,) = _events_named(ga4, "payment_failed")
        assert event["params"]["plan"] == "pro"
        assert event["params"]["currency"] == "USD"
        assert event["params"]["value"] == 29.0
        assert event["params"]["transaction_id"] == "in_1"
        assert event["params"]["attempt_count"] == 2

    def test_payment_failed_for_other_subscription_not_tracked(
        self, ga4: MagicMock, db: None
    ) -> None:
        """A failed one-off/add-on invoice must not emit the event."""
        from webhooks.services.billing import BillingService

        _paid_workspace()
        BillingService.handle_payment_failed(_invoice(subscription="sub_addon"))

        assert _events_named(ga4, "payment_failed") == []


class TestWebhookIdentityLinking:
    """Billing webhook events attribute to the purchasing browser."""

    def test_checkout_metadata_carries_client_id(
        self, ga4: MagicMock, logged_in_client: Client, user: User, db: None
    ) -> None:
        """The checkout session metadata records the browser's client id."""
        Plan.objects.update_or_create(
            name="pro",
            defaults={
                "display_name": "Pro",
                "price_monthly": 29,
                "is_active": True,
            },
        )
        workspace = Workspace.objects.create(name="Acme")
        WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")
        logged_in_client.cookies[analytics.CLIENT_ID_COOKIE] = "12345.67890"

        with patch("core.services.stripe.StripeAPI") as mock_api_cls:
            mock_api = mock_api_cls.return_value
            mock_api.get_or_create_customer.return_value = {"id": "cus_1"}
            mock_api.get_price_by_lookup_key.return_value = {
                "id": "price_pro_monthly",
                "unit_amount": 2900,
                "currency": "usd",
            }
            mock_api.create_checkout_session.return_value = {
                "id": "cs_1",
                "url": "https://checkout.stripe.com/x",
            }
            response = logged_in_client.get(reverse("core:checkout", args=["pro"]))

        assert response.status_code == 302
        metadata = mock_api.create_checkout_session.call_args.kwargs["metadata"]
        assert metadata["ga_client_id"] == "12345.67890"
        # begin_checkout from the same request uses the same identity.
        payload = next(
            p for p in _payloads(ga4) if p["events"][0]["name"] == "begin_checkout"
        )
        assert payload["client_id"] == "12345.67890"

    def test_checkout_completed_persists_client_id(
        self, ga4: MagicMock, db: None
    ) -> None:
        """handle_checkout_completed stores the metadata client id."""
        from webhooks.services.billing import BillingService

        workspace = Workspace.objects.create(
            name="Acme", stripe_customer_id="cus_42", subscription_plan="free"
        )

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_checkout_completed(
                {
                    "customer": "cus_42",
                    "subscription": "sub_main",
                    "metadata": {
                        "workspace_id": str(workspace.pk),
                        "plan_name": "pro",
                        "ga_client_id": "12345.67890",
                    },
                }
            )

        workspace.refresh_from_db()
        assert workspace.ga4_client_id == "12345.67890"

    def test_checkout_completed_rejects_malformed_client_id(
        self, ga4: MagicMock, db: None
    ) -> None:
        """Metadata is caller-influenced: garbage ids are not persisted."""
        from webhooks.services.billing import BillingService

        workspace = Workspace.objects.create(
            name="Acme", stripe_customer_id="cus_42", subscription_plan="free"
        )

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_checkout_completed(
                {
                    "customer": "cus_42",
                    "subscription": "sub_main",
                    "metadata": {
                        "workspace_id": str(workspace.pk),
                        "plan_name": "pro",
                        "ga_client_id": "not-a-client-id",
                    },
                }
            )

        workspace.refresh_from_db()
        assert workspace.ga4_client_id == ""

    def test_workspace_event_prefers_stored_client_id(
        self, ga4: MagicMock, db: None
    ) -> None:
        """Events for a linked workspace use the purchaser's client id."""
        workspace = Workspace.objects.create(name="Acme", ga4_client_id="12345.67890")

        analytics.track_workspace_event(workspace, "plan_change", {"new_plan": "pro"})

        (payload,) = _payloads(ga4)
        assert payload["client_id"] == "12345.67890"

    def test_payment_event_uses_stored_client_id(
        self, ga4: MagicMock, db: None
    ) -> None:
        """A renewal lands on the same GA4 user as the original checkout."""
        from webhooks.services.billing import BillingService

        workspace = _paid_workspace()
        Workspace.objects.filter(id=workspace.id).update(ga4_client_id="12345.67890")
        workspace.refresh_from_db()

        with patch("webhooks.services.billing.StripeAPI") as mock_api_cls:
            mock_api_cls.return_value.get_customer_subscriptions.return_value = []
            BillingService.handle_payment_success(_invoice())

        payload = next(
            p for p in _payloads(ga4) if p["events"][0]["name"] == "payment_succeeded"
        )
        assert payload["client_id"] == "12345.67890"


class TestDeliveryBackpressure:
    """The fire-and-forget queue is bounded, never unbounded."""

    def test_events_dropped_when_backlog_full(self) -> None:
        """At the pending cap, payloads are dropped, not queued."""
        with override_settings(**GA4_TEST_SETTINGS):
            with (
                patch.object(analytics, "_MAX_PENDING_DELIVERIES", 0),
                patch.object(analytics._executor, "submit") as mock_submit,
            ):
                analytics._submit({"client_id": "1.2", "events": []})
            mock_submit.assert_not_called()

    def test_slot_released_after_delivery(self) -> None:
        """Completed deliveries free their slot for later events."""
        with patch.object(analytics, "_post"):
            baseline = analytics._pending_deliveries
            analytics._submit({"client_id": "1.2", "events": []})
            # The done callback runs synchronously once the (mocked)
            # delivery finishes; poll briefly for the pool thread.
            for _ in range(50):
                if analytics._pending_deliveries == baseline:
                    break
                time.sleep(0.01)
            assert analytics._pending_deliveries == baseline


class TestNestedRedaction:
    """PII redaction reaches nested param structures."""

    def test_emails_redacted_inside_items(self) -> None:
        """Email-shaped strings inside nested dicts/lists are scrubbed."""
        params = analytics._build_event_params(
            {
                "items": [{"item_id": "pro", "note": "bought by user@example.com"}],
                "meta": {"contact": ["ops@example.com", "plain text"]},
            },
            None,
        )
        assert "user@example.com" not in str(params)
        assert "ops@example.com" not in str(params)
        assert params["items"][0]["item_id"] == "pro"
        assert params["meta"]["contact"][1] == "plain text"

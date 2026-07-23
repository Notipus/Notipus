"""Billing and subscription management views.

This module handles plan selection, billing dashboard, payment methods,
and checkout flows.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils import timezone
from webhooks.services.rate_limiter import rate_limiter

from .. import analytics
from ..models import Plan
from ..permissions import get_workspace_for_user, get_workspace_member
from .integrations.base import require_admin_role

logger = logging.getLogger(__name__)


# Use centralized permission function instead of duplicating logic
_get_user_workspace = get_workspace_for_user


@login_required
def select_plan(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Plan selection page for users who do not have a workspace yet.

    New signups get a free workspace auto-provisioned by the dashboard,
    so plan decisions for existing workspaces belong to the upgrade
    page — owners and admins who land here are redirected there, and
    plain members go to the dashboard (upgrade_plan would bounce them
    anyway).

    Args:
        request: The HTTP request object.

    Returns:
        Plan selection page, redirect on successful selection, or a
        role-appropriate redirect for users with a workspace.
    """
    if _get_user_workspace(request.user):
        member = get_workspace_member(request.user)
        if member and member.role not in ("owner", "admin"):
            return redirect("core:dashboard")
        return redirect("core:upgrade_plan")

    if request.method == "POST":
        selected_plan = request.POST.get("plan")
        # Validate against available plans
        if Plan.objects.filter(name=selected_plan, is_active=True).exists():
            request.session["selected_plan"] = selected_plan
            analytics.track_event(request, "select_plan", {"plan": selected_plan})
            return redirect("core:plan_selected")

    # Get plans from database
    plans_queryset = Plan.objects.filter(is_active=True).order_by("price_monthly")
    plans: list[dict[str, Any]] = []

    for plan in plans_queryset:
        # Bare amount only — the template appends "/month" to dollar prices
        price_display = (
            "Free" if plan.price_monthly == 0 else f"${plan.price_monthly:.0f}"
        )
        plans.append(
            {
                "name": plan.name,
                "display_name": plan.display_name,
                "price": price_display,
                "features": plan.features,
                "description": plan.description,
            }
        )

    return render(request, "core/select_plan.html.j2", {"plans": plans})


@login_required
def plan_selected(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Plan confirmation page.

    Args:
        request: The HTTP request object.

    Returns:
        Plan confirmation page or redirect if no plan selected.
    """
    selected_plan = request.session.get("selected_plan")
    if not selected_plan:
        return redirect("core:select_plan")

    return render(
        request, "core/plan_selected.html.j2", {"selected_plan": selected_plan}
    )


@login_required
def billing_dashboard(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Billing dashboard showing current plan, usage, and billing info.

    Args:
        request: The HTTP request object.

    Returns:
        Billing dashboard page or redirect to workspace creation.
    """
    from core.services.dashboard import BillingService

    workspace = _get_user_workspace(request.user)
    if not workspace:
        return redirect("core:create_workspace")

    billing_service = BillingService()
    billing_data = billing_service.get_billing_dashboard_data(workspace)

    # Flatten data for template compatibility
    context: dict[str, Any] = {
        "workspace": billing_data["workspace"],
        **billing_data["usage_data"],  # rate_limit_info, usage_stats, etc.
        **billing_data["trial_info"],  # trial_days_remaining, is_trial, etc.
        "available_plans": billing_data["available_plans"],
        "current_plan": billing_data["current_plan"],
        "current_plan_obj": Plan.objects.filter(
            name=billing_data["current_plan"], is_active=True
        ).first(),
    }

    return render(request, "core/billing_dashboard.html.j2", context)


@login_required
def upgrade_plan(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Plan upgrade/downgrade page.

    Args:
        request: The HTTP request object.

    Returns:
        Upgrade plan page, redirect to workspace creation if the user has
        no workspace, or redirect to the dashboard if the user is not an
        owner/admin (permission denied).
    """
    from core.services.dashboard import BillingService

    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    billing_service = BillingService()
    available_plans = billing_service.get_available_plans(workspace.subscription_plan)

    # Mark plans cheaper than the current one so the template can present
    # them honestly as downgrades instead of "upgrades".
    current_plan_obj = Plan.objects.filter(
        name=workspace.subscription_plan, is_active=True
    ).first()
    if current_plan_obj:
        for plan in available_plans:
            try:
                plan["is_downgrade"] = (
                    Decimal(str(plan.get("price", 0))) < current_plan_obj.price_monthly
                )
            except (InvalidOperation, TypeError, ValueError):
                plan["is_downgrade"] = False

    context: dict[str, Any] = {
        "workspace": workspace,
        "plans": available_plans,
        "current_plan": workspace.subscription_plan,
    }
    return render(request, "core/upgrade_plan.html.j2", context)


@login_required
def payment_methods(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Payment method management page.

    Args:
        request: The HTTP request object.

    Returns:
        Payment methods page, redirect to workspace creation if the user
        has no workspace, or redirect to the dashboard if the user is not
        an owner/admin (permission denied).
    """
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    # In a real implementation, you would fetch payment methods from Stripe
    # using workspace.stripe_customer_id
    payment_methods_list: list[dict[str, Any]] = []

    context: dict[str, Any] = {
        "workspace": workspace,
        "payment_methods": payment_methods_list,
        "has_payment_method": workspace.payment_method_added,
    }
    return render(request, "core/payment_methods.html.j2", context)


@login_required
def billing_history(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Billing history and invoices page.

    Fetches real invoice data from Stripe for the workspace.

    Args:
        request: The HTTP request object.

    Returns:
        Billing history page, redirect to workspace creation if the user
        has no workspace, or redirect to the dashboard if the user is not
        an owner/admin (permission denied).
    """
    from datetime import datetime
    from datetime import timezone as dt_timezone

    from core.services.stripe import StripeAPI
    from webhooks.utils.currency import from_minor_units

    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    # Fetch real invoices from Stripe
    invoices: list[dict[str, Any]] = []
    if workspace.stripe_customer_id:
        stripe_api = StripeAPI()
        raw_invoices = stripe_api.get_invoices(workspace.stripe_customer_id, limit=20)

        def _aware(ts: int | None) -> datetime | None:
            # Aware datetimes: naive fromtimestamp() uses the server's
            # local zone and trips USE_TZ comparisons in templates.
            return (
                datetime.fromtimestamp(ts, tz=dt_timezone.utc)
                if ts is not None
                else None
            )

        # Format invoices for template
        for inv in raw_invoices:
            invoices.append(
                {
                    "id": inv["id"],
                    "number": inv.get("number", "N/A"),
                    "status": inv["status"],
                    # from_minor_units, not /100: zero- and three-decimal
                    # currencies (JPY, KWD) have different minor units.
                    "amount": from_minor_units(inv["amount_paid"], inv["currency"]),
                    "currency": inv["currency"].upper(),
                    "date": _aware(inv["created"]),
                    "period_start": _aware(inv.get("period_start")),
                    "period_end": _aware(inv.get("period_end")),
                    "invoice_url": inv.get("hosted_invoice_url"),
                    "pdf_url": inv.get("invoice_pdf"),
                }
            )

    # Get current month billing amount from Plan model
    current_month_amount = 0.00
    if workspace.subscription_status != "trial":
        try:
            plan = Plan.objects.get(name=workspace.subscription_plan, is_active=True)
            current_month_amount = float(plan.price_monthly)
        except Plan.DoesNotExist:
            current_month_amount = 0.00

    # Get rate limit info for next payment date
    is_allowed, rate_limit_info = rate_limiter.check_rate_limit(workspace)

    # Calculate trial days remaining
    trial_days_remaining = 0
    if workspace.subscription_status == "trial" and workspace.trial_end_date:
        trial_days_remaining = max(0, (workspace.trial_end_date - timezone.now()).days)

    context: dict[str, Any] = {
        "workspace": workspace,
        "invoices": invoices,
        "current_month_amount": current_month_amount,
        "rate_limit_info": rate_limit_info,
        "trial_days_remaining": trial_days_remaining,
    }
    return render(request, "core/billing_history.html.j2", context)


def _duplicate_subscription_guard(
    request: HttpRequest,
    stripe_api: Any,
    *,
    customer_id: str,
    had_stripe_customer: bool,
) -> HttpResponseRedirect | None:
    """Block checkout when the customer already has a live subscription.

    Returns a redirect response when the caller must abort (existing live
    sub, or Stripe outage during the check), or None to proceed with
    checkout.

    Skips the Stripe probe entirely when the workspace had no Stripe
    customer before this request: a brand-new customer can't have any
    subscriptions, so the 1-3 probe calls would be wasted latency.

    The Stripe-error branch fails closed: a silently-returned False would
    be indistinguishable from "no live sub" and let a second subscription
    through during a transient outage, so we redirect to the portal with
    an explicit message instead.
    """
    import stripe

    if not had_stripe_customer:
        return None

    try:
        has_live_sub = stripe_api.has_live_subscription(
            customer_id, raise_on_error=True
        )
    except stripe.StripeError:
        logger.exception(
            "Stripe error while checking existing subscriptions for "
            f"customer {customer_id}; refusing to create a new "
            "subscription to avoid duplicate billing."
        )
        messages.error(
            request,
            "We couldn't verify your subscription status with Stripe. "
            "Please try again in a moment, or use the billing portal "
            "to manage your subscription.",
        )
        return redirect("core:billing_portal")

    if has_live_sub:
        messages.info(
            request,
            "You already have an active subscription. "
            "Use the billing portal to change plans.",
        )
        return redirect("core:billing_portal")

    return None


@login_required
def checkout(
    request: HttpRequest, plan_name: str
) -> HttpResponse | HttpResponseRedirect:
    """Create Stripe Checkout Session and redirect to Stripe-hosted checkout.

    Args:
        request: The HTTP request object.
        plan_name: Name of the plan to checkout (basic, pro, enterprise).

    Returns:
        Redirect to Stripe Checkout or error page. Redirects to workspace
        creation if the user has no workspace, or to the dashboard if the
        user is not an owner/admin (permission denied).
    """
    from core.services.stripe import StripeAPI
    from django.conf import settings as django_settings

    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    try:
        # Validate against the Plan model (single source of truth for
        # purchasable plans) instead of a hardcoded list that drifts.
        plan = (
            Plan.objects.filter(name=plan_name, is_active=True)
            .exclude(price_monthly=0)
            .first()
        )
        if plan is None:
            messages.error(request, "Invalid plan selected.")
            return redirect("core:upgrade_plan")

        # Initialize Stripe API
        stripe_api = StripeAPI()

        # Capture whether the workspace already had a Stripe customer
        # *before* get_or_create_customer mutates the instance. A brand-new
        # customer can't have any subscriptions, so we can skip the
        # has_live_subscription probe (1–3 Stripe calls) on first checkout.
        had_stripe_customer = bool(workspace.stripe_customer_id)

        # Get or create Stripe customer for the workspace
        customer = stripe_api.get_or_create_customer(workspace)
        if not customer:
            messages.error(
                request, "Unable to create billing account. Please try again."
            )
            return redirect("core:upgrade_plan")

        guard_redirect = _duplicate_subscription_guard(
            request,
            stripe_api,
            customer_id=customer["id"],
            had_stripe_customer=had_stripe_customer,
        )
        if guard_redirect is not None:
            return guard_redirect

        # Try to get price from Stripe using lookup key (preferred method)
        lookup_key = f"{plan_name}_monthly"
        price = stripe_api.get_price_by_lookup_key(lookup_key)

        if price:
            price_id = price["id"]
        else:
            # Fall back to the Plan model's stored price ID, then to the
            # environment variable mapping.
            price_id = plan.stripe_price_id_monthly or django_settings.STRIPE_PLANS.get(
                plan_name
            )
            if not price_id:
                logger.error(f"No Stripe price configured for plan: {plan_name}")
                messages.error(
                    request, "Plan configuration error. Please contact support."
                )
                return redirect("core:upgrade_plan")

        # Create Stripe Checkout Session.
        # Idempotency key collapses duplicate checkout-initiation requests
        # (double-click, browser back, retry) to one session within Stripe's
        # 24h window, while leaving a deliberate next-day retry unblocked.
        checkout_session = stripe_api.create_checkout_session(
            customer_id=customer["id"],
            price_id=price_id,
            metadata={
                "workspace_id": str(workspace.pk),
                "plan_name": plan_name,
            },
            # No time-based component: any date bucket (local or UTC)
            # has a midnight edge where retries minutes apart fall on
            # different sides and stop deduping. Stripe's idempotency
            # keys already expire 24h after first use, so the same
            # (workspace, plan) within 24h collapses to one session and
            # a deliberate next-day retry creates a new one — exactly
            # the desired behavior, without a midnight glitch.
            idempotency_key=f"checkout-{workspace.uuid}-{plan_name}",
        )

        if not checkout_session or not checkout_session.get("url"):
            messages.error(
                request, "Unable to create checkout session. Please try again."
            )
            return redirect("core:upgrade_plan")

        analytics.track_event(
            request,
            "begin_checkout",
            {
                "plan": plan.name,
                "currency": "USD",
                "value": float(plan.price_monthly),
                "items": [{"item_id": plan.name, "item_name": plan.display_name}],
            },
        )

        # Redirect to Stripe Checkout
        return redirect(checkout_session["url"])

    except Exception as e:
        logger.exception(f"Checkout error: {e!s}")
        messages.error(request, "An error occurred. Please try again.")
        return redirect("core:upgrade_plan")


@login_required
def billing_portal(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Redirect to Stripe Customer Portal for self-service billing management.

    Allows customers to update payment methods, view invoices,
    and manage their subscription through Stripe's hosted portal.

    Args:
        request: The HTTP request object.

    Returns:
        Redirect to Stripe Customer Portal or billing dashboard on error.
        Redirects to workspace creation if the user has no workspace, or to
        the dashboard if the user is not an owner/admin (permission denied).
    """
    from core.services.stripe import StripeAPI

    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    try:
        # Check if workspace has a Stripe customer
        if not workspace.stripe_customer_id:
            messages.warning(
                request,
                "No billing account found. Please subscribe to a plan first.",
            )
            return redirect("core:upgrade_plan")

        # Initialize Stripe API and create portal session
        stripe_api = StripeAPI()
        portal_session = stripe_api.create_portal_session(
            customer_id=workspace.stripe_customer_id,
        )

        if not portal_session or not portal_session.get("url"):
            messages.error(
                request, "Unable to access billing portal. Please try again."
            )
            return redirect("core:billing_dashboard")

        # Redirect to Stripe Customer Portal
        return redirect(portal_session["url"])

    except Exception as e:
        logger.exception(f"Billing portal error: {e!s}")
        messages.error(request, "An error occurred. Please try again.")
        return redirect("core:billing_dashboard")


@login_required
def checkout_success(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Checkout success page.

    Retrieves plan information from Stripe session to avoid session cookie issues
    with cross-site redirects.

    Args:
        request: The HTTP request object.

    Returns:
        Success page or redirect to billing dashboard.
    """
    from core.services.stripe import StripeAPI

    # Get session_id from query params (passed by Stripe)
    session_id = request.GET.get("session_id")

    plan_name = None

    if session_id:
        # Retrieve plan name from Stripe Checkout Session metadata,
        # but only for a session that belongs to this user's workspace —
        # session_id is caller-supplied, so don't reflect someone else's
        # session contents back.
        try:
            workspace = _get_user_workspace(request.user)
            stripe_api = StripeAPI()
            checkout_session = stripe_api.retrieve_checkout_session(session_id)
            if (
                checkout_session
                and workspace
                and workspace.stripe_customer_id
                and checkout_session.get("customer") == workspace.stripe_customer_id
            ):
                plan_name = checkout_session.get("metadata", {}).get("plan_name")
        except Exception as e:
            logger.warning(f"Error retrieving Stripe session: {e}")

    if not plan_name:
        # No plan info available, redirect to billing dashboard
        messages.info(request, "Your subscription has been updated successfully.")
        return redirect("core:billing_dashboard")

    # Track the conversion once per checkout session: Stripe redirects
    # land here with a reusable URL, so a page refresh must not double
    # count revenue. transaction_id also lets GA4 dedupe on its side.
    purchase_tracked_key = f"ga4_purchase_{session_id}"
    if not request.session.get(purchase_tracked_key):
        request.session[purchase_tracked_key] = True
        plan = Plan.objects.filter(name=plan_name, is_active=True).first()
        analytics.track_event(
            request,
            "purchase",
            {
                "plan": plan_name,
                "transaction_id": session_id,
                "currency": "USD",
                "value": float(plan.price_monthly) if plan else 0.0,
                "items": [{"item_id": plan_name}],
            },
        )

    context: dict[str, Any] = {
        "plan_name": plan_name,
    }
    return render(request, "core/checkout_success.html.j2", context)


@login_required
def checkout_cancel(request: HttpRequest) -> HttpResponse:
    """Checkout cancelled page.

    Args:
        request: The HTTP request object.

    Returns:
        Checkout cancel page.
    """
    analytics.track_event(request, "checkout_cancelled")
    return render(request, "core/checkout_cancel.html.j2")

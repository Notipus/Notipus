"""Billing and subscription management views.

This module handles plan selection, billing dashboard, payment methods,
and checkout flows.
"""

import logging
from datetime import date
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils import timezone
from webhooks.services.rate_limiter import rate_limiter

from ..models import Plan
from ..permissions import get_workspace_for_user

logger = logging.getLogger(__name__)


# Use centralized permission function instead of duplicating logic
_get_user_workspace = get_workspace_for_user


def select_plan(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Plan selection page.

    Args:
        request: The HTTP request object.

    Returns:
        Plan selection page or redirect on successful selection.
    """
    if request.method == "POST":
        selected_plan = request.POST.get("plan")
        # Validate against available plans
        if Plan.objects.filter(name=selected_plan, is_active=True).exists():
            request.session["selected_plan"] = selected_plan
            return redirect("core:plan_selected")

    # Get plans from database
    plans_queryset = Plan.objects.filter(is_active=True).order_by("price_monthly")
    plans: list[dict[str, Any]] = []

    for plan in plans_queryset:
        price_display = (
            "Free" if plan.price_monthly == 0 else f"${plan.price_monthly:.0f}/month"
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
    }

    return render(request, "core/billing_dashboard.html.j2", context)


@login_required
def upgrade_plan(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Plan upgrade/downgrade page.

    Args:
        request: The HTTP request object.

    Returns:
        Upgrade plan page or redirect to workspace creation.
    """
    from core.services.dashboard import BillingService

    workspace = _get_user_workspace(request.user)
    if not workspace:
        return redirect("core:create_workspace")

    billing_service = BillingService()
    available_plans = billing_service.get_available_plans(workspace.subscription_plan)

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
        Payment methods page or redirect to workspace creation.
    """
    workspace = _get_user_workspace(request.user)
    if not workspace:
        return redirect("core:create_workspace")

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
        Billing history page or redirect to workspace creation.
    """
    from datetime import datetime

    from core.services.stripe import StripeAPI

    workspace = _get_user_workspace(request.user)
    if not workspace:
        return redirect("core:create_workspace")

    # Fetch real invoices from Stripe
    invoices: list[dict[str, Any]] = []
    if workspace.stripe_customer_id:
        stripe_api = StripeAPI()
        raw_invoices = stripe_api.get_invoices(workspace.stripe_customer_id, limit=20)
        # Format invoices for template
        for inv in raw_invoices:
            invoices.append(
                {
                    "id": inv["id"],
                    "number": inv.get("number", "N/A"),
                    "status": inv["status"],
                    "amount": inv["amount_paid"] / 100,  # Convert from cents
                    "currency": inv["currency"].upper(),
                    "date": datetime.fromtimestamp(inv["created"]),
                    "period_start": datetime.fromtimestamp(inv["period_start"])
                    if inv.get("period_start")
                    else None,
                    "period_end": datetime.fromtimestamp(inv["period_end"])
                    if inv.get("period_end")
                    else None,
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


@login_required
def checkout(
    request: HttpRequest, plan_name: str
) -> HttpResponse | HttpResponseRedirect:
    """Create Stripe Checkout Session and redirect to Stripe-hosted checkout.

    Args:
        request: The HTTP request object.
        plan_name: Name of the plan to checkout (basic, pro, enterprise).

    Returns:
        Redirect to Stripe Checkout or error page.
    """
    from core.services.stripe import StripeAPI
    from django.conf import settings as django_settings

    workspace = _get_user_workspace(request.user)
    if not workspace:
        return redirect("core:create_workspace")

    try:
        # Validate plan name
        valid_plans = ["basic", "pro", "enterprise"]
        if plan_name not in valid_plans:
            messages.error(request, "Invalid plan selected.")
            return redirect("core:upgrade_plan")

        # Initialize Stripe API
        stripe_api = StripeAPI()

        # Get or create Stripe customer for the workspace
        customer = stripe_api.get_or_create_customer(workspace)
        if not customer:
            messages.error(
                request, "Unable to create billing account. Please try again."
            )
            return redirect("core:upgrade_plan")

        # Don't create a duplicate subscription if the customer already has
        # a live one. Plan changes belong in the Stripe Customer Portal,
        # which handles them with prorations instead of a second subscription.
        existing_subs = stripe_api.get_customer_subscriptions(
            customer["id"], status="all"
        )
        if any(
            sub.get("status") in ("active", "trialing", "past_due")
            for sub in existing_subs
        ):
            messages.info(
                request,
                "You already have an active subscription. "
                "Use the billing portal to change plans.",
            )
            return redirect("core:billing_portal")

        # Try to get price from Stripe using lookup key (preferred method)
        lookup_key = f"{plan_name}_monthly"
        price = stripe_api.get_price_by_lookup_key(lookup_key)

        if not price:
            # Fall back to environment variable price ID
            price_id = django_settings.STRIPE_PLANS.get(plan_name)
            if not price_id:
                logger.error(f"No Stripe price configured for plan: {plan_name}")
                messages.error(
                    request, "Plan configuration error. Please contact support."
                )
                return redirect("core:upgrade_plan")
        else:
            price_id = price["id"]

        # Create Stripe Checkout Session.
        # Idempotency key collapses duplicate checkout-initiation requests
        # (double-click, browser back, retry) to one session within Stripe's
        # 24h window, while leaving a deliberate next-day retry unblocked.
        checkout_session = stripe_api.create_checkout_session(
            customer_id=customer["id"],
            price_id=price_id,
            metadata={
                "workspace_id": str(workspace.id),
                "plan_name": plan_name,
            },
            idempotency_key=(
                f"checkout-{workspace.uuid}-{plan_name}-{date.today().isoformat()}"
            ),
        )

        if not checkout_session or not checkout_session.get("url"):
            messages.error(
                request, "Unable to create checkout session. Please try again."
            )
            return redirect("core:upgrade_plan")

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
    """
    from core.services.stripe import StripeAPI

    workspace = _get_user_workspace(request.user)
    if not workspace:
        return redirect("core:create_workspace")

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
        # Retrieve plan name from Stripe Checkout Session metadata
        try:
            stripe_api = StripeAPI()
            checkout_session = stripe_api.retrieve_checkout_session(session_id)
            if checkout_session:
                plan_name = checkout_session.get("metadata", {}).get("plan_name")
        except Exception as e:
            logger.warning(f"Error retrieving Stripe session: {e}")

    if not plan_name:
        # No plan info available, redirect to billing dashboard
        messages.info(request, "Your subscription has been updated successfully.")
        return redirect("core:billing_dashboard")

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
    return render(request, "core/checkout_cancel.html.j2")

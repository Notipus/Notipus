"""Dashboard and workspace management views.

This module handles the main dashboard and workspace settings.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render

from ..models import UserProfile, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)


@login_required
def dashboard(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Main dashboard for authenticated users.

    Args:
        request: The HTTP request object.

    Returns:
        Dashboard page or redirect to workspace creation.
    """
    from core.services.dashboard import DashboardService

    dashboard_service = DashboardService()
    dashboard_data = dashboard_service.get_dashboard_data(request.user)

    if not dashboard_data:
        # User doesn't have a workspace yet
        # First, ensure they've selected a plan
        if "selected_plan" not in request.session:
            return redirect("core:select_plan")
        # Then redirect to workspace creation
        return redirect("core:create_workspace")

    # Flatten the data for template compatibility
    workspace = dashboard_data["workspace"]
    context: dict[str, Any] = {
        "workspace": workspace,
        "organization": workspace,  # Alias for template compatibility
        "user_profile": dashboard_data["user_profile"],
        "member": dashboard_data.get("member"),
        **dashboard_data["integrations"],  # has_slack, has_shopify, etc.
        "recent_activity": dashboard_data["recent_activity"],
        **dashboard_data["usage_data"],  # rate_limit_info, usage_stats, etc.
        **dashboard_data["trial_info"],  # trial_days_remaining, is_trial, etc.
    }

    return render(request, "core/dashboard.html.j2", context)


def _create_stripe_checkout_for_plan(
    workspace: Workspace, selected_plan: str
) -> str | None:
    """Create a Stripe checkout session for a paid plan with trial.

    Args:
        workspace: The newly created workspace.
        selected_plan: The selected plan name.

    Returns:
        Checkout URL if successful, None otherwise.
    """
    # Imports are inside function to avoid circular imports with models/services
    from core.models import Plan
    from core.services.stripe import StripeAPI
    from django.conf import settings

    try:
        plan = Plan.objects.get(name=selected_plan, is_active=True)
        if not plan.stripe_price_id_monthly:
            logger.warning(f"Plan '{selected_plan}' has no Stripe price ID")
            return None

        stripe_api = StripeAPI()
        customer = stripe_api.get_or_create_customer(workspace)
        if not customer:
            logger.error(
                f"Failed to get/create Stripe customer for workspace {workspace.pk}"
            )
            return None

        # TRIAL_PERIOD_DAYS: Number of days for paid plan trials (default: 14)
        trial_days = getattr(settings, "TRIAL_PERIOD_DAYS", 14)
        checkout = stripe_api.create_checkout_session(
            customer_id=customer["id"],
            price_id=plan.stripe_price_id_monthly,
            trial_period_days=trial_days,
            metadata={"workspace_id": str(workspace.pk)},
        )
        return checkout.get("url") if checkout else None

    except Plan.DoesNotExist:
        logger.warning(f"Plan '{selected_plan}' not found in database")
        return None
    except Exception as e:
        logger.error(f"Error creating Stripe checkout: {e}")
        return None


@login_required
def create_workspace(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Workspace creation page.

    Args:
        request: The HTTP request object.

    Returns:
        Workspace creation form or redirect to dashboard on success.
    """
    if request.method == "POST":
        name = request.POST.get("name")
        shop_domain = request.POST.get("shop_domain")
        selected_plan = request.session.get("selected_plan", "free")

        if name:
            # Create workspace with selected plan
            # Use None for empty shop_domain to allow multiple orgs without domains
            # (PostgreSQL unique constraint allows multiple NULLs but not empty strings)
            workspace = Workspace.objects.create(
                name=name,
                shop_domain=shop_domain or None,
                subscription_plan=selected_plan,
                # If paid plan with trial, set status to trial
                subscription_status="trial" if selected_plan != "free" else "active",
            )

            # Create workspace member with owner role
            WorkspaceMember.objects.create(
                user=request.user,
                workspace=workspace,
                role="owner",
            )

            # Also create/update user profile for backward compatibility (slack_user_id)
            # Use get_or_create to handle users who already have a profile from SSO
            user_profile, created = UserProfile.objects.get_or_create(
                user=request.user,
                defaults={"workspace": workspace, "slack_user_id": None},
            )
            if not created:
                # Update existing profile's workspace
                user_profile.workspace = workspace
                user_profile.save()

            # Clear the selected plan from session
            if "selected_plan" in request.session:
                del request.session["selected_plan"]

            # For paid plans, redirect to Stripe checkout with trial period
            if selected_plan != "free":
                checkout_url = _create_stripe_checkout_for_plan(
                    workspace, selected_plan
                )
                if checkout_url:
                    return redirect(checkout_url)
                # Checkout creation failed - workspace is in trial status so user can
                # still use the app; they can set up billing later from billing page
                logger.warning(
                    f"Stripe checkout creation failed for workspace {workspace.pk}, "
                    f"plan '{selected_plan}'. User will need to set up billing later."
                )

            messages.success(request, f"Workspace '{name}' created successfully!")
            return redirect("core:dashboard")

    return render(request, "core/create_workspace.html.j2")


@login_required
def workspace_settings(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Workspace settings page.

    Args:
        request: The HTTP request object.

    Returns:
        Settings page or redirect to workspace creation.
    """
    try:
        # Try to get workspace from WorkspaceMember first
        member = WorkspaceMember.objects.filter(
            user=request.user, is_active=True
        ).first()
        if member:
            workspace = member.workspace
        else:
            # Fall back to UserProfile for backward compatibility
            user_profile = UserProfile.objects.get(user=request.user)
            workspace = user_profile.workspace

        if request.method == "POST":
            workspace.name = request.POST.get("name", workspace.name)
            workspace.shop_domain = request.POST.get(
                "shop_domain", workspace.shop_domain
            )
            workspace.save()
            messages.success(request, "Workspace settings updated!")
            return redirect("core:workspace_settings")

        context = {"workspace": workspace}
        return render(request, "core/workspace_settings.html.j2", context)

    except UserProfile.DoesNotExist:
        return redirect("core:create_workspace")

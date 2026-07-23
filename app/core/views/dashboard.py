"""Dashboard and workspace management views.

This module handles the main dashboard and workspace settings.
"""

import logging
from typing import Any, cast

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render

from .. import analytics
from ..constants import SLACK_TEAM_NAME_SESSION_KEY
from ..models import UserProfile, Workspace, WorkspaceMember
from .integrations.base import require_admin_role

logger = logging.getLogger(__name__)


@login_required
def dashboard(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Main dashboard for authenticated users.

    Args:
        request: The HTTP request object.

    Returns:
        Dashboard page, or a redirect to the integrations hub right
        after the user's free workspace is auto-provisioned.
    """
    from core.services.dashboard import DashboardService

    dashboard_service = DashboardService()
    dashboard_data = dashboard_service.get_dashboard_data(request.user)

    if not dashboard_data:
        # First visit: provision a free workspace instead of forcing a
        # plan decision before the user has built anything. Plan choice
        # moves to the upgrade page, once there is something to lose.
        workspace = _provision_free_workspace(request)
        messages.success(
            request,
            f"Your workspace '{workspace.name}' is ready. "
            f"Connect your first integration to start receiving notifications.",
        )
        return redirect("core:integrations")

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
            # No time-based component: any date bucket (local or UTC)
            # has a midnight edge where retries minutes apart fall on
            # different sides and stop deduping. Stripe's idempotency
            # keys already expire 24h after first use, so the same
            # (workspace, plan) within 24h collapses to one session and
            # a deliberate next-day retry creates a new one — exactly
            # the desired behavior, without a midnight glitch.
            idempotency_key=f"checkout-{workspace.uuid}-{selected_plan}",
        )
        return checkout.get("url") if checkout else None

    except Plan.DoesNotExist:
        logger.warning(f"Plan '{selected_plan}' not found in database")
        return None
    except Exception as e:
        logger.error(f"Error creating Stripe checkout: {e}")
        return None


def _create_workspace_records(
    user: Any, *, name: str, shop_domain: str | None, plan: str
) -> Workspace:
    """Create a workspace with its owner membership and user profile.

    Args:
        user: The user who becomes the workspace owner.
        name: Workspace display name.
        shop_domain: Optional shop domain; empty values are stored as
            None so the unique constraint allows multiple workspaces
            without domains (PostgreSQL allows multiple NULLs but not
            multiple empty strings).
        plan: Internal plan name; paid plans start in trial status.

    Returns:
        The created Workspace.
    """
    workspace = Workspace.objects.create(
        name=name,
        shop_domain=shop_domain or None,
        subscription_plan=plan,
        subscription_status="trial" if plan != "free" else "active",
    )
    WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")

    # Also create/update user profile for backward compatibility
    # (slack_user_id). get_or_create handles users who already have a
    # profile from SSO.
    user_profile, created = UserProfile.objects.get_or_create(
        user=user,
        defaults={"workspace": workspace, "slack_user_id": None},
    )
    if not created:
        user_profile.workspace = workspace
        user_profile.save()

    return workspace


def _provision_free_workspace(request: HttpRequest) -> Workspace:
    """Create the user's default free workspace without a form.

    Users build before they commit: they land in the product on the
    free plan with a workspace named after their Slack team (captured
    at OAuth) or their username, and pick a paid plan later from the
    upgrade page.

    Safe under concurrent first visits: the user row is locked while
    provisioning, so a racing request waits and then adopts the
    winner's workspace instead of creating a duplicate.

    Args:
        request: The HTTP request object for the workspace-less user.

    Returns:
        The provisioned (or concurrently created) Workspace.
    """
    # get_username() exists on the AbstractBaseUser union; the view is
    # login_required so this is never an AnonymousUser.
    name = (
        request.session.get(SLACK_TEAM_NAME_SESSION_KEY)
        or f"{request.user.get_username()}'s Workspace"
    )

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        existing = (
            WorkspaceMember.objects.filter(user=request.user, is_active=True)
            .select_related("workspace")
            .first()
        )
        if existing:
            # cast: without the django mypy plugin, FK attribute access
            # types as Any.
            return cast(Workspace, existing.workspace)
        workspace = _create_workspace_records(
            request.user, name=name, shop_domain=None, plan="free"
        )

    request.session.pop(SLACK_TEAM_NAME_SESSION_KEY, None)
    request.session.pop("selected_plan", None)
    analytics.track_event(
        request, "workspace_created", {"plan": "free", "method": "auto"}
    )
    logger.info(f"Auto-provisioned free workspace '{name}' for user {request.user.pk}")
    return workspace


@login_required
def create_workspace(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Workspace creation page.

    Kept as the manual path (custom name, shop domain, or a paid plan
    already staged in the session); new signups normally get a free
    workspace auto-provisioned by the dashboard instead.

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
            workspace = _create_workspace_records(
                request.user,
                name=name,
                shop_domain=shop_domain,
                plan=selected_plan,
            )

            # Clear the selected plan from session
            if "selected_plan" in request.session:
                del request.session["selected_plan"]

            # The Slack team name has served its purpose as a prefill
            request.session.pop(SLACK_TEAM_NAME_SESSION_KEY, None)

            analytics.track_event(request, "workspace_created", {"plan": selected_plan})

            # For paid plans, redirect to Stripe checkout with trial period
            if selected_plan != "free":
                checkout_url = _create_stripe_checkout_for_plan(
                    workspace, selected_plan
                )
                if checkout_url:
                    analytics.track_event(
                        request, "begin_checkout", {"plan": selected_plan}
                    )
                    return redirect(checkout_url)
                # Checkout creation failed - workspace is in trial status so user can
                # still use the app; they can set up billing later from billing page
                logger.warning(
                    f"Stripe checkout creation failed for workspace {workspace.pk}, "
                    f"plan '{selected_plan}'. User will need to set up billing later."
                )

            messages.success(request, f"Workspace '{name}' created successfully!")
            return redirect("core:dashboard")

    return render(
        request,
        "core/create_workspace.html.j2",
        {"suggested_name": request.session.get(SLACK_TEAM_NAME_SESSION_KEY, "")},
    )


@login_required
def workspace_settings(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Workspace settings page.

    Args:
        request: The HTTP request object.

    Returns:
        Settings page, redirect to workspace creation if the user has no
        workspace, or redirect to the dashboard if the user is not an
        owner/admin (permission denied).
    """
    # Modifying workspace settings is an admin capability: shop_domain in
    # particular participates in webhook routing / tenant identity, so gate
    # the whole view behind the owner/admin role check.
    workspace, redirect_response = require_admin_role(request)
    if redirect_response:
        return redirect_response
    assert workspace is not None

    if request.method == "POST":
        workspace.name = request.POST.get("name", workspace.name)
        workspace.shop_domain = request.POST.get("shop_domain", workspace.shop_domain)
        workspace.save()
        messages.success(request, "Workspace settings updated!")
        return redirect("core:workspace_settings")

    context = {"workspace": workspace}
    return render(request, "core/workspace_settings.html.j2", context)

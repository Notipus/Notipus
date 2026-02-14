"""Integration views package.

This package provides a modular, plugin-like architecture for integration views.
Each integration type has its own module with dedicated views for connection,
configuration, and disconnection.

Modules:
    - slack: Slack OAuth for notifications
    - stripe: Stripe webhook configuration (manual setup)
    - shopify: Shopify OAuth and webhook configuration
    - chargify: Chargify/Maxio webhook configuration (manual setup)

To add a new integration:
    1. Create a new module (e.g., `discord.py`)
    2. Define the integration views following the patterns in existing modules
    3. Export the views from this __init__.py
    4. Add URL routes in core/urls.py
    5. Update the IntegrationService to include the new integration
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render

from ...models import UserProfile, WorkspaceMember

# Import all integration views for re-export
from .chargify import integrate_chargify
from .hunter import disconnect_hunter, integrate_hunter
from .shopify import (
    disconnect_shopify,
    integrate_shopify,
    shopify_connect,
    shopify_connect_callback,
    update_shopify_events,
)
from .slack import (
    configure_slack,
    disconnect_slack,
    get_slack_channels,
    integrate_slack,
    slack_connect,
    slack_connect_callback,
    test_slack,
)
from .stripe import (
    disconnect_stripe,
    integrate_stripe,
)
from .zendesk import (
    disconnect_zendesk,
    integrate_zendesk,
)

logger = logging.getLogger(__name__)

# Export all views
__all__ = [
    # Main page
    "integrations",
    # Slack
    "integrate_slack",
    "slack_connect",
    "slack_connect_callback",
    "disconnect_slack",
    "test_slack",
    "get_slack_channels",
    "configure_slack",
    # Stripe
    "integrate_stripe",
    "disconnect_stripe",
    # Shopify
    "integrate_shopify",
    "shopify_connect",
    "shopify_connect_callback",
    "disconnect_shopify",
    "update_shopify_events",
    # Chargify
    "integrate_chargify",
    # Zendesk
    "integrate_zendesk",
    "disconnect_zendesk",
    # Hunter.io
    "integrate_hunter",
    "disconnect_hunter",
]


@login_required
def integrations(request: HttpRequest) -> HttpResponse | HttpResponseRedirect:
    """Integrations overview page.

    Displays all available integrations grouped by category
    (event sources and notification channels).

    Args:
        request: The HTTP request object.

    Returns:
        Integrations page or redirect to workspace creation.
    """
    from core.services.dashboard import IntegrationService

    # Try WorkspaceMember first
    member = WorkspaceMember.objects.filter(user=request.user, is_active=True).first()
    if member:
        workspace = member.workspace
    else:
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            workspace = user_profile.workspace
        except UserProfile.DoesNotExist:
            return redirect("core:create_workspace")

    integration_service = IntegrationService()
    context = integration_service.get_integration_overview(workspace)

    return render(request, "core/integrations.html.j2", context)

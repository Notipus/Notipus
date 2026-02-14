"""Core views package.

This package organizes views into logical modules for better maintainability.
"""

from .auth import (
    home,
    landing,
    slack_auth,
    slack_auth_callback,
)
from .billing import (
    billing_dashboard,
    billing_history,
    billing_portal,
    checkout,
    checkout_cancel,
    checkout_success,
    payment_methods,
    plan_selected,
    select_plan,
    upgrade_plan,
)
from .dashboard import (
    create_workspace,
    dashboard,
    workspace_settings,
)
from .errors import (
    custom_404,
    custom_500,
)
from .integrations import (
    configure_slack,
    disconnect_hunter,
    disconnect_shopify,
    disconnect_slack,
    disconnect_stripe,
    disconnect_zendesk,
    get_slack_channels,
    integrate_chargify,
    integrate_hunter,
    integrate_shopify,
    integrate_slack,
    integrate_stripe,
    integrate_zendesk,
    integrations,
    shopify_connect,
    shopify_connect_callback,
    slack_connect,
    slack_connect_callback,
    test_slack,
    update_shopify_events,
)
from .members import (
    accept_invitation,
    cancel_invitation,
    change_role,
    confirm_accept_invitation,
    invite_member,
    members_list,
    remove_member,
)
from .settings import (
    get_notification_settings,
    update_notification_settings,
)
from .webauthn import (
    webauthn_authenticate_begin,
    webauthn_authenticate_complete,
    webauthn_credentials,
    webauthn_register_begin,
    webauthn_register_complete,
    webauthn_signup_begin,
    webauthn_signup_complete,
)

__all__ = [
    # Auth
    "home",
    "landing",
    "slack_auth",
    "slack_auth_callback",
    # Dashboard
    "dashboard",
    "create_workspace",
    "workspace_settings",
    # Integrations
    "integrations",
    "integrate_slack",
    "integrate_shopify",
    "integrate_chargify",
    "integrate_stripe",
    "integrate_zendesk",
    "integrate_hunter",
    "slack_connect",
    "slack_connect_callback",
    "shopify_connect",
    "shopify_connect_callback",
    "disconnect_slack",
    "disconnect_stripe",
    "disconnect_shopify",
    "disconnect_zendesk",
    "disconnect_hunter",
    "update_shopify_events",
    "test_slack",
    "get_slack_channels",
    "configure_slack",
    # Settings
    "get_notification_settings",
    "update_notification_settings",
    # Billing
    "select_plan",
    "plan_selected",
    "billing_dashboard",
    "billing_portal",
    "upgrade_plan",
    "payment_methods",
    "billing_history",
    "checkout",
    "checkout_success",
    "checkout_cancel",
    # WebAuthn
    "webauthn_register_begin",
    "webauthn_register_complete",
    "webauthn_authenticate_begin",
    "webauthn_authenticate_complete",
    "webauthn_credentials",
    "webauthn_signup_begin",
    "webauthn_signup_complete",
    # Members
    "members_list",
    "invite_member",
    "remove_member",
    "change_role",
    "cancel_invitation",
    "accept_invitation",
    "confirm_accept_invitation",
    # Error handlers
    "custom_404",
    "custom_500",
]

"""Authorization tests for billing and workspace-settings views.

These tests prove that state-mutating and financial-data-exposing views are
gated behind the owner/admin role:

- Billing views (billing_portal, checkout, payment_methods, billing_history,
  upgrade_plan) must reject a plain ``role="user"`` member.
- workspace_settings must reject a plain ``role="user"`` member and must not
  let one mutate the workspace name / shop_domain.
- Owners and admins retain access to all of the above.

Non-admins are redirected to the dashboard (the permission-denied UX used by
``require_admin_role`` elsewhere in the codebase), never served the page.
"""

from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

# Billing/settings views that only owners and admins may reach. ``args`` are
# the reverse() positional arguments for views that take URL parameters.
GATED_VIEWS: list[tuple[str, list[str]]] = [
    ("core:billing_portal", []),
    ("core:payment_methods", []),
    ("core:billing_history", []),
    ("core:upgrade_plan", []),
    ("core:checkout", ["pro"]),
    ("core:workspace_settings", []),
]

# Gated views that only accept POST (state-changing, CSRF-protected).
POST_ONLY_VIEWS = {"core:checkout"}


def _fetch(client: Client, view_name: str, args: list[str]) -> Any:
    """Request a gated view with the HTTP method it accepts.

    Args:
        client: Logged-in test client.
        view_name: URL name of the view.
        args: Positional reverse() arguments.

    Returns:
        The response.
    """
    url = reverse(view_name, args=args)
    return client.post(url) if view_name in POST_ONLY_VIEWS else client.get(url)


@pytest.mark.django_db
class TestBillingAndSettingsRequireAdmin:
    """Verify billing and workspace-settings views enforce the admin role."""

    @pytest.mark.parametrize(("view_name", "args"), GATED_VIEWS)
    def test_non_admin_member_is_denied(
        self,
        authenticated_user_client: Client,
        view_name: str,
        args: list[str],
    ) -> None:
        """A ``role="user"`` member is redirected to the dashboard, not served.

        Args:
            authenticated_user_client: Client logged in as a plain member.
            view_name: The URL name of the gated view under test.
            args: Positional reverse() arguments for the view.
        """
        response = _fetch(authenticated_user_client, view_name, args)

        assert response.status_code == 302
        assert response.url == reverse("core:dashboard")

    @pytest.mark.parametrize(("view_name", "args"), GATED_VIEWS)
    def test_admin_member_is_not_denied(
        self,
        authenticated_admin_client: Client,
        view_name: str,
        args: list[str],
    ) -> None:
        """An admin passes the role gate (never bounced to the dashboard).

        Stripe is mocked so ``checkout`` exercises the authorization path
        without making a network call; the assertion only proves the admin
        cleared the permission gate.

        Args:
            authenticated_admin_client: Client logged in as an admin.
            view_name: The URL name of the gated view under test.
            args: Positional reverse() arguments for the view.
        """
        with patch(
            "core.services.stripe.StripeAPI.get_or_create_customer",
            return_value=None,
        ):
            response = _fetch(authenticated_admin_client, view_name, args)

        # The permission gate redirects denied users to the dashboard; an
        # admin must land anywhere but there (200, or a redirect elsewhere
        # such as the upgrade page when no Stripe customer exists yet).
        if response.status_code == 302:
            assert response.url != reverse("core:dashboard")
        else:
            assert response.status_code == 200

    @pytest.mark.parametrize(("view_name", "args"), GATED_VIEWS)
    def test_owner_member_is_not_denied(
        self,
        authenticated_owner_client: Client,
        view_name: str,
        args: list[str],
    ) -> None:
        """An owner passes the role gate (never bounced to the dashboard).

        Args:
            authenticated_owner_client: Client logged in as the owner.
            view_name: The URL name of the gated view under test.
            args: Positional reverse() arguments for the view.
        """
        with patch(
            "core.services.stripe.StripeAPI.get_or_create_customer",
            return_value=None,
        ):
            response = _fetch(authenticated_owner_client, view_name, args)

        if response.status_code == 302:
            assert response.url != reverse("core:dashboard")
        else:
            assert response.status_code == 200


@pytest.mark.django_db
class TestWorkspaceSettingsMutation:
    """Verify workspace_settings mutations are gated behind the admin role."""

    def test_non_admin_cannot_update_workspace(
        self,
        authenticated_user_client: Client,
        workspace,  # noqa: ANN001 - fixture from tests/core/conftest.py
        user_member,  # noqa: ANN001 - ensures the user is a plain member
    ) -> None:
        """A plain member's POST must not change the name or shop_domain.

        shop_domain participates in webhook routing / tenant identity, so a
        non-admin must not be able to change it.

        Args:
            authenticated_user_client: Client logged in as a plain member.
            workspace: The workspace under test.
            user_member: The plain-member membership for the logged-in user.
        """
        original_name = workspace.name
        original_domain = workspace.shop_domain

        response = authenticated_user_client.post(
            reverse("core:workspace_settings"),
            {"name": "Hijacked Name", "shop_domain": "evil.myshopify.com"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:dashboard")

        workspace.refresh_from_db()
        assert workspace.name == original_name
        assert workspace.shop_domain == original_domain

    def test_admin_can_update_workspace(
        self,
        authenticated_admin_client: Client,
        workspace,  # noqa: ANN001 - fixture from tests/core/conftest.py
        admin_member,  # noqa: ANN001 - ensures the user is an admin
    ) -> None:
        """An admin's POST updates the workspace name and shop_domain.

        Args:
            authenticated_admin_client: Client logged in as an admin.
            workspace: The workspace under test.
            admin_member: The admin membership for the logged-in user.
        """
        response = authenticated_admin_client.post(
            reverse("core:workspace_settings"),
            {"name": "Renamed Workspace", "shop_domain": "renamed.myshopify.com"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:workspace_settings")

        workspace.refresh_from_db()
        assert workspace.name == "Renamed Workspace"
        assert workspace.shop_domain == "renamed.myshopify.com"

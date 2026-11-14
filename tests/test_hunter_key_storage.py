"""Tests that the Hunter.io API key is stored in the encrypted credentials.

Covers the connect view, the service reader (including the fallback to the
legacy plaintext location) and the 0037 data migration.
"""

from importlib import import_module
from typing import Callable
from unittest.mock import patch

import pytest
from core.models import Integration, Workspace, WorkspaceMember
from core.services.email_enrichment import EmailEnrichmentService, get_hunter_api_key
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.urls import reverse

API_KEY = "a" * 40


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create a Pro workspace (Hunter.io requires Pro or higher)."""
    return Workspace.objects.create(
        name="Hunter WS", subscription_plan="pro", subscription_status="active"
    )


@pytest.fixture
def admin_client(workspace: Workspace) -> Client:
    """Return a client logged in as the workspace owner."""
    user = User.objects.create_user(
        username="hunter-owner", email="owner@example.com", password="x"
    )
    WorkspaceMember.objects.create(user=user, workspace=workspace, role="owner")
    client = Client()
    client.force_login(user)
    return client


def _raw_column(integration: Integration, column: str) -> str:
    """Read a column straight from the database, bypassing field decoding.

    Args:
        integration: The integration row.
        column: The column name.

    Returns:
        The stored value as text.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {column} FROM core_integration WHERE id = %s",  # noqa: S608
            [integration.pk],
        )
        return str(cursor.fetchone()[0])


@pytest.mark.parametrize("existing", [False, True])
def test_connect_view_stores_key_encrypted(
    existing: bool, workspace: Workspace, admin_client: Client
) -> None:
    """Connecting (or reconnecting) saves the key only in encrypted form."""
    if existing:
        Integration.objects.create(
            workspace=workspace,
            integration_type="hunter_enrichment",
            integration_settings={"api_key": "b" * 40},
            is_active=False,
        )

    with patch(
        "core.views.integrations.hunter.HunterPlugin.verify_api_key",
        return_value=(True, "ok"),
    ):
        response = admin_client.post(
            reverse("core:integrate_hunter"), {"api_key": API_KEY}
        )

    assert response.status_code == 302
    integration = Integration.objects.get(
        workspace=workspace, integration_type="hunter_enrichment"
    )
    assert integration.oauth_credentials == {"api_key": API_KEY}
    assert "api_key" not in integration.integration_settings
    assert API_KEY not in _raw_column(integration, "integration_settings")
    assert API_KEY not in _raw_column(integration, "oauth_credentials")
    assert get_hunter_api_key(integration) == API_KEY


def test_service_reads_encrypted_key(workspace: Workspace) -> None:
    """EmailEnrichmentService finds a key stored in oauth_credentials."""
    Integration.objects.create(
        workspace=workspace,
        integration_type="hunter_enrichment",
        oauth_credentials={"api_key": API_KEY},
    )

    assert EmailEnrichmentService()._get_hunter_api_key(workspace) == API_KEY


def test_service_falls_back_to_legacy_settings_key(workspace: Workspace) -> None:
    """A key not yet migrated out of integration_settings still works."""
    Integration.objects.create(
        workspace=workspace,
        integration_type="hunter_enrichment",
        integration_settings={"api_key": API_KEY},
    )

    assert EmailEnrichmentService()._get_hunter_api_key(workspace) == API_KEY


def _migration_function(name: str) -> Callable[..., None]:
    """Import a function from the 0037 data migration.

    Args:
        name: The function name.

    Returns:
        The migration function.
    """
    module = import_module(
        "core.migrations.0037_move_hunter_api_key_to_encrypted_credentials"
    )
    return getattr(module, name)  # type: ignore[no-any-return]


def test_migration_moves_plaintext_key_to_credentials(workspace: Workspace) -> None:
    """The data migration encrypts existing keys and removes the plaintext."""
    integration = Integration.objects.create(
        workspace=workspace,
        integration_type="hunter_enrichment",
        integration_settings={"api_key": API_KEY, "other": "kept"},
    )
    other = Integration.objects.create(
        workspace=workspace,
        integration_type="slack_notifications",
        integration_settings={"api_key": "not-hunter"},
    )

    _migration_function("move_keys_to_credentials")(django_apps, None)

    integration.refresh_from_db()
    assert integration.integration_settings == {"other": "kept"}
    assert integration.oauth_credentials == {"api_key": API_KEY}
    assert API_KEY not in _raw_column(integration, "integration_settings")
    assert API_KEY not in _raw_column(integration, "oauth_credentials")
    other.refresh_from_db()
    assert other.integration_settings == {"api_key": "not-hunter"}


def test_migration_reverse_restores_plaintext_location(workspace: Workspace) -> None:
    """Reversing the migration puts the key back where older code reads it."""
    integration = Integration.objects.create(
        workspace=workspace,
        integration_type="hunter_enrichment",
        oauth_credentials={"api_key": API_KEY},
    )

    _migration_function("move_keys_back_to_settings")(django_apps, None)

    integration.refresh_from_db()
    assert integration.integration_settings == {"api_key": API_KEY}
    assert integration.oauth_credentials == {}

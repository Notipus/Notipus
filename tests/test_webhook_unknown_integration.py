"""Webhooks for unknown or disconnected integrations answer 404, not 500.

A 500 makes the provider retry for days and then disable the endpoint;
404 says plainly that nothing lives at the address any more.
"""

import uuid

import pytest
from core.models import Integration, Workspace
from django.test import Client


@pytest.mark.django_db
@pytest.mark.parametrize("provider", ["stripe", "chargify"])
def test_unknown_workspace_returns_404(client: Client, provider: str) -> None:
    """A workspace UUID that does not exist is a 404."""
    response = client.post(
        f"/webhook/customer/{uuid.uuid4()}/{provider}/",
        data="{}",
        content_type="application/json",
    )
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("provider", "integration_type"),
    [("stripe", "stripe_customer"), ("chargify", "chargify")],
)
def test_disconnected_integration_returns_404(
    client: Client, provider: str, integration_type: str
) -> None:
    """An inactive (disconnected) integration is a 404."""
    workspace = Workspace.objects.create(name="Disconnected")
    Integration.objects.create(
        workspace=workspace,
        integration_type=integration_type,
        webhook_secret="secret",
        is_active=False,
    )
    response = client.post(
        f"/webhook/customer/{workspace.uuid}/{provider}/",
        data="{}",
        content_type="application/json",
    )
    assert response.status_code == 404

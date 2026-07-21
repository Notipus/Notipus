"""Tests for emailing integration setup instructions to a colleague.

Covers the email_setup_instructions view (permissions, validation,
provider gating) and the send_setup_instructions_email helper (content
of the generated email).
"""

import pytest
from core.models import Workspace, WorkspaceMember
from core.views.integrations.email_instructions import (
    PROVIDER_INSTRUCTIONS,
    send_setup_instructions_email,
)
from django.core import mail
from django.test import Client
from django.urls import reverse


@pytest.fixture(autouse=True)
def locmem_email(settings) -> None:
    """Route all emails to the in-memory outbox."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def owner_client(owner_user, owner_member) -> Client:
    """Client logged in as the workspace owner."""
    client = Client()
    client.force_login(owner_user)
    return client


@pytest.fixture
def member_client(regular_user, workspace) -> Client:
    """Client logged in as a non-admin workspace member."""
    WorkspaceMember.objects.create(user=regular_user, workspace=workspace, role="user")
    client = Client()
    client.force_login(regular_user)
    return client


class TestEmailSetupInstructionsView:
    """Test the POST endpoint that emails setup instructions."""

    def test_sends_email_for_stripe(
        self, owner_client: Client, workspace: Workspace
    ) -> None:
        """Test a valid request sends one email to the colleague."""
        response = owner_client.post(
            reverse("core:email_setup_instructions", args=["stripe"]),
            {"recipient_email": "cfo@example.com"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrate_stripe")
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["cfo@example.com"]

    def test_sends_email_for_chargify(
        self, owner_client: Client, workspace: Workspace
    ) -> None:
        """Test the Chargify variant sends and redirects to its page."""
        response = owner_client.post(
            reverse("core:email_setup_instructions", args=["chargify"]),
            {"recipient_email": "billing@example.com"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrate_chargify")
        assert len(mail.outbox) == 1

    def test_unknown_provider_rejected(
        self, owner_client: Client, workspace: Workspace
    ) -> None:
        """Test an unsupported provider sends nothing."""
        response = owner_client.post(
            reverse("core:email_setup_instructions", args=["shopify"]),
            {"recipient_email": "cfo@example.com"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        assert len(mail.outbox) == 0

    def test_invalid_email_rejected(
        self, owner_client: Client, workspace: Workspace
    ) -> None:
        """Test a malformed recipient address sends nothing."""
        response = owner_client.post(
            reverse("core:email_setup_instructions", args=["stripe"]),
            {"recipient_email": "not-an-email"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrate_stripe")
        assert len(mail.outbox) == 0

    def test_missing_email_rejected(
        self, owner_client: Client, workspace: Workspace
    ) -> None:
        """Test an empty recipient sends nothing."""
        response = owner_client.post(
            reverse("core:email_setup_instructions", args=["stripe"]), {}
        )

        assert response.status_code == 302
        assert len(mail.outbox) == 0

    def test_get_method_rejected(
        self, owner_client: Client, workspace: Workspace
    ) -> None:
        """Test GET requests are redirected without sending."""
        response = owner_client.get(
            reverse("core:email_setup_instructions", args=["stripe"])
        )

        assert response.status_code == 302
        assert len(mail.outbox) == 0

    def test_non_admin_rejected(
        self, member_client: Client, workspace: Workspace
    ) -> None:
        """Test regular members cannot send setup instructions."""
        response = member_client.post(
            reverse("core:email_setup_instructions", args=["stripe"]),
            {"recipient_email": "cfo@example.com"},
        )

        assert response.status_code == 302
        assert len(mail.outbox) == 0

    def test_anonymous_rejected(self, db) -> None:
        """Test unauthenticated users are redirected to login."""
        response = Client().post(
            reverse("core:email_setup_instructions", args=["stripe"]),
            {"recipient_email": "cfo@example.com"},
        )

        assert response.status_code == 302
        assert len(mail.outbox) == 0


class TestSendSetupInstructionsEmail:
    """Test the content of the generated instruction email."""

    def test_stripe_email_contains_webhook_url_and_events(
        self, workspace: Workspace
    ) -> None:
        """Test the Stripe email carries the URL and every event."""
        result = send_setup_instructions_email(
            "cfo@example.com",
            workspace,
            "Alice Admin",
            PROVIDER_INSTRUCTIONS["stripe"],
        )

        assert result is True
        message = mail.outbox[0]
        expected_url = f"/webhook/customer/{workspace.uuid}/stripe/"
        html_body = message.alternatives[0][0]
        assert expected_url in message.body
        assert expected_url in html_body
        for event in PROVIDER_INSTRUCTIONS["stripe"].webhook_events:
            assert event in message.body
            assert event in html_body

    def test_email_names_the_requester(self, workspace: Workspace) -> None:
        """Test the colleague can see who asked for the setup."""
        send_setup_instructions_email(
            "cfo@example.com",
            workspace,
            "Alice Admin",
            PROVIDER_INSTRUCTIONS["stripe"],
        )

        message = mail.outbox[0]
        assert "Alice Admin" in message.body
        assert "Alice Admin" in message.alternatives[0][0]

    def test_email_never_contains_a_secret_value(self, workspace: Workspace) -> None:
        """Test instructions ask for a secure channel, not email replies.

        The email must instruct the colleague where to FIND the secret,
        never carry one itself, and steer the handover to a secure
        channel.
        """
        send_setup_instructions_email(
            "cfo@example.com",
            workspace,
            "Alice Admin",
            PROVIDER_INSTRUCTIONS["stripe"],
        )

        message = mail.outbox[0]
        html_body = message.alternatives[0][0]
        assert "secure channel" in message.body
        assert "not by replying to this email" in message.body
        assert "secure channel" in html_body
        assert "not by replying to this email" in html_body

    def test_subject_names_provider_and_workspace(self, workspace: Workspace) -> None:
        """Test the subject line is self-explanatory in an inbox."""
        send_setup_instructions_email(
            "cfo@example.com",
            workspace,
            "Alice Admin",
            PROVIDER_INSTRUCTIONS["chargify"],
        )

        subject = mail.outbox[0].subject
        assert "Chargify" in subject
        assert workspace.name in subject

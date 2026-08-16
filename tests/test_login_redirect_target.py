"""Tests that an interrupted journey resumes after signing in.

Someone who clicks "Add to Slack" on the marketing site is anonymous, so
they hit the login page first. Before this, they landed on the dashboard
afterwards and the thing they came to do was simply gone. These cover the
destination surviving the round trip, and not being usable to bounce a
freshly authenticated user off-site.
"""

import json
from unittest.mock import patch

import pytest
from core.views.webauthn import _safe_redirect_target
from django.contrib.auth.models import User
from django.test import Client, RequestFactory


class TestSafeRedirectTarget:
    """Validation of the post-login destination."""

    @pytest.mark.parametrize(
        "candidate",
        [
            "/integrate/slack/",
            "/dashboard/",
            "/billing/?upgraded=1",
        ],
    )
    def test_accepts_relative_paths(self, candidate: str) -> None:
        """Same-origin paths are handed back unchanged."""
        request = RequestFactory().post("/webauthn/authenticate/complete/")

        assert _safe_redirect_target(request, candidate) == candidate

    @pytest.mark.parametrize(
        "candidate",
        [
            "https://evil.example/steal",
            "//evil.example/steal",
            "http://evil.example",
            "javascript:alert(1)",
        ],
    )
    def test_rejects_off_site_destinations(self, candidate: str) -> None:
        """A crafted next must not bounce an authenticated user away.

        Without this, /accounts/login/?next=https://evil.example would
        hand a just-signed-in user straight to another origin.
        """
        request = RequestFactory().post("/webauthn/authenticate/complete/")

        assert _safe_redirect_target(request, candidate) == "/dashboard/"

    @pytest.mark.parametrize("candidate", [None, ""])
    def test_falls_back_to_dashboard(self, candidate: str | None) -> None:
        """No destination means the usual landing page."""
        request = RequestFactory().post("/webauthn/authenticate/complete/")

        assert _safe_redirect_target(request, candidate) == "/dashboard/"


@pytest.mark.django_db
class TestAnonymousGatePreservesDestination:
    """The gate in front of admin-only pages."""

    def test_integrate_slack_redirects_to_login_carrying_next(self) -> None:
        """The Add to Slack path is the one that motivated this."""
        response = Client().get("/integrate/slack/")

        assert response.status_code == 302
        assert response.url.startswith("/accounts/login/")
        assert "next=/integrate/slack/" in response.url

    def test_query_string_survives(self) -> None:
        """The whole path is preserved, not just the route."""
        response = Client().get("/integrations/?highlight=slack")

        assert response.status_code == 302
        assert "highlight" in response.url


@pytest.mark.django_db
class TestLoginPageCarriesDestination:
    """The login page has to hand the destination to both sign-in methods."""

    def test_both_methods_receive_the_destination(self) -> None:
        """Otherwise the gate preserves an intent the login page discards."""
        client = Client()
        gate = client.get("/integrate/slack/")
        page = client.get(gate.url)
        html = page.content.decode()

        assert page.status_code == 200
        # Passkey: JS reads it off the button and posts it back.
        assert 'data-next="/integrate/slack/"' in html
        # Slack OAuth: allauth threads it through the provider round-trip.
        assert (
            "next=%2Fintegrate%2Fslack%2F" in html or "next=/integrate/slack/" in html
        )


@pytest.mark.django_db
class TestPasskeyHonoursDestination:
    """The passkey endpoint returns where the browser should go next."""

    def _authenticate(self, next_value: str) -> dict:
        """Complete a passkey authentication asking for ``next_value``.

        Args:
            next_value: Requested post-login destination.

        Returns:
            The parsed JSON response.
        """
        user = User.objects.create_user(username="peter", email="peter@initech.com")
        with patch(
            "core.views.webauthn.WebAuthnService.verify_authentication",
            return_value=user,
        ):
            response = Client().post(
                "/webauthn/authenticate/complete/",
                data=json.dumps({"credential": {"id": "x"}, "next": next_value}),
                content_type="application/json",
            )
        return json.loads(response.content)

    def test_returns_the_requested_path(self) -> None:
        """A safe destination comes back for the browser to follow."""
        assert self._authenticate("/integrate/slack/")["redirect_url"] == (
            "/integrate/slack/"
        )

    def test_ignores_an_off_site_destination(self) -> None:
        """The endpoint is the authority, so the client cannot be tricked."""
        assert self._authenticate("https://evil.example")["redirect_url"] == (
            "/dashboard/"
        )

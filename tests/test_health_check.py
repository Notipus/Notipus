"""Tests for the liveness probe.

Fly's health check and nginx's /health/ location both depend on this
answering 200 to an unauthenticated GET. It used to carry @csrf_exempt,
which did nothing - Django only enforces CSRF on unsafe methods - so the
decorator went along with its suppression. These pin the behaviour that
was being relied on, so the removal cannot regress unnoticed.
"""

import json

from django.test import Client


class TestHealthCheck:
    """The probe answers without a session or a token."""

    def test_anonymous_get_is_healthy(self) -> None:
        """An unauthenticated GET returns 200 and the healthy payload."""
        response = Client().get("/webhook/health/")

        assert response.status_code == 200
        assert json.loads(response.content) == {
            "status": "healthy",
            "service": "webhook-processor",
        }

    def test_csrf_enforcement_does_not_block_it(self) -> None:
        """CSRF checks never applied to this GET, and still do not."""
        response = Client(enforce_csrf_checks=True).get("/webhook/health/")

        assert response.status_code == 200

    def test_post_is_rejected(self) -> None:
        """Only GET is served.

        Without @csrf_exempt an untokened POST is turned away by the CSRF
        middleware before require_http_methods gets to answer 405, so this
        accepts either refusal - what matters is that it is not served.
        """
        assert Client().post("/webhook/health/").status_code == 405
        assert Client(enforce_csrf_checks=True).post(
            "/webhook/health/"
        ).status_code in (403, 405)

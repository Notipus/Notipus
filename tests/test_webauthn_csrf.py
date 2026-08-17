"""Tests that the WebAuthn ceremony endpoints are CSRF-protected.

All six of these used to carry @csrf_exempt, because the passkey fetches on
the login and signup pages did not send a token. The exemption meant any
site could POST to them on a visitor's behalf: overwriting a signed-in
user's pending challenge, or probing /webauthn/signup/begin/ cross-origin
for whether a username or email is taken.

The ceremony itself is not the thing at risk - verify_registration and
verify_authentication both bind expected_origin and expected_rp_id, so a
credential cannot be forged from another origin. These cover the endpoints
being closed to off-site callers in the first place, and the token actually
reaching them from the pages that legitimately call them.
"""

import json

import pytest
from django.test import Client

CEREMONY_ENDPOINTS = [
    "/webauthn/register/begin/",
    "/webauthn/register/complete/",
    "/webauthn/authenticate/begin/",
    "/webauthn/authenticate/complete/",
    "/webauthn/signup/begin/",
    "/webauthn/signup/complete/",
]


@pytest.mark.django_db
class TestCeremoniesRejectUntokenedPosts:
    """A POST without a CSRF token does not reach the view."""

    @pytest.mark.parametrize("endpoint", CEREMONY_ENDPOINTS)
    def test_post_without_token_is_forbidden(self, endpoint: str) -> None:
        """Every ceremony endpoint refuses an untokened cross-site POST.

        Args:
            endpoint: The ceremony URL under test.
        """
        response = Client(enforce_csrf_checks=True).post(
            endpoint,
            data=json.dumps({"username": "peter", "email": "peter@initech.com"}),
            content_type="application/json",
        )

        assert response.status_code == 403

    def test_signup_begin_does_not_answer_whether_a_user_exists(self) -> None:
        """The enumeration oracle is closed before it can answer.

        /webauthn/signup/begin/ reports "Username already exists", which is
        unavoidable on a signup form but must not be readable from another
        origin.
        """
        response = Client(enforce_csrf_checks=True).post(
            "/webauthn/signup/begin/",
            data=json.dumps({"username": "peter", "email": "peter@initech.com"}),
            content_type="application/json",
        )

        assert response.status_code == 403
        assert b"already exists" not in response.content


@pytest.mark.django_db
class TestPagesSendTheToken:
    """The pages that legitimately call the ceremonies still work."""

    @pytest.mark.parametrize(
        "page",
        ["/accounts/login/", "/accounts/signup/"],
    )
    def test_page_defines_a_csrf_token_for_its_fetches(self, page: str) -> None:
        """The passkey script gets a real token rendered into the page.

        Reading the cookie is not an option: CSRF_COOKIE_HTTPONLY is set in
        production, so window.document.cookie cannot see it.

        Args:
            page: The page whose passkey script is under test.
        """
        html = Client().get(page).content.decode()

        assert 'const CSRF_TOKEN = "' in html
        assert 'const CSRF_TOKEN = ""' not in html
        assert "'X-CSRFToken': CSRF_TOKEN," in html

    def test_a_tokened_post_reaches_the_view(self) -> None:
        """With the header the request is served, not rejected at the door."""
        client = Client(enforce_csrf_checks=True)
        client.get("/accounts/login/")
        token = client.cookies["csrftoken"].value

        response = client.post(
            "/webauthn/authenticate/begin/",
            data=json.dumps({}),
            content_type="application/json",
            headers={"x-csrftoken": token},
        )

        assert response.status_code == 200

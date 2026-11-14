"""Tests for the custom error handlers in core.views.errors."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.views.errors import STATIC_500_HTML, custom_500
from django.db import OperationalError
from django.test import RequestFactory


def _signed_in_request(request_factory: RequestFactory) -> Any:
    """Build a request whose user is authenticated, so the nav needs the DB.

    Args:
        request_factory: Django's request factory.

    Returns:
        A GET request carrying an authenticated stand-in user.
    """
    request = request_factory.get("/broken/")
    request.user = MagicMock(is_authenticated=True, is_staff=False)
    return request


def test_500_page_survives_a_database_outage(request_factory: RequestFactory) -> None:
    """A DB error inside a context processor still yields a 500 page.

    workspace_role queries WorkspaceMember for the signed-in user. With the
    database down that query raises, and before the fallback the handler
    raised with it, so the visitor saw no error page at all.
    """
    request = _signed_in_request(request_factory)
    with patch("core.context_processors.WorkspaceMember") as member_model:
        member_model.objects.filter.side_effect = OperationalError("db is down")
        response = custom_500(request)

    assert response.status_code == 500
    assert response.content.decode() == STATIC_500_HTML
    assert "Server error" in response.content.decode()


@pytest.mark.django_db
def test_500_page_renders_the_styled_template_normally(
    request_factory: RequestFactory,
) -> None:
    """With a working database the regular branded page is served."""
    request = request_factory.get("/broken/")
    response = custom_500(request)

    assert response.status_code == 500
    body = response.content.decode()
    assert "Server error" in body
    assert body != STATIC_500_HTML

"""Tests for the atomic dedup claim and for charging quota after dedup.

Dedup used to be check-then-record around slow synchronous delivery, so
two concurrent deliveries of one event both passed the check and both
notified. The key is now claimed with cache.add before processing.

Quota used to be charged for every authenticated request; it is now
charged only for events that are parsed, pass dedup and get processed.
"""

import json
from typing import Any, Generator
from unittest.mock import Mock, patch

import pytest
from core.models import Workspace
from django.core.cache import cache
from django.http import JsonResponse
from django.test import override_settings
from webhooks.services.rate_limiter import rate_limiter
from webhooks.webhook_router import _process_webhook_data


@pytest.fixture
def locmem_cache() -> Generator[None, None, None]:
    """Use a real cache so cache.add has SET NX semantics.

    Yields:
        Nothing; the cache is cleared around the test.
    """
    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        cache.clear()
        yield
        cache.clear()


@pytest.fixture
def provider() -> Mock:
    """A source provider stub with empty customer data.

    Returns:
        The mock provider.
    """
    mock = Mock()
    mock.get_customer_data.return_value = {}
    return mock


def _event() -> dict[str, Any]:
    """Build a Chargify-style event keyed on its signed body hash.

    Returns:
        The event dictionary.
    """
    return {
        "type": "payment_success",
        "provider": "chargify",
        "customer_id": "cust_1",
        "content_hash": "abc123",
        "amount": 10.0,
    }


def _ok() -> JsonResponse:
    """Return a success response like the immediate path does.

    Returns:
        A 200 JsonResponse.
    """
    return JsonResponse({"status": "success", "message": "delivered"}, status=200)


def test_concurrent_duplicate_gets_in_progress(
    locmem_cache: None, provider: Mock
) -> None:
    """A duplicate arriving mid-delivery is not delivered a second time."""
    inner: list[JsonResponse] = []

    def deliver(*args: Any, **kwargs: Any) -> JsonResponse:
        # The provider redelivers while the first delivery is still running.
        inner.append(
            _process_webhook_data(_event(), provider, "customer_chargify", None)
        )
        return _ok()

    with patch(
        "webhooks.webhook_router._process_immediately", side_effect=deliver
    ) as mock_deliver:
        first = _process_webhook_data(_event(), provider, "customer_chargify", None)

    assert first.status_code == 200
    assert mock_deliver.call_count == 1
    assert inner[0].status_code == 200
    assert "duplicate in progress" in json.loads(inner[0].content)["message"]

    # Once done, a redelivery is a plain duplicate.
    with patch("webhooks.webhook_router._process_immediately") as mock_deliver:
        third = _process_webhook_data(_event(), provider, "customer_chargify", None)
    mock_deliver.assert_not_called()
    assert "duplicate suppressed" in json.loads(third.content)["message"]


def test_failed_delivery_releases_claim(locmem_cache: None, provider: Mock) -> None:
    """A failure frees the key so the provider's retry is processed."""
    with patch(
        "webhooks.webhook_router._process_immediately",
        side_effect=RuntimeError("slack down"),
    ):
        with pytest.raises(RuntimeError):
            _process_webhook_data(_event(), provider, "customer_chargify", None)

    with patch(
        "webhooks.webhook_router._process_immediately", return_value=_ok()
    ) as mock_deliver:
        retry = _process_webhook_data(_event(), provider, "customer_chargify", None)

    mock_deliver.assert_called_once()
    assert json.loads(retry.content)["message"] == "delivered"


@pytest.mark.django_db
def test_duplicate_is_not_charged_quota(locmem_cache: None, provider: Mock) -> None:
    """Only the delivery that is processed counts against the quota."""
    workspace = Workspace.objects.create(name="Quota")
    info = {
        "limit": 100,
        "current_usage": 1,
        "remaining": 99,
        "reset_time": None,
        "plan": "free",
    }
    with (
        patch.object(rate_limiter, "enforce_rate_limit", return_value=info) as charge,
        patch.object(rate_limiter, "get_rate_limit_headers", return_value={}),
        patch("webhooks.webhook_router._process_immediately", return_value=_ok()),
    ):
        _process_webhook_data(_event(), provider, "customer_chargify", workspace)
        _process_webhook_data(_event(), provider, "customer_chargify", workspace)

    charge.assert_called_once()


@pytest.mark.django_db
def test_rate_limited_event_releases_claim(locmem_cache: None, provider: Mock) -> None:
    """A 429 must not leave the event marked as in progress."""
    from datetime import datetime, timezone

    from webhooks.services.rate_limiter import RateLimitException

    workspace = Workspace.objects.create(name="Capped")
    exc = RateLimitException(
        "capped", limit=1, current_usage=2, reset_time=datetime.now(timezone.utc)
    )
    with (
        patch.object(rate_limiter, "enforce_rate_limit", side_effect=exc),
        patch("webhooks.webhook_router._process_immediately") as mock_deliver,
    ):
        first = _process_webhook_data(
            _event(), provider, "customer_chargify", workspace
        )
    assert first.status_code == 429
    mock_deliver.assert_not_called()

    with (
        patch.object(
            rate_limiter,
            "enforce_rate_limit",
            return_value={
                "limit": 1,
                "current_usage": 1,
                "remaining": 0,
                "reset_time": None,
                "plan": "free",
            },
        ),
        patch.object(rate_limiter, "get_rate_limit_headers", return_value={}),
        patch(
            "webhooks.webhook_router._process_immediately", return_value=_ok()
        ) as mock_deliver,
    ):
        retry = _process_webhook_data(
            _event(), provider, "customer_chargify", workspace
        )
    assert retry.status_code == 200
    mock_deliver.assert_called_once()

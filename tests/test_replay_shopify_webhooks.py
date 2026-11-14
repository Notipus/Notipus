"""Tests for the replay_shopify_webhooks management command.

The command used to check a dedup key the live path never writes (the
bare order id), so it resent everything; it crashed on guest checkouts
and null line-item prices; it kept only the first webhook per body id;
and it mapped unknown topics to payment_success.
"""

import base64
import hashlib
import hmac
import json
from io import StringIO
from typing import Any, Generator
from unittest.mock import patch

import pytest
from core.models import Workspace
from django.core.cache import cache
from django.core.management import call_command
from django.http import JsonResponse
from django.test import RequestFactory, override_settings
from plugins.sources.shopify import ShopifySourcePlugin
from webhooks.services.webhook_storage import webhook_storage_service
from webhooks.webhook_router import _get_dedup_key, _record_dedup_marker

ORDER: dict[str, Any] = {
    "id": 4100,
    "order_number": 1001,
    "total_price": "25.00",
    "currency": "USD",
    "customer": {"id": 77, "email": "buyer@example.com"},
    "line_items": [{"name": "Widget", "quantity": 1, "price": "25.00"}],
}


@pytest.fixture
def locmem_cache() -> Generator[None, None, None]:
    """Use a real in-memory cache for storage and dedup.

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
def workspace(db: Any) -> Workspace:
    """Create the workspace being replayed.

    Args:
        db: pytest-django database fixture.

    Returns:
        The workspace.
    """
    return Workspace.objects.create(name="Replay Shop", slug="replay-shop")


def _shopify_request(topic: str, body: Any) -> Any:
    """Build a signed Shopify request.

    Args:
        topic: The Shopify topic.
        body: The JSON body.

    Returns:
        The request.
    """
    raw = json.dumps(body).encode()
    digest = base64.b64encode(hmac.new(b"s", raw, hashlib.sha256).digest()).decode()
    return RequestFactory().post(
        "/webhook/",
        data=raw,
        content_type="application/json",
        HTTP_X_SHOPIFY_TOPIC=topic,
        HTTP_X_SHOPIFY_HMAC_SHA256=digest,
    )


def _store(workspace: Workspace, topic: str, body: Any) -> Any:
    """Store a webhook the way the live endpoint does.

    Args:
        workspace: Owning workspace.
        topic: The Shopify topic.
        body: The JSON body.

    Returns:
        The request that was stored.
    """
    request = _shopify_request(topic, body)
    assert webhook_storage_service.store_webhook(
        request, "shopify", str(workspace.uuid)
    )
    return request


def _run(*args: str) -> str:
    """Run the command and return its output.

    Args:
        *args: Extra command arguments.

    Returns:
        Captured stdout.
    """
    out = StringIO()
    call_command(
        "replay_shopify_webhooks", "--workspace", "replay-shop", *args, stdout=out
    )
    return out.getvalue()


def _ok(*args: Any, **kwargs: Any) -> JsonResponse:
    """Stand in for a successful delivery.

    Args:
        *args: Ignored.
        **kwargs: Ignored.

    Returns:
        A 200 response.
    """
    return JsonResponse({"status": "success", "message": "delivered"})


def test_event_processed_live_is_not_resent(
    locmem_cache: None, workspace: Workspace
) -> None:
    """The live dedup key (shopify:{type}:sha256:{hash}) is honoured."""
    request = _store(workspace, "orders/paid", ORDER)
    live_event = ShopifySourcePlugin("s").parse_webhook(request)
    assert live_event is not None
    _record_dedup_marker(live_event, str(workspace.uuid), _get_dedup_key(live_event))

    with patch(
        "webhooks.management.commands.replay_shopify_webhooks._process_immediately"
    ) as deliver:
        output = _run()

    deliver.assert_not_called()
    assert "Skipped (already processed): 1" in output


def test_guest_checkout_and_null_price_do_not_crash(
    locmem_cache: None, workspace: Workspace
) -> None:
    """customer: null and price: null are handled like the live parser."""
    _store(
        workspace,
        "orders/paid",
        {**ORDER, "customer": None, "line_items": [{"name": "Gift", "price": None}]},
    )
    output = _run("--dry-run")
    assert "Processed: 1" in output
    assert "Errors: 0" in output


def test_every_topic_for_an_order_is_replayed_and_unknown_topics_skipped(
    locmem_cache: None, workspace: Workspace
) -> None:
    """orders/create and orders/paid for one order are separate events."""
    _store(workspace, "orders/create", ORDER)
    _store(workspace, "orders/paid", ORDER)
    _store(workspace, "inventory_levels/update", {"id": 4100})

    with patch(
        "webhooks.management.commands.replay_shopify_webhooks._process_immediately",
        side_effect=_ok,
    ) as deliver:
        output = _run()

    types = sorted(call.args[0]["type"] for call in deliver.call_args_list)
    assert types == ["order_created", "payment_success"]
    assert "Skipped (unsupported topic): 1" in output


def test_bad_payload_does_not_stop_the_run(
    locmem_cache: None, workspace: Workspace
) -> None:
    """Each webhook is isolated; one failure is counted, others proceed."""
    _store(workspace, "orders/paid", ["not", "an", "object"])
    _store(workspace, "orders/paid", ORDER)

    with patch(
        "webhooks.management.commands.replay_shopify_webhooks._process_immediately",
        side_effect=_ok,
    ):
        output = _run()

    assert "Processed: 1" in output
    assert "Errors: 1" in output


def test_replayed_event_is_dedup_marked_and_dry_run_sends_nothing(
    locmem_cache: None, workspace: Workspace
) -> None:
    """A replay records the live key; dry-run delivers nothing."""
    _store(workspace, "orders/paid", ORDER)

    with patch(
        "webhooks.management.commands.replay_shopify_webhooks._process_immediately",
        side_effect=_ok,
    ) as deliver:
        _run("--dry-run")
        deliver.assert_not_called()
        _run()
        assert deliver.call_count == 1
        second = _run()
        assert deliver.call_count == 1

    assert "Skipped (already processed): 1" in second

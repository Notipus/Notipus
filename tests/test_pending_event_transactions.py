"""Separate payments for one customer must not merge into one notification.

Events without a Stripe idempotency key (renewals, dashboard actions) are
bucketed per customer for 60 seconds so a subscription event can borrow
the email from its invoice. Only the subscription and invoice events of
ONE transaction may merge; two invoices are two notifications.
"""

from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from core.encrypted_cache import decrypt_cache_value
from webhooks.services.pending_event_queue import PendingEventQueue


@pytest.fixture
def queue() -> PendingEventQueue:
    """Create a queue with no live timers.

    Returns:
        The queue.
    """
    queue = PendingEventQueue()
    with queue._lock:
        queue._active_timers.clear()
    return queue


@pytest.fixture
def stateful_cache() -> Generator[MagicMock, None, None]:
    """Back the queue's cache with a dict supporting get/set/add/delete.

    Yields:
        The patched cache mock, with the dict on ``.data``.
    """
    data: dict[str, Any] = {}

    def add(key: str, value: Any, timeout: Any = None) -> bool:
        if key in data:
            return False
        data[key] = value
        return True

    with patch("webhooks.services.pending_event_queue.cache") as mock:
        mock.get = lambda key, default=None: data.get(key, default)
        mock.set = lambda key, value, timeout=None: data.__setitem__(key, value)
        mock.add = add
        mock.delete = lambda key: data.pop(key, None)
        mock.data = data
        yield mock


def _invoice(
    invoice_id: str, subscription_id: str, amount: float, type_: str = "payment_success"
) -> dict[str, Any]:
    """Build a stored invoice event item.

    Args:
        invoice_id: Stripe invoice id.
        subscription_id: Stripe subscription id.
        amount: Invoice amount.
        type_: Normalized event type.

    Returns:
        A stored pending item.
    """
    return {
        "event_data": {
            "type": type_,
            "customer_id": "cus_1",
            "external_id": invoice_id,
            "amount": amount,
            "metadata": {"subscription_id": subscription_id},
        },
        "customer_data": {"email": "buyer@example.com"},
    }


KEY = "customer:cus_1:t1"
PENDING = f"pending_webhook:ws_1:{KEY}"


def test_two_renewals_send_two_notifications(
    queue: PendingEventQueue, stateful_cache: MagicMock
) -> None:
    """$49 on in_A and $999 on in_B each get their own notification."""
    stateful_cache.set(
        PENDING,
        [_invoice("in_A", "sub_A", 49.0), _invoice("in_B", "sub_B", 999.0)],
    )

    with patch.object(queue, "_send_notification", return_value=True) as send:
        queue._process_events(KEY, "ws_1", "stripe", None)

    amounts = sorted(call.args[0]["amount"] for call in send.call_args_list)
    assert amounts == [49.0, 999.0]
    assert PENDING not in stateful_cache.data


def test_one_transaction_still_merges(
    queue: PendingEventQueue, stateful_cache: MagicMock
) -> None:
    """Subscription + both invoice events of one payment stay one message."""
    subscription = {
        "event_data": {
            "type": "subscription_created",
            "customer_id": "cus_1",
            "external_id": "sub_X",
            "amount": 49.0,
            "metadata": {"subscription_id": "sub_X"},
        },
        "customer_data": {"email": ""},
    }
    stateful_cache.set(
        PENDING,
        [
            subscription,
            _invoice("in_X", "sub_X", 49.0, "invoice_paid"),
            _invoice("in_X", "sub_X", 49.0, "payment_success"),
        ],
    )

    with patch.object(queue, "_send_notification", return_value=True) as send:
        queue._process_events(KEY, "ws_1", "stripe", None)

    send.assert_called_once()
    event, customer = send.call_args.args[0], send.call_args.args[1]
    assert event["type"] == "subscription_created"
    assert customer["email"] == "buyer@example.com"


def test_partial_failure_keeps_only_undelivered_transaction(
    queue: PendingEventQueue, stateful_cache: MagicMock
) -> None:
    """A delivered invoice is not resent when another one failed."""
    stateful_cache.set(
        PENDING,
        [_invoice("in_A", "sub_A", 49.0), _invoice("in_B", "sub_B", 999.0)],
    )

    def send(event: dict[str, Any], *args: Any) -> bool:
        return event["external_id"] == "in_A"

    with patch.object(queue, "_send_notification", side_effect=send):
        queue._process_events(KEY, "ws_1", "stripe", None)

    remaining = decrypt_cache_value(stateful_cache.data[PENDING])
    assert [item["event_data"]["external_id"] for item in remaining] == ["in_B"]

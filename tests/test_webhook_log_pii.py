"""Customer PII must not reach plaintext logs.

The pending-event queue logged customer emails at INFO and WARNING, and
LOG_WEBHOOKS put the whole raw body into the log record. Raw bodies are
kept only in the encrypted Redis copy.
"""

import json
import logging
from typing import Any

import pytest
from django.test import RequestFactory
from webhooks.services.pending_event_queue import PendingEventQueue
from webhooks.webhook_router import _log_webhook_payload

EMAIL = "pii-canary@example.com"


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Join log text and every record's attributes, including ``extra``.

    Args:
        caplog: pytest log capture fixture.

    Returns:
        All captured log content.
    """
    return "\n".join([caplog.text, *(str(vars(r)) for r in caplog.records)])


def test_aggregation_logs_do_not_contain_emails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Aggregating events logs whether an email exists, not the email."""
    items: list[dict[str, Any]] = [
        {
            "event_data": {"type": "trial_started", "customer_id": "cus_1"},
            "customer_data": {"email": ""},
        },
        {
            "event_data": {"type": "invoice_paid", "customer_id": "cus_1"},
            "customer_data": {"email": EMAIL},
        },
    ]
    with caplog.at_level(logging.DEBUG, logger="webhooks.services"):
        _, customer = PendingEventQueue()._aggregate_events(items)

    assert customer["email"] == EMAIL
    assert EMAIL not in _log_text(caplog)
    assert "email_present=True" in caplog.text


def test_missing_email_warning_does_not_list_emails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The missing-email diagnostic lists presence flags only."""
    items: list[dict[str, Any]] = [
        {
            "event_data": {"type": "trial_started"},
            "customer_data": {"email": EMAIL},
        }
    ]
    with caplog.at_level(logging.WARNING, logger="webhooks.services"):
        PendingEventQueue()._warn_if_missing_email(
            {"type": "trial_started"}, {"email": ""}, items
        )

    assert "No customer email found" in caplog.text
    assert EMAIL not in _log_text(caplog)


def test_log_webhooks_does_not_put_body_in_log_record(
    caplog: pytest.LogCaptureFixture, settings: Any
) -> None:
    """With LOG_WEBHOOKS on, the body stays out of the log record."""
    settings.LOG_WEBHOOKS = True
    request = RequestFactory().post(
        "/webhook/",
        data=json.dumps({"customer": {"email": EMAIL}}),
        content_type="application/json",
    )
    with caplog.at_level(logging.INFO, logger="webhooks.webhook_router"):
        _log_webhook_payload(request, "shopify", "ws-1")

    assert "WEBHOOK_LOG" in caplog.text
    assert EMAIL not in _log_text(caplog)

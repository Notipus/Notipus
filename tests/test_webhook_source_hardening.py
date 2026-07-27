"""Security hardening tests for webhook source plugins.

These tests lock in fail-closed behavior for the Stripe, Shopify, and
Chargify source plugins:

* An empty webhook secret must never validate a webhook (Stripe/Shopify),
  even when the request otherwise looks valid and even under DEBUG.
* A Chargify webhook missing its timestamp header must be rejected so a
  captured signed body cannot be replayed indefinitely.
* Chargify webhook parsing must not log customer PII (email, name, card
  last-4) from the raw form body.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory
from plugins.sources.base import InvalidDataError
from plugins.sources.chargify import ChargifySourcePlugin
from plugins.sources.shopify import ShopifySourcePlugin
from plugins.sources.stripe import StripeSourcePlugin


@pytest.fixture
def request_factory() -> RequestFactory:
    """Return a Django request factory for building test requests."""
    return RequestFactory()


class TestStripeEmptySecretRejected:
    """Stripe must fail closed when no webhook secret is configured."""

    def test_validate_webhook_rejects_empty_secret(self) -> None:
        """validate_webhook returns False and never calls construct_event.

        An empty secret means Stripe's signature check cannot be trusted,
        so the guard must reject before any construct_event work.
        """
        provider = StripeSourcePlugin(webhook_secret="")
        request = MagicMock()
        request.headers = {"Stripe-Signature": "t=1,v1=deadbeef"}
        request.body = b'{"id": "evt_1", "type": "invoice.paid"}'

        with patch("stripe.Webhook.construct_event") as construct_event:
            assert provider.validate_webhook(request) is False
            construct_event.assert_not_called()

    def test_validate_webhook_rejects_empty_secret_under_debug(
        self, settings: Any
    ) -> None:
        """The empty-secret guard holds even when DEBUG is True."""
        settings.DEBUG = True
        provider = StripeSourcePlugin(webhook_secret="")
        request = MagicMock()
        request.headers = {"Stripe-Signature": "t=1,v1=deadbeef"}
        request.body = b'{"id": "evt_1", "type": "invoice.paid"}'

        with patch("stripe.Webhook.construct_event") as construct_event:
            assert provider.validate_webhook(request) is False
            construct_event.assert_not_called()

    def test_parse_webhook_rejects_empty_secret(
        self, request_factory: RequestFactory
    ) -> None:
        """parse_webhook raises before any construct_event work."""
        provider = StripeSourcePlugin(webhook_secret="")
        request = request_factory.post(
            "/webhook/stripe/",
            data=b'{"id": "evt_1", "type": "invoice.paid"}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=deadbeef",
        )

        with patch("stripe.Webhook.construct_event") as construct_event:
            with pytest.raises(InvalidDataError):
                provider.parse_webhook(request)
            construct_event.assert_not_called()


class TestShopifyEmptySecretRejected:
    """Shopify must fail closed when no webhook secret is configured."""

    def test_validate_webhook_rejects_empty_secret(self) -> None:
        """validate_webhook returns False even with a plausible HMAC header.

        With an empty secret the HMAC cannot be verified, so an attacker
        could otherwise forge a matching signature over any payload.
        """
        provider = ShopifySourcePlugin(webhook_secret="")
        request = MagicMock()
        request.headers = {"X-Shopify-Hmac-SHA256": "anything"}
        request.body = b'{"id": 123}'

        # Even if the underlying compare succeeded, the empty-secret guard
        # must short-circuit to False first.
        with patch("hmac.compare_digest", return_value=True):
            assert provider.validate_webhook(request) is False

    def test_validate_webhook_rejects_empty_secret_under_debug(
        self, settings: Any
    ) -> None:
        """The empty-secret guard holds even when DEBUG is True."""
        settings.DEBUG = True
        provider = ShopifySourcePlugin(webhook_secret="")
        request = MagicMock()
        request.headers = {"X-Shopify-Hmac-SHA256": "anything"}
        request.body = b'{"id": 123}'

        with patch("hmac.compare_digest", return_value=True):
            assert provider.validate_webhook(request) is False


class TestChargifyMissingTimestampRejected:
    """Chargify must reject webhooks without a timestamp header."""

    def test_validate_timestamp_missing_header_rejected(self) -> None:
        """_validate_webhook_timestamp fails closed on a missing header."""
        provider = ChargifySourcePlugin(webhook_secret="test_secret")
        request = MagicMock()
        request.headers = {}

        assert provider._validate_webhook_timestamp(request) is False

    def test_validate_webhook_missing_timestamp_rejected(
        self, request_factory: RequestFactory
    ) -> None:
        """validate_webhook rejects an otherwise-valid webhook with no timestamp.

        The signature only proves the body is authentic, not fresh: without
        a timestamp header a captured signed body could be replayed, so the
        webhook must be rejected even when the signature would match.
        """
        provider = ChargifySourcePlugin(webhook_secret="test_secret")
        request = request_factory.post(
            "/webhook/chargify/",
            data=b"event=payment_success&payload[subscription][id]=12345",
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_123",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256="deadbeef",
        )

        # compare_digest would pass, but the missing timestamp must reject.
        with patch("hmac.compare_digest", return_value=True):
            assert provider.validate_webhook(request) is False

    def test_validate_webhook_with_timestamp_accepted(
        self, request_factory: RequestFactory
    ) -> None:
        """A current timestamp header allows an otherwise-valid webhook."""
        provider = ChargifySourcePlugin(webhook_secret="test_secret")
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        request = request_factory.post(
            "/webhook/chargify/",
            data=b"event=payment_success&payload[subscription][id]=12345",
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_123",
            HTTP_X_CHARGIFY_WEBHOOK_SIGNATURE_HMAC_SHA_256="deadbeef",
            HTTP_X_CHARGIFY_WEBHOOK_TIMESTAMP=timestamp,
        )

        with patch("hmac.compare_digest", return_value=True):
            assert provider.validate_webhook(request) is True


class TestChargifyParseWebhookDoesNotLogPII:
    """Chargify webhook parsing must not log raw customer PII."""

    def test_parse_webhook_omits_pii_from_logs(
        self, request_factory: RequestFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The parse-time log must not contain email, name, or card last-4.

        Uses an acknowledged-but-unprocessed event so parsing returns None
        without needing a full payload; the PII-bearing log line runs first
        regardless.
        """
        provider = ChargifySourcePlugin(webhook_secret="test_secret")
        email = "secret-customer@example.com"
        first_name = "Topsecret"
        card_last4 = "4242"
        body = (
            "event=customer_updated"
            f"&payload[subscription][customer][email]={email}"
            f"&payload[subscription][customer][first_name]={first_name}"
            f"&payload[transaction][card_last_four]={card_last4}"
        )
        request = request_factory.post(
            "/webhook/chargify/",
            data=body,
            content_type="application/x-www-form-urlencoded",
            HTTP_X_CHARGIFY_WEBHOOK_ID="webhook_123",
        )

        with caplog.at_level(logging.DEBUG, logger="plugins.sources.chargify"):
            result = provider.parse_webhook(request)

        assert result is None

        # Inspect the specific parse-time record. PII previously leaked via
        # the ``form_data`` extra (a LogRecord attribute, not the rendered
        # message), so assert against the record attributes directly.
        parse_records = [
            record
            for record in caplog.records
            if record.getMessage() == "Parsing Chargify webhook data"
        ]
        assert len(parse_records) == 1
        record = parse_records[0]

        # The raw form body must no longer be attached to the log record.
        assert not hasattr(record, "form_data")

        # No PII value may appear anywhere in the record's attributes.
        record_dump = repr(record.__dict__)
        assert email not in record_dump
        assert first_name not in record_dump
        assert card_last4 not in record_dump

        # Non-sensitive metadata is still logged for observability.
        assert record.event_type == "customer_updated"
        assert record.webhook_id == "webhook_123"

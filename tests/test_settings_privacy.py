"""Tests for privacy-related Django settings.

Covers the Sentry ``before_send`` scrubbing hook and the guarantee that
default PII capture is disabled, so payment-payload PII and provider
signature headers never leave the app via error reporting.
"""

from typing import Any

from django.conf import settings
from django_notipus.settings import _sentry_before_send


def test_settings_source_disables_default_pii() -> None:
    """The settings module must configure Sentry with send_default_pii=False."""
    from pathlib import Path

    from django_notipus import settings as settings_module

    source = Path(settings_module.__file__ or "").read_text()
    assert "send_default_pii=False" in source
    assert "send_default_pii=True" not in source


def test_before_send_drops_webhook_request_body_and_headers() -> None:
    """Webhook events must have their body dropped and headers stripped."""
    event: dict[str, Any] = {
        "request": {
            "url": "https://notipus.com/webhook/customer/abc-123/stripe/",
            "data": {"amount": 4200, "customer_email": "buyer@example.com"},
            "headers": {
                "Stripe-Signature": "t=1,v1=deadbeef",
                "Content-Type": "application/json",
            },
            "cookies": {"sessionid": "secret-session"},
        }
    }

    result = _sentry_before_send(event, {})

    assert result is not None
    request = result["request"]
    # Raw payment payload dropped entirely.
    assert "data" not in request
    # Signature-bearing headers stripped wholesale for webhook routes.
    assert request["headers"] == {}
    # Cookies dropped for webhook routes.
    assert "cookies" not in request


def test_before_send_redacts_sensitive_headers_on_non_webhook_routes() -> None:
    """Non-webhook events keep body but redact sensitive header/query keys."""
    event: dict[str, Any] = {
        "request": {
            "url": "https://notipus.com/dashboard/",
            "data": {"foo": "bar"},
            "headers": {
                "Authorization": "Bearer super-secret",
                "Accept": "text/html",
            },
            "query_string": {"api_key": "leaky", "page": "2"},
            "cookies": {"sessionid": "abc", "theme": "dark"},
        }
    }

    result = _sentry_before_send(event, {})

    assert result is not None
    request = result["request"]
    # Non-webhook body is preserved.
    assert request["data"] == {"foo": "bar"}
    # Sensitive header redacted, benign header preserved.
    assert request["headers"]["Authorization"] == "[Filtered]"
    assert request["headers"]["Accept"] == "text/html"
    # Sensitive query key redacted, benign one preserved.
    assert request["query_string"]["api_key"] == "[Filtered]"
    assert request["query_string"]["page"] == "2"
    # Session cookie redacted, benign cookie preserved.
    assert request["cookies"]["sessionid"] == "[Filtered]"
    assert request["cookies"]["theme"] == "dark"


def test_before_send_uses_path_only_for_webhook_detection() -> None:
    """A "/webhook/" fragment in the query string must not trigger webhook mode."""
    event: dict[str, Any] = {
        "request": {
            # Non-webhook path; "/webhook/" appears only in the query string.
            "url": "https://notipus.com/dashboard/?next=/webhook/customer/x/",
            "data": {"keep": "me"},
            "headers": {"Accept": "text/html"},
        }
    }

    result = _sentry_before_send(event, {})

    assert result is not None
    request = result["request"]
    # Treated as non-webhook: body and benign headers are preserved, not dropped.
    assert request["data"] == {"keep": "me"}
    assert request["headers"] == {"Accept": "text/html"}


def test_before_send_redacts_sensitive_keys_in_non_webhook_body() -> None:
    """Non-webhook request bodies must have sensitive keys redacted recursively."""
    event: dict[str, Any] = {
        "request": {
            "url": "https://notipus.com/dashboard/",
            "data": {
                "username": "alice",
                "password": "hunter2",
                "nested": {"api_token": "leaky", "keep": "ok"},
                "items": [{"secret": "shh", "label": "one"}],
            },
        }
    }

    result = _sentry_before_send(event, {})

    assert result is not None
    data = result["request"]["data"]
    # Top-level sensitive key redacted, benign preserved.
    assert data["username"] == "alice"
    assert data["password"] == "[Filtered]"
    # Nested dict walked recursively.
    assert data["nested"]["api_token"] == "[Filtered]"
    assert data["nested"]["keep"] == "ok"
    # Lists of dicts walked recursively.
    assert data["items"][0]["secret"] == "[Filtered]"
    assert data["items"][0]["label"] == "one"


def test_before_send_redacts_string_query_string() -> None:
    """A raw-string query_string must have sensitive keys redacted in place."""
    event: dict[str, Any] = {
        "request": {
            "url": "https://notipus.com/dashboard/",
            "query_string": "page=2&token=super-secret&api_key=leaky",
        }
    }

    result = _sentry_before_send(event, {})

    assert result is not None
    query = result["request"]["query_string"]
    assert isinstance(query, str)
    # Benign key preserved; sensitive keys redacted.
    assert "page=2" in query
    assert "super-secret" not in query
    assert "leaky" not in query
    assert query.count("%5BFiltered%5D") == 2  # url-encoded "[Filtered]" twice


def test_before_send_passes_events_without_request() -> None:
    """Events lacking a request section are returned unchanged."""
    event: dict[str, Any] = {"message": "boom"}

    result = _sentry_before_send(event, {})

    assert result == {"message": "boom"}


def test_email_backend_configured() -> None:
    """The EMAIL_BACKEND setting is resolvable (console default under tests)."""
    assert settings.EMAIL_BACKEND

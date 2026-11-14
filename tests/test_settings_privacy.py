"""Tests for privacy-related Django settings.

Covers the Sentry ``before_send`` scrubbing hook and the guarantee that
default PII capture is disabled, so payment-payload PII and provider
signature headers never leave the app via error reporting.
"""

from typing import Any, Callable

from django.conf import settings
from django_notipus.settings import _sentry_before_send


def test_settings_disables_default_pii() -> None:
    """Sentry must be initialized with send_default_pii=False and the scrubber.

    Spies on the real ``sentry_sdk.init`` call the settings module makes at
    import time (by reloading it under a patch) and asserts on the runtime
    keyword arguments, so comment/formatting changes cannot break it.
    """
    import importlib
    from unittest.mock import patch

    from django_notipus import settings as settings_module

    with patch("sentry_sdk.init") as mock_init:
        importlib.reload(settings_module)

    assert mock_init.called
    _, kwargs = mock_init.call_args
    assert kwargs["send_default_pii"] is False
    assert kwargs["before_send"] is settings_module._sentry_before_send


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


def _capture_with_settings_init(
    exercise: Callable[[], None],
) -> list[dict[str, Any]]:
    """Run ``exercise`` under the real settings Sentry config and fake transport.

    The keyword arguments the settings module passes to ``sentry_sdk.init``
    are captured by reloading it under a patch, then Sentry is initialised for
    real with those same arguments (only the DSN and transport are swapped for
    a dummy DSN and an in-memory transport), so the test exercises exactly the
    production client options.

    :param exercise: Callable that triggers Sentry events.
    :returns: The JSON event payloads the transport would have sent.
    """
    import importlib
    from unittest.mock import patch

    import sentry_sdk
    from django_notipus import settings as settings_module
    from sentry_sdk.envelope import Envelope
    from sentry_sdk.transport import Transport

    captured: list[dict[str, Any]] = []

    class _MemoryTransport(Transport):
        """Transport that records event payloads instead of sending them."""

        def capture_envelope(self, envelope: Envelope) -> None:
            """Record every event item in ``envelope``."""
            for item in envelope.items:
                if item.type == "event" and item.payload.json is not None:
                    captured.append(item.payload.json)

    with patch("sentry_sdk.init") as mock_init:
        importlib.reload(settings_module)
    _, kwargs = mock_init.call_args
    kwargs = {
        **kwargs,
        "dsn": "https://key@o0.ingest.sentry.io/1",
        "transport": _MemoryTransport,
    }
    sentry_sdk.init(**kwargs)
    try:
        exercise()
        sentry_sdk.flush()
    finally:
        sentry_sdk.init(dsn="")
    return captured


def test_sentry_events_do_not_carry_frame_locals_or_tokens() -> None:
    """Credentials held in frame locals and breadcrumbs never reach Sentry.

    Reproduces a destination failing while its frame holds a bot token, a
    token-bearing URL, a webhook URL, a payload and the end customer's email.
    """
    import json

    import sentry_sdk

    # Secrets are assembled at runtime: Sentry attaches source context lines,
    # so literal secrets in this file would show up in the event regardless.
    bot_secret = "-".join(("SECRET", "BOT", "TOKEN"))
    crumb_secret = "-".join(("BREADCRUMB", "TOKEN"))
    hook_secret = "-".join(("SECRET", "HOOK"))
    customer_email = "@".join(("buyer", "example.com"))

    def _deliver(credentials: dict[str, Any]) -> None:
        """Fail like a destination send whose locals hold secrets."""
        bot_token = credentials["bot_token"]
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        webhook_url = credentials["webhook_url"]
        payload = {"text": "hi", "email": customer_email}
        destinations = [{"credentials": credentials}]
        assert url and webhook_url and payload and destinations
        raise RuntimeError("delivery failed")

    def _exercise() -> None:
        """Add a token-bearing breadcrumb and capture a failing delivery."""
        sentry_sdk.add_breadcrumb(
            category="httplib",
            data={"url": f"https://api.telegram.org/bot123456:{crumb_secret}/x"},
        )
        try:
            _deliver(
                {
                    "bot_token": f"123456:{bot_secret}",
                    "webhook_url": f"https://hooks.example.com/{hook_secret}",
                }
            )
        except RuntimeError:
            sentry_sdk.capture_exception()

    events = _capture_with_settings_init(_exercise)

    assert len(events) == 1
    serialized = json.dumps(events)
    for secret in (bot_secret, crumb_secret, hook_secret, customer_email):
        assert secret not in serialized
    # The stack trace itself is still reported, just without locals.
    frames = events[0]["exception"]["values"][0]["stacktrace"]["frames"]
    assert any(frame.get("function") == "_deliver" for frame in frames)
    assert all("vars" not in frame for frame in frames)


def test_settings_enables_recursive_scrubber_with_extended_denylist() -> None:
    """Sentry init disables frame locals and uses an extended scrubber."""
    import importlib
    from unittest.mock import patch

    from django_notipus import settings as settings_module

    with patch("sentry_sdk.init") as mock_init:
        importlib.reload(settings_module)

    _, kwargs = mock_init.call_args
    assert kwargs["include_local_variables"] is False
    scrubber = kwargs["event_scrubber"]
    assert scrubber.recursive is True
    for key in ("url", "bot_token", "webhook_url", "payload", "email", "api_key"):
        assert key in scrubber.denylist


def test_before_send_strips_frame_vars_and_breadcrumb_data() -> None:
    """before_send removes frame vars and breadcrumb data as defence in depth."""
    event: dict[str, Any] = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [{"function": "send", "vars": {"x": "SECRET"}}]
                    }
                }
            ]
        },
        "threads": {
            "values": [
                {"stacktrace": {"frames": [{"function": "t", "vars": {"y": "S"}}]}}
            ]
        },
        "breadcrumbs": {
            "values": [{"category": "httplib", "data": {"url": "https://x/SECRET"}}]
        },
    }

    result = _sentry_before_send(event, {})

    assert result is not None
    frame = result["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame == {"function": "send"}
    assert "vars" not in result["threads"]["values"][0]["stacktrace"]["frames"][0]
    assert result["breadcrumbs"]["values"][0] == {"category": "httplib"}

"""Redis pool options: retry establishing a connection, never commands."""

from typing import Any
from unittest.mock import patch

import pytest
from django.core.cache.backends.redis import RedisCacheClient
from django_notipus import settings as notipus_settings
from django_notipus.redis_connection import (
    ConnectRetryConnection,
    ConnectRetrySSLConnection,
    connection_class_for,
)
from redis import Connection, SSLConnection
from redis.exceptions import TimeoutError as RedisTimeoutError


def _client(url: str = "redis://localhost:6399/0") -> RedisCacheClient:
    """Build Django's Redis cache client with the production options."""
    return RedisCacheClient([url], **notipus_settings.REDIS_CONNECTION_OPTIONS)


def test_cache_is_configured_with_the_connection_options() -> None:
    """Both cache configurations hand the options to redis-py."""
    options = notipus_settings.CACHES["default"]["OPTIONS"]
    assert options is notipus_settings.REDIS_CONNECTION_OPTIONS
    assert options["socket_keepalive"] is True
    assert options["health_check_interval"] > 0


def test_pooled_connections_never_retry_commands() -> None:
    """A retried INCR or SET NX would double count or self-lock."""
    pool = _client().get_client(write=True).connection_pool
    conn = pool.make_connection()
    assert isinstance(conn, ConnectRetryConnection)
    assert conn.retry.get_retries() == 0
    assert conn.socket_keepalive is True


def test_incr_is_not_resent_when_the_reply_times_out() -> None:
    """A command whose reply times out fails once instead of being re-sent."""
    client = _client().get_client(write=True)
    sent: list[tuple[Any, ...]] = []

    def fake_send(conn: Any, command_name: str, *args: Any, **kwargs: Any) -> Any:
        sent.append((command_name, *args))
        raise RedisTimeoutError("Timeout reading from socket")

    with (
        patch.object(ConnectRetryConnection, "connect"),
        patch.object(type(client), "_send_command_parse_response", fake_send),
        pytest.raises(RedisTimeoutError),
    ):
        client.incr("counter")

    assert len(sent) == 1


def test_connect_retries_a_timed_out_handshake() -> None:
    """One handshake timeout is retried instead of failing the command."""
    conn = _client().get_client(write=True).connection_pool.make_connection()
    calls = {"n": 0}

    def flaky_connect(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedisTimeoutError("Timeout reading from socket")

    with (
        patch.object(Connection, "connect", flaky_connect),
        patch.object(conn, "disconnect"),
        patch("redis.retry.sleep"),
    ):
        conn.connect()

    assert calls["n"] == 2


def test_connect_gives_up_after_the_configured_retries() -> None:
    """The connect retry is bounded."""
    cls = connection_class_for("redis://localhost:6399/0", 2)
    conn = cls(host="localhost", port=6399)
    calls = {"n": 0}

    def failing_connect(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        raise RedisTimeoutError("Timeout reading from socket")

    with (
        patch.object(Connection, "connect", failing_connect),
        patch.object(conn, "disconnect"),
        patch("redis.retry.sleep"),
        pytest.raises(RedisTimeoutError),
    ):
        conn.connect()

    assert calls["n"] == 3


def test_negative_retries_do_not_mean_retry_forever() -> None:
    """redis-py treats a negative count as infinite; clamp it to zero."""
    assert connection_class_for("redis://h/0", -1).connect_retries == 0


def test_tls_urls_keep_a_tls_connection() -> None:
    """Overriding connection_class must not downgrade rediss:// to plaintext."""
    cls = connection_class_for("rediss://h:6379/0", 2)
    assert issubclass(cls, ConnectRetrySSLConnection)
    assert issubclass(cls, SSLConnection)
    url = "rediss://h:6379/0"
    client = RedisCacheClient([url], connection_class=connection_class_for(url, 2))
    pool = client.get_client(write=True).connection_pool
    assert isinstance(pool.make_connection(), SSLConnection)


def test_failed_health_check_reconnects_instead_of_failing() -> None:
    """A stale idle socket is replaced on the PING health check."""
    conn = _client().get_client(write=True).connection_pool.make_connection()
    conn.next_health_check = 0
    calls = {"n": 0}

    def flaky_ping() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedisTimeoutError("Timeout reading from socket")

    with (
        patch.object(conn, "_send_ping", flaky_ping),
        patch.object(conn, "disconnect") as mock_disconnect,
        patch("redis.retry.sleep"),
    ):
        conn.check_health()

    assert calls["n"] == 2
    mock_disconnect.assert_called_once()

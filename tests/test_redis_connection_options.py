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
from redis import Connection, SSLConnection, UnixDomainSocketConnection
from redis.exceptions import AuthenticationError
from redis.exceptions import TimeoutError as RedisTimeoutError


def _client(url: str = "redis://localhost:6399/0") -> RedisCacheClient:
    """Build Django's Redis cache client with the production options."""
    return RedisCacheClient([url], **notipus_settings.REDIS_CONNECTION_OPTIONS)


def test_cache_is_configured_with_the_connection_options() -> None:
    """Both cache configurations hand the options to redis-py."""
    options = notipus_settings.CACHES["default"]["OPTIONS"]
    assert options is notipus_settings.REDIS_CONNECTION_OPTIONS
    assert options["socket_keepalive"] is True
    assert options["socket_timeout"] > 0


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


def test_auth_failures_are_not_retried() -> None:
    """A wrong password fails the same way every time; do not retry it."""
    cls = connection_class_for("redis://localhost:6399/0", 2)
    conn = cls(host="localhost", port=6399)
    calls = {"n": 0}

    def bad_auth(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        raise AuthenticationError("invalid password")

    with (
        patch.object(Connection, "connect", bad_auth),
        patch.object(conn, "disconnect"),
        patch("redis.retry.sleep"),
        pytest.raises(AuthenticationError),
    ):
        conn.connect()

    assert calls["n"] == 1


def test_unix_socket_urls_keep_redis_py_connection_class() -> None:
    """unix:// must not be forced onto a TCP class (it rejects ``path``)."""
    assert connection_class_for("unix:///var/run/redis.sock", 2) is None
    client = RedisCacheClient(["unix:///var/run/redis.sock"])
    conn = client.get_client(write=True).connection_pool.make_connection()
    assert isinstance(conn, UnixDomainSocketConnection)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "inf", "nan", "1e400"])
def test_invalid_socket_timeouts_fall_back_to_default(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero, negative, non-finite or junk timeouts use the default."""
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", raw)
    assert notipus_settings._env_positive_float("REDIS_SOCKET_TIMEOUT", 5) == 5


@pytest.mark.parametrize("raw", ["", "abc", "-1", "inf", "3.0"])
def test_invalid_connect_retries_fall_back_to_default(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Junk or negative retry counts never mean "retry forever"."""
    monkeypatch.setenv("REDIS_CONNECT_RETRIES", raw)
    assert notipus_settings._env_non_negative_int("REDIS_CONNECT_RETRIES", 1) == 1

"""Redis pool options must retry a slow connect/handshake (NOTIPUS-3J/3K/3N)."""

from unittest.mock import patch

from redis import ConnectionPool
from redis.exceptions import TimeoutError as RedisTimeoutError


def test_redis_options_are_wired_into_the_cache() -> None:
    """The cache backend is handed the connection options."""
    from django_notipus import settings as notipus_settings

    options = notipus_settings.CACHES["default"]["OPTIONS"]
    assert options is notipus_settings.REDIS_CONNECTION_OPTIONS
    assert options["socket_keepalive"] is True
    assert options["health_check_interval"] > 0
    assert options["retry"].get_retries() >= 1


def test_pooled_connection_retries_a_timed_out_handshake() -> None:
    """One handshake timeout is retried instead of failing the command."""
    from django_notipus import settings as notipus_settings

    pool = ConnectionPool.from_url(
        "redis://localhost:6379/0", **notipus_settings.REDIS_CONNECTION_OPTIONS
    )
    conn = pool.make_connection()
    calls = {"n": 0}

    def flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedisTimeoutError("Timeout reading from socket")

    with (
        patch.object(conn, "connect_check_health", side_effect=flaky_connect),
        patch.object(conn, "disconnect"),
        patch("redis.retry.sleep"),
    ):
        conn.connect()

    assert calls["n"] == 2

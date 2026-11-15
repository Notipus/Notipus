"""Redis connections that retry establishing the connection, never commands.

redis-py uses a connection's ``retry`` for both the connect/handshake AND
every command sent on it (``conn.retry.call_with_retry`` in
``Redis._execute_command``). Django's RedisCache hands redis-py its own
pool, so that per-connection policy is the only one in play. Retrying
commands is unsafe for us: a timed-out INCR that Redis already applied
would be counted twice, and a retried ``SET NX`` lock would find its own
key and report the lock as held by someone else.

The connection classes here keep the default "never retry commands"
policy and add a separate, bounded retry around ``connect()`` only. The
connect step (TCP connect plus HELLO/AUTH handshake) changes no data, so
retrying it is always safe.
"""

from typing import Any, ClassVar

from redis.backoff import ExponentialWithJitterBackoff
from redis.connection import Connection, SSLConnection
from redis.exceptions import AuthenticationError, AuthorizationError
from redis.retry import Retry


def _is_transient(error: Exception) -> bool:
    """Whether a connect failure is worth retrying.

    Bad credentials and missing ACL permissions subclass ConnectionError
    but will fail the same way every time; retrying them only adds AUTH
    load and latency.
    """
    return not isinstance(error, (AuthenticationError, AuthorizationError))


class _ConnectRetryMixin:
    """Retry ``connect()`` (TCP connect + HELLO/AUTH handshake) on failure."""

    connect_retries: ClassVar[int] = 1

    def connect(self) -> None:
        """Connect, retrying transient connection and timeout errors."""
        # A fresh Retry per connect: cheap next to a network handshake,
        # and it shares no state between the threads using one pool.
        retry = Retry(
            ExponentialWithJitterBackoff(base=0.1, cap=1.0), self.connect_retries
        )
        parent: Any = super()
        retry.call_with_retry(
            parent.connect,
            self.disconnect,  # type: ignore[attr-defined]
            is_retryable=_is_transient,
        )


class ConnectRetryConnection(_ConnectRetryMixin, Connection):
    """Plain TCP connection whose connect step is retried."""


class ConnectRetrySSLConnection(_ConnectRetryMixin, SSLConnection):
    """TLS connection whose connect step is retried."""


def connection_class_for(url: str, connect_retries: int) -> type[Connection] | None:
    """Pick the connection class matching the URL scheme.

    Passing ``connection_class`` to ``ConnectionPool.from_url`` overrides
    the class redis-py derives from the scheme, so a ``rediss://`` URL
    must get the TLS variant or it would silently connect in plaintext.
    Other schemes (``unix://``) keep redis-py's own class.

    Args:
        url: Redis URL the cache connects to.
        connect_retries: Extra connect attempts after the first failure.

    Returns:
        A connection class with ``connect_retries`` applied, or None when
        redis-py's default class for the scheme should be used.
    """
    if url.startswith("rediss://"):
        base: type[Connection] = ConnectRetrySSLConnection
    elif url.startswith("redis://"):
        base = ConnectRetryConnection
    else:
        return None
    retries = max(0, connect_retries)
    return type(
        f"{base.__name__}Retries{retries}", (base,), {"connect_retries": retries}
    )

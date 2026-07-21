"""URL safety utilities for guarding against SSRF.

This module provides helpers to validate that an outbound URL points at a
public internet host before it is fetched, and to actually fetch it in a way
that is resistant to DNS-rebinding (TOCTOU) attacks.

The core guarantees are:

* Only ``http``/``https`` schemes with a host are allowed.
* The hostname is resolved and every resolved IP must be routable on the
  public internet (rejecting private, loopback, link-local, reserved,
  multicast, unspecified, and IPv4-mapped-IPv6 variants of those).
* When fetching, the connection is *pinned* to a pre-validated IP so that
  ``requests`` cannot silently re-resolve the hostname to an internal
  address between validation and connection. The original hostname is kept
  for TLS SNI and certificate verification, so certificate checking is not
  weakened.
"""

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# URL schemes permitted for outbound fetches.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Default ports per scheme, used when reconstructing the Host header.
DEFAULT_PORTS = {"http": 80, "https": 443}


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe to fetch from a server context."""


def _is_public_ip(ip_str: str) -> bool:
    """Return whether an IP address string is routable on the public internet.

    IPv4-mapped IPv6 addresses (e.g. ``::ffff:127.0.0.1``) are normalized to
    their embedded IPv4 address before evaluation, so they cannot be used to
    smuggle an internal address past the check. The authoritative test is
    ``ipaddress``'s global-reachability flag (``is_global``), which covers
    unique-local (ULA), shared-address/CGNAT and similar ranges; explicit
    category checks are kept as belt-and-suspenders.

    Args:
        ip_str: Textual IPv4 or IPv6 address.

    Returns:
        True if the address is public, False for non-public or unparseable
        addresses.
    """
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    # Normalize IPv4-mapped IPv6 to the embedded IPv4 address so the real
    # target is evaluated instead of the wrapper.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    # is_global is the primary, authoritative reject condition.
    if not ip.is_global:
        return False

    # Belt-and-suspenders: reject well-known non-public categories explicitly.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False

    return True


def _resolve_validated_ips(hostname: str) -> list[str]:
    """Resolve a hostname once and return its validated public IPs.

    The resolution is fail-closed: if resolution fails, or if ANY resolved
    address is non-public, an empty list is returned so callers reject the
    host entirely rather than gambling on address ordering.

    Args:
        hostname: The hostname to resolve.

    Returns:
        A list of validated public IP strings, or an empty list if the host
        is unsafe or unresolvable.
    """
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, OSError, UnicodeError):
        return []

    if not addr_infos:
        return []

    ips: list[str] = []
    for addr_info in addr_infos:
        ip_str = str(addr_info[4][0])
        if not _is_public_ip(ip_str):
            return []
        ips.append(ip_str)

    return ips


def _prefer_ipv4(ips: list[str]) -> str:
    """Pick the connection IP, preferring IPv4 over IPv6.

    Dual-stack hosts may return an IPv6 address first, which fails in
    IPv4-only or broken-IPv6 environments even when a validated public IPv4
    exists. Prefer the first IPv4 address and fall back to the first address
    (IPv6) only if no IPv4 is present.

    Args:
        ips: Non-empty list of already-validated public IP strings.

    Returns:
        The IP string to pin the connection to.
    """
    for ip in ips:
        if ":" not in ip:
            return ip
    return ips[0]


def is_safe_public_url(url: str) -> bool:
    """Return whether a URL is safe to fetch from a server context.

    The URL must use an http/https scheme and specify a host. The host is
    resolved via DNS and every resolved IP address must be public. This
    guards against SSRF where an attacker-controlled URL points at internal
    infrastructure.

    Note:
        This performs a point-in-time check. To fetch safely, use
        :func:`create_pinned_session`, which additionally pins the connection
        to the validated IP and so is not vulnerable to DNS rebinding.

    Args:
        url: The URL to validate.

    Returns:
        True if the URL is safe to fetch, False otherwise.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False

    if not parsed.hostname:
        return False

    return bool(_resolve_validated_ips(parsed.hostname))


def assert_safe_public_url(url: str) -> None:
    """Raise if a URL is not safe to fetch from a server context.

    Args:
        url: The URL to validate.

    Raises:
        UnsafeUrlError: If the URL fails the :func:`is_safe_public_url` check.
    """
    if not is_safe_public_url(url):
        raise UnsafeUrlError(f"URL is not a safe public URL: {url!r}")


class _PinnedIPAdapter(HTTPAdapter):
    """Requests adapter that connects to a pre-validated public IP.

    The connection target is pinned to an IP that was already validated as
    public, closing the TOCTOU DNS-rebinding window: without this, ``requests``
    would re-resolve the hostname when opening the socket, and an attacker
    controlling DNS could return a public IP during validation and an internal
    IP at fetch time. The original hostname is preserved for the ``Host``
    header and (for TLS) for SNI and certificate verification, so certificate
    checking is not weakened.
    """

    def __init__(self, hostname: str, validated_ip: str, use_tls: bool) -> None:
        """Initialize the adapter.

        Args:
            hostname: Original hostname (used for Host header and TLS verify).
            validated_ip: Pre-validated public IP to connect to.
            use_tls: Whether the target scheme is https (enables SNI pinning).
        """
        self._hostname = hostname
        self._validated_ip = validated_ip
        self._use_tls = use_tls
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Force TLS SNI and hostname verification to use the real hostname.

        Even though the socket connects to the pinned IP, SNI and certificate
        verification must target the original hostname. These pool kwargs are
        only meaningful for TLS, so they are set for https targets only.
        """
        if self._use_tls:
            kwargs["server_hostname"] = self._hostname
            kwargs["assert_hostname"] = self._hostname
        super().init_poolmanager(*args, **kwargs)

    def send(
        self,
        request: requests.PreparedRequest,
        stream: Any = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        """Rewrite the request to connect to the pinned IP, then send.

        Args:
            request: The prepared request to send.
            stream: Whether to stream the response body.
            timeout: Request timeout.
            verify: Whether/how to verify TLS certificates.
            cert: Optional client certificate.
            proxies: Optional proxy mapping.

        Returns:
            The HTTP response.

        Raises:
            UnsafeUrlError: If the request host no longer matches the pinned
                hostname (e.g. an unexpected retarget).
        """
        parsed = urlsplit(request.url or "")
        if parsed.hostname != self._hostname:
            raise UnsafeUrlError(
                f"Request host does not match pinned host: {request.url!r}"
            )

        conn_host = (
            f"[{self._validated_ip}]"
            if ":" in self._validated_ip
            else self._validated_ip
        )
        netloc = conn_host if parsed.port is None else f"{conn_host}:{parsed.port}"
        request.url = urlunsplit(parsed._replace(netloc=netloc))

        # Preserve the original Host header so the origin server routes the
        # request correctly despite the IP-based connection.
        default_port = DEFAULT_PORTS.get(parsed.scheme.lower())
        if parsed.port and parsed.port != default_port:
            request.headers["Host"] = f"{self._hostname}:{parsed.port}"
        else:
            request.headers["Host"] = self._hostname

        return super().send(
            request,
            stream=stream,
            timeout=timeout,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )


def create_pinned_session(url: str) -> requests.Session:
    """Return a requests Session pinned to a validated public IP for ``url``.

    The hostname is resolved exactly once, every resolved IP is validated as
    public, and the returned session is mounted with an adapter that connects
    to a validated IP while preserving the hostname for TLS verification. This
    is the DNS-rebinding-safe way to fetch an untrusted URL.

    Args:
        url: The URL that will be fetched with the returned session.

    Returns:
        A requests Session pinned to a validated public IP.

    Raises:
        UnsafeUrlError: If the scheme/host is invalid or the host resolves to
            any non-public address.
    """
    try:
        parsed = urlsplit(url)
    except ValueError as e:
        raise UnsafeUrlError(f"Malformed URL: {url!r}") from e

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"Unsupported URL scheme: {url!r}")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError(f"URL has no host: {url!r}")

    ips = _resolve_validated_ips(hostname)
    if not ips:
        raise UnsafeUrlError(f"URL resolves to a non-public host: {url!r}")

    adapter = _PinnedIPAdapter(hostname, _prefer_ipv4(ips), use_tls=(scheme == "https"))
    session = requests.Session()
    session.mount(f"{scheme}://", adapter)
    return session

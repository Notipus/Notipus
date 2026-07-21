"""URL safety utilities for guarding against SSRF.

This module provides helpers to validate that an outbound URL points at a
public internet host before it is fetched. It resolves the hostname and
rejects any URL that resolves to a private, loopback, link-local, reserved,
multicast, or unspecified address, closing off server-side request forgery
(SSRF) vectors against internal infrastructure (e.g. cloud metadata endpoints).
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# URL schemes permitted for outbound fetches.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def _is_public_ip(ip_str: str) -> bool:
    """Return whether an IP address string is a routable public address.

    Args:
        ip_str: Textual IPv4 or IPv6 address.

    Returns:
        True if the address is public (not private, loopback, link-local,
        reserved, multicast, or unspecified). False for non-public or
        unparseable addresses.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

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


def is_safe_public_url(url: str) -> bool:
    """Return whether a URL is safe to fetch from a server context.

    The URL must use an http/https scheme and specify a host. The host is
    resolved via DNS and every resolved IP address must be public; if any
    resolved address is private, loopback, link-local, reserved, multicast,
    or unspecified the URL is rejected. This guards against SSRF where an
    attacker-controlled URL points at internal infrastructure.

    Args:
        url: The URL to validate.

    Returns:
        True if the URL is safe to fetch, False otherwise.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, socket.error, UnicodeError):
        return False

    if not addr_infos:
        return False

    for addr_info in addr_infos:
        ip_str = str(addr_info[4][0])
        if not _is_public_ip(ip_str):
            return False

    return True


def assert_safe_public_url(url: str) -> None:
    """Raise if a URL is not safe to fetch from a server context.

    Args:
        url: The URL to validate.

    Raises:
        ValueError: If the URL fails the :func:`is_safe_public_url` check.
    """
    if not is_safe_public_url(url):
        raise ValueError(f"URL is not a safe public URL: {url!r}")

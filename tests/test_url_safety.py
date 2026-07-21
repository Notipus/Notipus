"""Tests for the URL safety (SSRF guard) utilities.

Tests cover:
- Rejection of non-http(s) schemes
- Rejection of hostless URLs
- Rejection of hosts resolving to private/loopback/link-local/reserved/
  multicast/unspecified addresses (IPv4 and IPv6)
- Acceptance of hosts resolving to public addresses
- DNS resolution failures being treated as unsafe

DNS resolution is monkeypatched so tests never touch the real network
(the suite runs with pytest-socket's --disable-socket).
"""

import socket
from collections.abc import Callable

import pytest
from core.utils import url_safety
from core.utils.url_safety import assert_safe_public_url, is_safe_public_url


def _fake_getaddrinfo_for(ip: str) -> Callable[..., list]:
    """Build a getaddrinfo replacement resolving every host to ``ip``.

    Args:
        ip: The IP address string every lookup should resolve to.

    Returns:
        A callable with the signature of ``socket.getaddrinfo`` returning a
        single address info tuple pointing at ``ip``.
    """
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def _fake(*_args: object, **_kwargs: object) -> list:
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


class TestIsSafePublicUrl:
    """Tests for :func:`is_safe_public_url`."""

    def test_public_https_url_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A public host over https is accepted."""
        monkeypatch.setattr(
            url_safety.socket, "getaddrinfo", _fake_getaddrinfo_for("93.184.216.34")
        )
        assert is_safe_public_url("https://example.com/logo.png") is True

    def test_public_http_url_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A public host over http is accepted."""
        monkeypatch.setattr(
            url_safety.socket, "getaddrinfo", _fake_getaddrinfo_for("93.184.216.34")
        )
        assert is_safe_public_url("http://example.com/logo.png") is True

    def test_public_ipv6_url_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A public IPv6 host is accepted."""
        monkeypatch.setattr(
            url_safety.socket,
            "getaddrinfo",
            _fake_getaddrinfo_for("2606:2800:220:1:248:1893:25c8:1946"),
        )
        assert is_safe_public_url("https://example.com/logo.png") is True

    def test_non_http_scheme_rejected(self) -> None:
        """A non-http(s) scheme is rejected without any DNS lookup."""
        assert is_safe_public_url("ftp://example.com/logo.png") is False
        assert is_safe_public_url("file:///etc/passwd") is False
        assert is_safe_public_url("gopher://example.com/") is False

    def test_hostless_url_rejected(self) -> None:
        """A URL with no host is rejected."""
        assert is_safe_public_url("http:///no-host") is False
        assert is_safe_public_url("https://") is False
        assert is_safe_public_url("not-a-url") is False

    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",  # private
            "192.168.1.1",  # private
            "172.16.0.1",  # private
            "127.0.0.1",  # loopback
            "169.254.169.254",  # link-local (cloud metadata)
            "0.0.0.0",  # unspecified
            "240.0.0.1",  # reserved
            "224.0.0.1",  # multicast
        ],
    )
    def test_private_ipv4_rejected(
        self, ip: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hosts resolving to non-public IPv4 addresses are rejected."""
        monkeypatch.setattr(url_safety.socket, "getaddrinfo", _fake_getaddrinfo_for(ip))
        assert is_safe_public_url("https://internal.example.com/") is False

    @pytest.mark.parametrize(
        "ip",
        [
            "::1",  # loopback
            "fe80::1",  # link-local
            "fc00::1",  # unique local (private)
            "::",  # unspecified
            "ff02::1",  # multicast
        ],
    )
    def test_private_ipv6_rejected(
        self, ip: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hosts resolving to non-public IPv6 addresses are rejected."""
        monkeypatch.setattr(url_safety.socket, "getaddrinfo", _fake_getaddrinfo_for(ip))
        assert is_safe_public_url("https://internal.example.com/") is False

    def test_mixed_resolution_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If any resolved address is non-public the URL is rejected."""

        def _fake(*_args: object, **_kwargs: object) -> list:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            ]

        monkeypatch.setattr(url_safety.socket, "getaddrinfo", _fake)
        assert is_safe_public_url("https://example.com/") is False

    def test_dns_failure_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A DNS resolution failure is treated as unsafe."""

        def _fake(*_args: object, **_kwargs: object) -> list:
            raise socket.gaierror("name resolution failed")

        monkeypatch.setattr(url_safety.socket, "getaddrinfo", _fake)
        assert is_safe_public_url("https://does-not-resolve.example/") is False

    def test_empty_resolution_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty resolution result is treated as unsafe."""
        monkeypatch.setattr(url_safety.socket, "getaddrinfo", lambda *a, **k: [])
        assert is_safe_public_url("https://example.com/") is False


class TestAssertSafePublicUrl:
    """Tests for :func:`assert_safe_public_url`."""

    def test_raises_on_unsafe_url(self) -> None:
        """An unsafe URL raises ValueError."""
        with pytest.raises(ValueError):
            assert_safe_public_url("ftp://example.com/")

    def test_passes_on_safe_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A safe URL does not raise."""
        monkeypatch.setattr(
            url_safety.socket, "getaddrinfo", _fake_getaddrinfo_for("93.184.216.34")
        )
        assert_safe_public_url("https://example.com/logo.png")

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
import requests
from core.utils import url_safety
from core.utils.url_safety import (
    UnsafeUrlError,
    _PinnedIPAdapter,
    assert_safe_public_url,
    create_pinned_session,
    is_safe_public_url,
)


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
            "fc00::1",  # unique local (ULA / private)
            "fd00::1",  # unique local (ULA / private)
            "::",  # unspecified
            "ff02::1",  # multicast
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",  # IPv4-mapped link-local (metadata)
            "::ffff:10.0.0.1",  # IPv4-mapped private
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


class TestCreatePinnedSession:
    """Tests for :func:`create_pinned_session` (DNS-rebinding-safe fetch)."""

    def test_pins_to_validated_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The session is mounted with an adapter pinned to the validated IP."""
        monkeypatch.setattr(
            url_safety.socket, "getaddrinfo", _fake_getaddrinfo_for("93.184.216.34")
        )
        session = create_pinned_session("https://example.com/logo.png")
        try:
            adapter = session.get_adapter("https://example.com/logo.png")
            assert isinstance(adapter, _PinnedIPAdapter)
            assert adapter._hostname == "example.com"
            assert adapter._validated_ip == "93.184.216.34"
        finally:
            session.close()

    def test_refuses_private_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host resolving to a private IP raises UnsafeUrlError."""
        monkeypatch.setattr(
            url_safety.socket, "getaddrinfo", _fake_getaddrinfo_for("127.0.0.1")
        )
        with pytest.raises(UnsafeUrlError):
            create_pinned_session("https://internal.example.com/")

    def test_refuses_non_http_scheme(self) -> None:
        """A non-http(s) scheme raises without resolving."""
        with pytest.raises(UnsafeUrlError):
            create_pinned_session("ftp://example.com/")

    def test_refuses_hostless_url(self) -> None:
        """A hostless URL raises."""
        with pytest.raises(UnsafeUrlError):
            create_pinned_session("https://")

    def test_refuses_malformed_url(self) -> None:
        """A malformed URL that fails to parse is rejected, not a 500."""
        # Unbalanced IPv6 brackets make urlsplit raise ValueError.
        with pytest.raises(UnsafeUrlError):
            create_pinned_session("http://[::1")

    def test_resolves_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DNS is resolved exactly once so there is no re-resolution window."""
        calls = {"n": 0}

        def _fake(*_args: object, **_kwargs: object) -> list:
            calls["n"] += 1
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(url_safety.socket, "getaddrinfo", _fake)
        session = create_pinned_session("https://example.com/")
        session.close()
        assert calls["n"] == 1

    def test_dns_rebinding_pins_first_public_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host that is public at validation then private at fetch is pinned.

        The resolver returns a public IP on the first (validation) call and a
        private IP on any subsequent call. Because the session resolves once
        and pins the connection to the validated public IP, the later private
        answer is never used, closing the DNS-rebinding window.
        """
        results = iter(
            [
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
            ]
        )

        def _fake(*_args: object, **_kwargs: object) -> list:
            return next(results)

        monkeypatch.setattr(url_safety.socket, "getaddrinfo", _fake)
        session = create_pinned_session("https://example.com/")
        try:
            adapter = session.get_adapter("https://example.com/")
            assert isinstance(adapter, _PinnedIPAdapter)
            # Pinned to the public IP seen at validation, not the later private.
            assert adapter._validated_ip == "93.184.216.34"
        finally:
            session.close()


class TestPinnedIPAdapter:
    """Tests for the connection-pinning behavior of :class:`_PinnedIPAdapter`."""

    def test_send_rewrites_host_to_ip_and_preserves_host_header(self) -> None:
        """send() connects to the pinned IP but keeps the original Host."""
        adapter = _PinnedIPAdapter("example.com", "93.184.216.34", use_tls=True)
        request = requests.Request("GET", "https://example.com/logo.png").prepare()

        captured: dict[str, requests.PreparedRequest] = {}

        def _fake_super_send(
            self: object, req: requests.PreparedRequest, **_kwargs: object
        ) -> str:
            captured["request"] = req
            return "sent"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(url_safety.HTTPAdapter, "send", _fake_super_send)
            adapter.send(request)

        sent = captured["request"]
        assert sent.url == "https://93.184.216.34/logo.png"
        assert sent.headers["Host"] == "example.com"

    def test_send_brackets_ipv6_and_keeps_port(self) -> None:
        """IPv6 pins are bracketed and non-default ports flow into Host."""
        adapter = _PinnedIPAdapter(
            "example.com", "2606:2800:220:1:248:1893:25c8:1946", use_tls=True
        )
        request = requests.Request("GET", "https://example.com:8443/logo.png").prepare()

        captured: dict[str, requests.PreparedRequest] = {}

        def _fake_super_send(
            self: object, req: requests.PreparedRequest, **_kwargs: object
        ) -> str:
            captured["request"] = req
            return "sent"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(url_safety.HTTPAdapter, "send", _fake_super_send)
            adapter.send(request)

        sent = captured["request"]
        assert sent.url == "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/logo.png"
        assert sent.headers["Host"] == "example.com:8443"

    def test_send_refuses_retargeted_host(self) -> None:
        """send() refuses a request whose host differs from the pinned one."""
        adapter = _PinnedIPAdapter("example.com", "93.184.216.34", use_tls=True)
        request = requests.Request("GET", "https://evil.example/logo.png").prepare()

        with pytest.raises(UnsafeUrlError):
            adapter.send(request)

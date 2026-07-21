"""Tests for the logo storage service SSRF protections.

Tests cover:
- ``_download_logo`` refusing URLs that resolve to internal hosts
- ``_download_logo`` disabling redirects and refusing any 3xx response
- ``_download_logo`` succeeding for a safe public URL

All network access is mocked; the pinned-session factory is patched so tests
never perform real DNS resolution or open sockets (the suite runs with
pytest-socket's --disable-socket).
"""

from unittest.mock import MagicMock, Mock, patch

from core.services.logo_storage import LogoStorageService
from core.utils.url_safety import UnsafeUrlError


def _mock_session(response: Mock) -> MagicMock:
    """Build a mock requests Session usable as a context manager.

    Args:
        response: The response its ``get`` should return.

    Returns:
        A MagicMock whose ``get`` returns ``response`` and which supports the
        ``with session:`` protocol.
    """
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response
    return session


class TestDownloadLogoSsrf:
    """Tests for SSRF guards in ``LogoStorageService._download_logo``."""

    def test_refuses_internal_url(self) -> None:
        """An unsafe (internal) URL is refused before any request is made."""
        service = LogoStorageService()

        with patch(
            "core.services.logo_storage.create_pinned_session",
            side_effect=UnsafeUrlError("unsafe"),
        ) as mock_factory:
            data, content_type = service._download_logo(
                "http://169.254.169.254/latest/meta-data/"
            )

        assert data is None
        assert content_type == ""
        mock_factory.assert_called_once_with("http://169.254.169.254/latest/meta-data/")

    def test_disables_redirects_and_refuses_redirect_response(self) -> None:
        """Redirects are disabled and any 3xx response is treated as failure."""
        service = LogoStorageService()

        response = Mock()
        response.status_code = 302
        session = _mock_session(response)

        with patch(
            "core.services.logo_storage.create_pinned_session",
            return_value=session,
        ):
            data, content_type = service._download_logo("https://example.com/logo.png")

        assert data is None
        assert content_type == ""
        # Redirect following must be disabled at the request layer.
        assert session.get.call_args.kwargs["allow_redirects"] is False
        response.raise_for_status.assert_not_called()

    def test_refuses_other_3xx_status(self) -> None:
        """A 3xx status is refused across the whole range, not just is_redirect."""
        service = LogoStorageService()

        response = Mock()
        response.status_code = 304  # Not an is_redirect status, still a 3xx.
        session = _mock_session(response)

        with patch(
            "core.services.logo_storage.create_pinned_session",
            return_value=session,
        ):
            data, content_type = service._download_logo("https://example.com/logo.png")

        assert data is None
        assert content_type == ""
        response.raise_for_status.assert_not_called()

    def test_downloads_safe_public_url(self) -> None:
        """A safe public URL downloads and returns logo bytes."""
        service = LogoStorageService()

        response = Mock()
        response.status_code = 200
        response.raise_for_status = Mock()
        response.headers = {
            "Content-Type": "image/png",
            "Content-Length": "4",
        }
        response.iter_content = Mock(return_value=[b"dead"])
        session = _mock_session(response)

        with patch(
            "core.services.logo_storage.create_pinned_session",
            return_value=session,
        ):
            data, content_type = service._download_logo("https://example.com/logo.png")

        assert data == b"dead"
        assert content_type == "image/png"

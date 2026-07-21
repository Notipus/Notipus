"""Tests for the logo storage service SSRF protections.

Tests cover:
- ``_download_logo`` refusing URLs that resolve to internal hosts
- ``_download_logo`` disabling redirects and refusing redirect responses
- ``_download_logo`` succeeding for a safe public URL

All network access is mocked; the SSRF safety check is patched so tests
never perform real DNS resolution (the suite runs with --disable-socket).
"""

from unittest.mock import Mock, patch

from core.services.logo_storage import LogoStorageService


class TestDownloadLogoSsrf:
    """Tests for SSRF guards in ``LogoStorageService._download_logo``."""

    def test_refuses_internal_url(self) -> None:
        """An unsafe (internal) URL is refused before any request is made."""
        service = LogoStorageService()

        with (
            patch(
                "core.services.logo_storage.is_safe_public_url",
                return_value=False,
            ) as mock_safe,
            patch("core.services.logo_storage.requests.get") as mock_get,
        ):
            data, content_type = service._download_logo(
                "http://169.254.169.254/latest/meta-data/"
            )

        assert data is None
        assert content_type == ""
        mock_safe.assert_called_once_with("http://169.254.169.254/latest/meta-data/")
        mock_get.assert_not_called()

    def test_disables_redirects_and_refuses_redirect_response(self) -> None:
        """Redirects are disabled and a redirect response is treated as failure."""
        service = LogoStorageService()

        response = Mock()
        response.is_redirect = True
        response.is_permanent_redirect = False

        with (
            patch(
                "core.services.logo_storage.is_safe_public_url",
                return_value=True,
            ),
            patch(
                "core.services.logo_storage.requests.get", return_value=response
            ) as mock_get,
        ):
            data, content_type = service._download_logo("https://example.com/logo.png")

        assert data is None
        assert content_type == ""
        # Redirect following must be disabled at the request layer.
        assert mock_get.call_args.kwargs["allow_redirects"] is False
        response.raise_for_status.assert_not_called()

    def test_downloads_safe_public_url(self) -> None:
        """A safe public URL downloads and returns logo bytes."""
        service = LogoStorageService()

        response = Mock()
        response.is_redirect = False
        response.is_permanent_redirect = False
        response.raise_for_status = Mock()
        response.headers = {
            "Content-Type": "image/png",
            "Content-Length": "4",
        }
        response.iter_content = Mock(return_value=[b"dead"])

        with (
            patch(
                "core.services.logo_storage.is_safe_public_url",
                return_value=True,
            ),
            patch("core.services.logo_storage.requests.get", return_value=response),
        ):
            data, content_type = service._download_logo("https://example.com/logo.png")

        assert data == b"dead"
        assert content_type == "image/png"

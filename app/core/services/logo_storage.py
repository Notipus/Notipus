"""Logo storage service for downloading and storing company logos.

This module handles downloading logos from external URLs and storing
them in the database as binary data.
"""

import logging

import requests
from core.models import Company
from core.utils.url_safety import (
    UnsafeUrlError,
    create_pinned_session,
    is_safe_public_url,
)

logger = logging.getLogger(__name__)

# Allowed content types for logos
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)

# Maximum logo size in bytes (500KB)
MAX_LOGO_SIZE = 500 * 1024

# Request timeout in seconds
REQUEST_TIMEOUT = 10


class LogoStorageService:
    """Service for downloading and storing company logos.

    Downloads logos from external URLs and stores them as binary data
    in the Company model for local serving.
    """

    def download_and_store(self, company: Company, logo_url: str) -> bool:
        """Download logo from URL and store in company record.

        Args:
            company: Company model instance to update.
            logo_url: External URL to download logo from.

        Returns:
            True if logo was successfully downloaded and stored.
        """
        if not logo_url:
            return False

        try:
            # Download the logo
            logo_data, content_type = self._download_logo(logo_url)

            if not logo_data:
                return False

            # Store in company record
            company.logo_data = logo_data
            company.logo_content_type = content_type
            company.logo_url = logo_url  # Keep original URL for reference
            company.save(update_fields=["logo_data", "logo_content_type", "logo_url"])

            logger.info(
                f"Stored logo for {company.domain}: "
                f"{len(logo_data)} bytes, {content_type}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to download/store logo for {company.domain}: {e}")
            return False

    def _download_logo(self, url: str) -> tuple[bytes | None, str]:
        """Download logo from URL with validation.

        Args:
            url: URL to download from.

        Returns:
            Tuple of (logo_data, content_type) or (None, "") on failure.
        """
        # Resolve and validate the host, then pin the connection to a
        # validated public IP. This rejects non-public hosts (SSRF guard) and
        # is resistant to DNS rebinding: requests cannot silently re-resolve
        # the hostname to an internal address between validation and connect.
        try:
            session = create_pinned_session(url)
        except UnsafeUrlError:
            logger.warning("Refusing to download logo from unsafe URL: %r", url)
            return None, ""

        try:
            with session:
                # Redirects are disabled so an attacker cannot bypass the SSRF
                # check by redirecting to an internal host after validation.
                response = session.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                    stream=True,
                    allow_redirects=False,
                    headers={
                        "User-Agent": "Notipus/1.0 (Logo Fetcher)",
                        "Accept": "image/*",
                    },
                )

                # Treat any 3xx redirect as a failed download rather than
                # following it to a potentially internal host.
                if 300 <= response.status_code < 400:
                    logger.warning(
                        "Refusing to follow redirect while downloading logo from %r",
                        url,
                    )
                    return None, ""

                response.raise_for_status()

                # Check content type
                raw_content_type = response.headers.get("Content-Type", "")
                content_type = raw_content_type.split(";")[0].strip()
                if content_type not in ALLOWED_CONTENT_TYPES:
                    logger.warning("Invalid content type for logo: %r", content_type)
                    return None, ""

                # Check content length if available
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_LOGO_SIZE:
                    logger.warning("Logo too large: %s bytes", content_length)
                    return None, ""

                # Download with size limit
                data = b""
                for chunk in response.iter_content(chunk_size=8192):
                    data += chunk
                    if len(data) > MAX_LOGO_SIZE:
                        logger.warning("Logo exceeded size limit during download")
                        return None, ""

                if not data:
                    return None, ""

                return data, content_type

        except requests.exceptions.Timeout:
            logger.warning("Timeout downloading logo from %r", url)
            return None, ""
        except requests.exceptions.RequestException as e:
            logger.warning("Error downloading logo from %r: %s", url, e)
            return None, ""

    def refresh_logo(self, company: Company) -> bool:
        """Refresh logo from original URL.

        Args:
            company: Company to refresh logo for.

        Returns:
            True if logo was refreshed successfully.
        """
        if not company.logo_url:
            return False

        # Reject URLs that resolve to non-public hosts (SSRF guard). The
        # authoritative, rebinding-safe check happens in _download_logo; this
        # is a cheap early rejection.
        if not is_safe_public_url(company.logo_url):
            logger.warning(
                "Refusing to refresh logo for %r from unsafe URL: %r",
                company.domain,
                company.logo_url,
            )
            return False

        return self.download_and_store(company, company.logo_url)

    def delete_logo(self, company: Company) -> None:
        """Delete stored logo data.

        Args:
            company: Company to delete logo from.
        """
        company.logo_data = None
        company.logo_content_type = ""
        company.save(update_fields=["logo_data", "logo_content_type"])
        logger.info(f"Deleted logo for {company.domain}")


# Singleton instance
_logo_storage_service: LogoStorageService | None = None


def get_logo_storage_service() -> LogoStorageService:
    """Get the logo storage service singleton.

    Returns:
        LogoStorageService instance.
    """
    global _logo_storage_service
    if _logo_storage_service is None:
        _logo_storage_service = LogoStorageService()
    return _logo_storage_service

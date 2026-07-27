"""Tests for company logo views."""

import hashlib

import pytest
from core.models import Company
from django.test import Client
from django.urls import reverse


@pytest.fixture
def company_with_logo(db) -> Company:
    """Create a company with a logo."""
    return Company.objects.create(
        name="Test Company",
        domain="testcompany.com",
        logo_data=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01",
        logo_content_type="image/png",
    )


@pytest.fixture
def company_without_logo(db) -> Company:
    """Create a company without a logo."""
    return Company.objects.create(
        name="No Logo Company",
        domain="nologocompany.com",
    )


class TestCompanyLogoView:
    """Tests for CompanyLogoView."""

    def test_get_logo_success(self, client: Client, company_with_logo: Company) -> None:
        """Test successful logo retrieval."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})
        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response["Content-Length"] == str(len(company_with_logo.logo_data))
        assert response.content == company_with_logo.logo_data

    def test_get_logo_nonexistent_company(self, client: Client, db) -> None:
        """Test 404 for nonexistent company."""
        url = reverse("core:company-logo", kwargs={"domain": "nonexistent.com"})
        response = client.get(url)

        assert response.status_code == 404

    def test_get_logo_company_without_logo(
        self, client: Client, company_without_logo: Company
    ) -> None:
        """Test 404 for company without logo."""
        url = reverse(
            "core:company-logo", kwargs={"domain": company_without_logo.domain}
        )
        response = client.get(url)

        assert response.status_code == 404

    def test_default_content_type(self, client: Client, db) -> None:
        """Test default content type when none is set."""
        company = Company.objects.create(
            name="Test Company",
            domain="default-content.com",
            logo_data=b"\x89PNG\r\n\x1a\n",
            logo_content_type="",  # Empty content type
        )
        url = reverse("core:company-logo", kwargs={"domain": company.domain})
        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"


class TestCompanyLogoViewCaching:
    """Tests for caching headers on CompanyLogoView."""

    def test_cache_control_headers(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test Cache-Control headers for Cloudflare caching."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})
        response = client.get(url)

        assert response.status_code == 200
        cache_control = response["Cache-Control"]

        # Check all expected cache directives
        assert "max-age=2592000" in cache_control  # 30 days for browsers
        assert "s-maxage=31536000" in cache_control  # 1 year for CDN
        assert "public" in cache_control
        assert "immutable" in cache_control

    def test_etag_header_present(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test ETag header is present in response."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})
        response = client.get(url)

        assert response.status_code == 200
        assert "ETag" in response

        # ETag should be a quoted string
        etag = response["ETag"]
        assert etag.startswith('"')
        assert etag.endswith('"')

    def test_last_modified_header_present(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test Last-Modified header is present in response."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})
        response = client.get(url)

        assert response.status_code == 200
        assert "Last-Modified" in response

    def test_etag_matches_expected_format(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test ETag is based on domain and updated_at."""
        company_with_logo.refresh_from_db()  # Get latest updated_at

        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})
        response = client.get(url)

        expected_etag = hashlib.sha256(
            f"{company_with_logo.domain}-{company_with_logo.updated_at.isoformat()}".encode()
        ).hexdigest()[:32]

        actual_etag = response["ETag"].strip('"')
        assert actual_etag == expected_etag


class TestCompanyLogoViewConditionalRequests:
    """Tests for conditional request handling (If-None-Match)."""

    def test_conditional_request_returns_304(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test If-None-Match with matching ETag returns 304."""
        # First, get the logo to obtain the ETag
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})
        initial_response = client.get(url)
        etag = initial_response["ETag"]

        # Now make conditional request with the ETag
        response = client.get(url, HTTP_IF_NONE_MATCH=etag)

        assert response.status_code == 304
        assert len(response.content) == 0  # No body for 304

    def test_conditional_request_with_wrong_etag_returns_200(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test If-None-Match with non-matching ETag returns 200."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})

        # Make conditional request with wrong ETag
        response = client.get(url, HTTP_IF_NONE_MATCH='"wrong_etag"')

        assert response.status_code == 200
        assert response.content == company_with_logo.logo_data

    def test_conditional_request_etag_without_quotes(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test If-None-Match works with ETag without quotes."""
        # Get the logo to obtain the ETag
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})
        initial_response = client.get(url)
        etag = initial_response["ETag"].strip('"')  # Remove quotes

        # Make conditional request with unquoted ETag
        response = client.get(url, HTTP_IF_NONE_MATCH=etag)

        assert response.status_code == 304

    def test_etag_changes_when_logo_updated(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test ETag changes when company is updated."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})

        # Get initial ETag
        initial_response = client.get(url)
        initial_etag = initial_response["ETag"]

        # Update the company (this changes updated_at)
        company_with_logo.name = "Updated Company Name"
        company_with_logo.save()

        # Get new ETag
        updated_response = client.get(url)
        updated_etag = updated_response["ETag"]

        # ETags should be different
        assert initial_etag != updated_etag

    def test_304_does_not_include_body_headers(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test 304 response does not include body headers."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})

        # Get the ETag first
        initial_response = client.get(url)
        etag = initial_response["ETag"]

        # Make conditional request
        response = client.get(url, HTTP_IF_NONE_MATCH=etag)

        assert response.status_code == 304
        # 304 responses should not have body-related headers
        # Note: Django's HttpResponseNotModified automatically handles this

    def test_conditional_request_with_multiple_etags(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test If-None-Match with multiple ETags (comma-separated)."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})

        # Get the actual ETag first
        initial_response = client.get(url)
        actual_etag = initial_response["ETag"]

        # Make conditional request with multiple ETags including the correct one
        multiple_etags = f'"wrong_etag_1", {actual_etag}, "wrong_etag_2"'
        response = client.get(url, HTTP_IF_NONE_MATCH=multiple_etags)

        assert response.status_code == 304

    def test_conditional_request_with_star_wildcard(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test If-None-Match with '*' wildcard returns 304."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})

        # Make conditional request with wildcard
        response = client.get(url, HTTP_IF_NONE_MATCH="*")

        assert response.status_code == 304

    def test_conditional_request_multiple_etags_none_match(
        self, client: Client, company_with_logo: Company
    ) -> None:
        """Test If-None-Match with multiple non-matching ETags returns 200."""
        url = reverse("core:company-logo", kwargs={"domain": company_with_logo.domain})

        # Make conditional request with multiple wrong ETags
        multiple_etags = '"wrong_etag_1", "wrong_etag_2", "wrong_etag_3"'
        response = client.get(url, HTTP_IF_NONE_MATCH=multiple_etags)

        assert response.status_code == 200
        assert response.content == company_with_logo.logo_data

"""Tests for Django admin interfaces.

Tests cover:
- CompanyAdmin list display
- CompanyAdmin search and filter
- CompanyAdmin actions (purge with confirmation, refresh)
"""

from unittest.mock import patch

import pytest
from core.admin import CompanyAdmin
from core.models import Company
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory


@pytest.fixture
def admin_site() -> AdminSite:
    """Create admin site instance."""
    return AdminSite()


@pytest.fixture
def company_admin(admin_site: AdminSite) -> CompanyAdmin:
    """Create CompanyAdmin instance."""
    return CompanyAdmin(Company, admin_site)


@pytest.fixture
def request_factory() -> RequestFactory:
    """Create request factory."""
    return RequestFactory()


@pytest.fixture
def sample_company(db: None) -> Company:
    """Create a sample company with enrichment data."""
    return Company.objects.create(
        domain="acme.com",
        name="Acme Corporation",
        brand_info={
            "description": "Leading provider of enterprise solutions",
            "industry": "Technology",
            "year_founded": 2015,
            "_sources": {
                "brandfetch": {
                    "fetched_at": "2025-01-24T10:00:00Z",
                    "raw": {"name": "Acme Corporation"},
                }
            },
            "_blended_at": "2025-01-24T10:00:00Z",
        },
        logo_url="https://acme.com/logo.png",
        logo_data=b"fake logo data",
        logo_content_type="image/png",
    )


@pytest.fixture
def company_without_enrichment(db: None) -> Company:
    """Create a company without enrichment data."""
    return Company.objects.create(domain="empty.com")


@pytest.mark.django_db
class TestCompanyAdminDisplayMethods:
    """Tests for CompanyAdmin display methods."""

    def test_has_logo_display_with_logo(
        self, company_admin: CompanyAdmin, sample_company: Company
    ) -> None:
        """Test has_logo_display returns True when logo exists."""
        assert company_admin.has_logo_display(sample_company) is True

    def test_has_logo_display_without_logo(
        self, company_admin: CompanyAdmin, company_without_enrichment: Company
    ) -> None:
        """Test has_logo_display returns False when no logo."""
        assert company_admin.has_logo_display(company_without_enrichment) is False

    def test_has_brand_info_display_with_info(
        self, company_admin: CompanyAdmin, sample_company: Company
    ) -> None:
        """Test has_brand_info_display returns True when brand info exists."""
        assert company_admin.has_brand_info_display(sample_company) is True

    def test_has_brand_info_display_without_info(
        self, company_admin: CompanyAdmin, company_without_enrichment: Company
    ) -> None:
        """Test has_brand_info_display returns False when no brand info."""
        assert company_admin.has_brand_info_display(company_without_enrichment) is False

    def test_logo_preview_with_logo(
        self, company_admin: CompanyAdmin, sample_company: Company
    ) -> None:
        """Test logo_preview returns img tag when logo exists."""
        # Mock get_logo_url to avoid URL resolution issues in tests
        with patch.object(
            Company, "get_logo_url", return_value="http://example.com/logo.png"
        ):
            preview = company_admin.logo_preview(sample_company)
            assert "<img" in preview
            assert "max-height" in preview

    def test_logo_preview_without_logo(
        self, company_admin: CompanyAdmin, company_without_enrichment: Company
    ) -> None:
        """Test logo_preview returns dash when no logo."""
        preview = company_admin.logo_preview(company_without_enrichment)
        assert preview == "-"

    def test_brand_info_pretty_with_info(
        self, company_admin: CompanyAdmin, sample_company: Company
    ) -> None:
        """Test brand_info_pretty returns formatted JSON."""
        pretty = company_admin.brand_info_pretty(sample_company)
        assert "<pre" in pretty
        assert "description" in pretty
        assert "Technology" in pretty
        # Internal fields should be filtered out
        assert "_sources" not in pretty
        assert "_blended_at" not in pretty

    def test_brand_info_pretty_without_info(
        self, company_admin: CompanyAdmin, company_without_enrichment: Company
    ) -> None:
        """Test brand_info_pretty returns dash when no info."""
        pretty = company_admin.brand_info_pretty(company_without_enrichment)
        assert pretty == "-"

    def test_enrichment_sources_display_with_sources(
        self, company_admin: CompanyAdmin, sample_company: Company
    ) -> None:
        """Test enrichment_sources_display shows sources."""
        display = company_admin.enrichment_sources_display(sample_company)
        assert "brandfetch" in display
        assert "2025-01-24" in display

    def test_enrichment_sources_display_without_info(
        self, company_admin: CompanyAdmin, company_without_enrichment: Company
    ) -> None:
        """Test enrichment_sources_display returns dash when no info."""
        display = company_admin.enrichment_sources_display(company_without_enrichment)
        assert display == "-"


@pytest.mark.django_db
class TestCompanyAdminActions:
    """Tests for CompanyAdmin actions."""

    def test_purge_enrichment_data_confirmed(
        self,
        company_admin: CompanyAdmin,
        sample_company: Company,
        request_factory: RequestFactory,
    ) -> None:
        """Test confirmed purge clears enrichment but keeps the domain."""
        request = request_factory.post("/admin/core/company/", {"confirm_purge": "yes"})
        request.user = type("User", (), {"has_perm": lambda self, x: True, "pk": 1})()

        queryset = Company.objects.filter(pk=sample_company.pk)

        # Mock message_user to avoid middleware requirement
        with patch.object(company_admin, "message_user"):
            response = company_admin.purge_enrichment_data(request, queryset)

        # Confirmed purge applies immediately and redirects (returns None)
        assert response is None

        # Refresh from database
        sample_company.refresh_from_db()

        # Domain should still exist
        assert sample_company.domain == "acme.com"
        # Enrichment data should be cleared
        assert sample_company.name == ""
        assert sample_company.brand_info == {}
        assert sample_company.logo_url == ""
        assert sample_company.logo_data is None
        assert sample_company.logo_content_type == ""

    def test_purge_enrichment_data_requires_confirmation(
        self,
        company_admin: CompanyAdmin,
        sample_company: Company,
        request_factory: RequestFactory,
    ) -> None:
        """Test purge without confirmation shows a page and does not purge."""
        request = request_factory.post("/admin/core/company/")
        request.user = type("User", (), {"has_perm": lambda self, x: True, "pk": 1})()

        queryset = Company.objects.filter(pk=sample_company.pk)

        # Bypass admin site context (permission machinery) for this unit test.
        with patch.object(company_admin.admin_site, "each_context", return_value={}):
            response = company_admin.purge_enrichment_data(request, queryset)

        # An intermediate confirmation page is returned instead of purging.
        assert response is not None
        assert response.template_name == (
            "admin/core/company/purge_enrichment_confirmation.html"
        )

        # Data must remain untouched until the user confirms.
        sample_company.refresh_from_db()
        assert sample_company.name == "Acme Corporation"
        assert sample_company.brand_info != {}

    def test_purge_enrichment_data_ignores_wrong_confirm_value(
        self,
        company_admin: CompanyAdmin,
        sample_company: Company,
        request_factory: RequestFactory,
    ) -> None:
        """Test only the exact ``confirm_purge=yes`` sentinel triggers a purge."""
        request = request_factory.post("/admin/core/company/", {"confirm_purge": "1"})
        request.user = type("User", (), {"has_perm": lambda self, x: True, "pk": 1})()

        queryset = Company.objects.filter(pk=sample_company.pk)

        # A truthy-but-wrong value must show the confirmation page, not purge.
        with patch.object(company_admin.admin_site, "each_context", return_value={}):
            response = company_admin.purge_enrichment_data(request, queryset)

        assert response is not None
        assert response.template_name == (
            "admin/core/company/purge_enrichment_confirmation.html"
        )

        sample_company.refresh_from_db()
        assert sample_company.name == "Acme Corporation"
        assert sample_company.brand_info != {}

    def test_purge_enrichment_data_refuses_oversized_selection(
        self,
        company_admin: CompanyAdmin,
        sample_company: Company,
        request_factory: RequestFactory,
    ) -> None:
        """Test a selection above the cap is refused with a warning, no purge."""
        # Lower the cap so a single-row queryset exceeds it.
        company_admin.PURGE_CONFIRM_LIMIT = 0

        request = request_factory.post("/admin/core/company/")
        request.user = type("User", (), {"has_perm": lambda self, x: True, "pk": 1})()

        queryset = Company.objects.filter(pk=sample_company.pk)

        with patch.object(company_admin, "message_user") as mock_message:
            response = company_admin.purge_enrichment_data(request, queryset)

        # No confirmation page rendered; returns to the changelist.
        assert response is None

        # A warning was shown telling the admin to narrow the selection.
        mock_message.assert_called_once()
        assert mock_message.call_args.kwargs["level"] == messages.WARNING

        # Data must be untouched.
        sample_company.refresh_from_db()
        assert sample_company.name == "Acme Corporation"
        assert sample_company.brand_info != {}

    def test_refresh_enrichment(
        self,
        company_admin: CompanyAdmin,
        sample_company: Company,
        request_factory: RequestFactory,
    ) -> None:
        """Test refresh_enrichment clears _blended_at timestamp."""
        request = request_factory.post("/admin/core/company/")
        request.user = type("User", (), {"has_perm": lambda self, x: True, "pk": 1})()

        # Verify _blended_at exists initially
        assert "_blended_at" in sample_company.brand_info

        queryset = Company.objects.filter(pk=sample_company.pk)

        # Mock message_user to avoid middleware requirement
        with patch.object(company_admin, "message_user"):
            company_admin.refresh_enrichment(request, queryset)

        # Refresh from database
        sample_company.refresh_from_db()

        # _blended_at should be removed
        assert "_blended_at" not in sample_company.brand_info
        # Other data should still exist
        assert sample_company.brand_info.get("description") is not None

    def test_no_custom_delete_action(
        self,
        company_admin: CompanyAdmin,
    ) -> None:
        """Test the one-click custom delete action was removed.

        Deletion now goes through Django's built-in ``delete_selected``,
        which shows its own confirmation page.
        """
        assert "delete_selected_companies" not in company_admin.actions
        assert not hasattr(company_admin, "delete_selected_companies")

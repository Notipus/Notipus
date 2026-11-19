"""Tests for company domain normalization.

A Company is stored under its normalized domain, so lookups must use the
same key. These cover the case where a customer email carries a "www."
host and the lookup used to miss the stored row.
"""

import pytest
from core.models import Company, validate_domain
from core.services.enrichment import DomainEnrichmentService
from core.utils.domain import normalize_company_domain
from core.utils.email_domain import extract_domain


class TestNormalizeCompanyDomain:
    """Tests for normalize_company_domain."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("example.com", "example.com"),
            ("  Example.COM ", "example.com"),
            ("www.example.com", "example.com"),
            ("https://www.example.com/about", "example.com"),
            ("http://example.com/", "example.com"),
            ("www.shop.example.com", "shop.example.com"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        """Scheme, path, case and a leading www. are removed."""
        assert normalize_company_domain(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["news-www.example.com", "mywww.example.com", "api.www.example.com"],
    )
    def test_www_only_stripped_as_leading_label(self, raw: str) -> None:
        """ "www." elsewhere in the hostname is left untouched."""
        assert normalize_company_domain(raw) == raw

    def test_keeps_www_before_bare_tld(self) -> None:
        """Stripping www. from www.com would leave only a TLD."""
        assert normalize_company_domain("www.com") == "www.com"


class TestValidateDomainWww:
    """validate_domain must not mangle hostnames containing "www."."""

    def test_does_not_strip_www_mid_hostname(self) -> None:
        """news-www.example.com used to become news-example.com."""
        assert validate_domain("news-www.example.com") == "news-www.example.com"

    def test_strips_path(self) -> None:
        """A pasted URL with a path is reduced to its host."""
        assert validate_domain("https://www.example.com/pricing") == "example.com"


class TestExtractDomainWww:
    """extract_domain returns the canonical company domain."""

    def test_strips_leading_www(self) -> None:
        """jane@www.example.com belongs to example.com."""
        assert extract_domain("jane@www.example.com") == "example.com"


@pytest.mark.django_db
class TestEnrichDomainWww:
    """enrich_domain finds companies regardless of a leading www."""

    @pytest.fixture
    def service(self) -> DomainEnrichmentService:
        """Service with no providers, so only the lookup is exercised."""
        svc = DomainEnrichmentService()
        svc._plugins = []
        svc._llm_plugins = []
        return svc

    def test_finds_existing_company(self, service: DomainEnrichmentService) -> None:
        """A www. lookup returns the company stored without www."""
        existing = Company.objects.create(domain="example.com", name="Example")
        assert service.enrich_domain("www.example.com") == existing

    def test_repeated_lookup_returns_same_company(
        self, service: DomainEnrichmentService
    ) -> None:
        """The second www. lookup used to fail on the unique constraint."""
        first = service.enrich_domain("www.example.org")
        second = service.enrich_domain("www.example.org")
        assert first is not None
        assert first == second
        assert first.domain == "example.org"
        assert Company.objects.filter(domain="example.org").count() == 1

    def test_refresh_uses_normalized_domain(
        self, service: DomainEnrichmentService
    ) -> None:
        """refresh_enrichment also resolves the www. form."""
        existing = Company.objects.create(domain="example.net", name="Example")
        assert service.refresh_enrichment("www.example.net") == existing

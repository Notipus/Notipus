"""Tests for email domain filtering utilities.

Tests cover:
- Email sanitization and validation
- Domain extraction
- Free email provider detection
- Disposable email detection
- Hosted email domain detection
- Combined enrichability check
"""

from core.utils.email_domain import (
    extract_domain,
    is_disposable_email,
    is_enrichable_domain,
    is_free_email_provider,
    is_hosted_email_domain,
    is_valid_email,
    sanitize_email_input,
)


class TestSanitizeEmailInput:
    """Tests for sanitize_email_input function."""

    def test_valid_email(self) -> None:
        """Test sanitization of valid email."""
        result = sanitize_email_input("User@Example.COM")
        assert result == "user@example.com"

    def test_strips_whitespace(self) -> None:
        """Test whitespace stripping."""
        result = sanitize_email_input("  user@example.com  ")
        assert result == "user@example.com"

    def test_rejects_non_string(self) -> None:
        """Test rejection of non-string input."""
        assert sanitize_email_input(None) is None
        assert sanitize_email_input(123) is None
        assert sanitize_email_input([]) is None
        assert sanitize_email_input({}) is None

    def test_rejects_empty_string(self) -> None:
        """Test rejection of empty string."""
        assert sanitize_email_input("") is None
        assert sanitize_email_input("   ") is None

    def test_rejects_oversized_input(self) -> None:
        """Test rejection of input exceeding max length."""
        oversized = "a" * 300 + "@example.com"
        assert sanitize_email_input(oversized) is None

    def test_rejects_null_bytes(self) -> None:
        """Test rejection of null byte injection."""
        assert sanitize_email_input("user\x00@example.com") is None

    def test_rejects_control_characters(self) -> None:
        """Test rejection of control characters."""
        assert sanitize_email_input("user\n@example.com") is None
        assert sanitize_email_input("user\t@example.com") is None
        assert sanitize_email_input("user\r@example.com") is None


class TestIsValidEmail:
    """Tests for is_valid_email function."""

    def test_valid_emails(self) -> None:
        """Test validation of valid email formats."""
        valid_emails = [
            "user@example.com",
            "name.surname@company.co.uk",
            "user+tag@example.com",
            "user123@test.org",
            "first.last@domain.io",
        ]
        for email in valid_emails:
            assert is_valid_email(email) is True, f"{email} should be valid"

    def test_invalid_format(self) -> None:
        """Test rejection of invalid formats."""
        invalid_emails = [
            "not-an-email",
            "missing@tld",
            "@nodomain.com",
            "nodomain@",
            "",
            "spaces in@email.com",
        ]
        for email in invalid_emails:
            assert is_valid_email(email) is False, f"{email} should be invalid"

    def test_rejects_ip_domains(self) -> None:
        """Test rejection of IP address domains."""
        assert is_valid_email("user@192.168.1.1") is False
        assert is_valid_email("user@10.0.0.1") is False

    def test_rejects_localhost(self) -> None:
        """Test rejection of localhost domains."""
        assert is_valid_email("user@localhost") is False
        assert is_valid_email("user@localhost.localdomain") is False


class TestExtractDomain:
    """Tests for extract_domain function."""

    def test_extracts_domain(self) -> None:
        """Test basic domain extraction."""
        assert extract_domain("user@example.com") == "example.com"
        assert extract_domain("name@company.co.uk") == "company.co.uk"

    def test_lowercases_domain(self) -> None:
        """Test domain is lowercased."""
        assert extract_domain("User@EXAMPLE.COM") == "example.com"

    def test_returns_none_for_invalid(self) -> None:
        """Test None return for invalid emails."""
        assert extract_domain("not-an-email") is None
        assert extract_domain("") is None
        assert extract_domain(None) is None  # type: ignore[arg-type]


class TestIsFreeEmailProvider:
    """Tests for is_free_email_provider function."""

    def test_detects_free_providers(self) -> None:
        """Test detection of known free providers."""
        free_providers = [
            "gmail.com",
            "yahoo.com",
            "hotmail.com",
            "outlook.com",
            "icloud.com",
            "protonmail.com",
            "aol.com",
            "mail.com",
            "zoho.com",
            "yandex.com",
            "privaterelay.appleid.com",  # Apple Hide My Email
        ]
        for domain in free_providers:
            assert is_free_email_provider(domain) is True, f"{domain} should be free"

    def test_rejects_company_domains(self) -> None:
        """Test rejection of company domains."""
        company_domains = [
            "acme.com",
            "company.io",
            "startup.co",
            "enterprise.org",
        ]
        for domain in company_domains:
            assert not is_free_email_provider(domain), f"{domain} should not be free"

    def test_case_insensitive(self) -> None:
        """Test case insensitivity."""
        assert is_free_email_provider("GMAIL.COM") is True
        assert is_free_email_provider("Gmail.Com") is True


class TestIsDisposableEmail:
    """Tests for is_disposable_email function."""

    def test_detects_disposable_domains(self) -> None:
        """Test detection of known disposable domains."""
        disposable_domains = [
            "mailinator.com",
            "guerrillamail.com",
            "10minutemail.com",
        ]
        for domain in disposable_domains:
            assert is_disposable_email(domain) is True, f"{domain} should be disposable"

    def test_rejects_real_domains(self) -> None:
        """Test rejection of real domains."""
        real_domains = [
            "gmail.com",
            "acme.com",
            "company.io",
        ]
        for domain in real_domains:
            assert not is_disposable_email(domain), f"{domain} not disposable"


class TestIsEnrichableDomain:
    """Tests for is_enrichable_domain function."""

    def test_enrichable_company_email(self) -> None:
        """Test that company emails are enrichable."""
        enrichable_emails = [
            "john@acme.com",
            "jane@startup.io",
            "contact@enterprise.org",
        ]
        for email in enrichable_emails:
            assert is_enrichable_domain(email) is True, f"{email} should be enrichable"

    def test_not_enrichable_free_provider(self) -> None:
        """Test that free provider emails are not enrichable."""
        free_emails = [
            "user@gmail.com",
            "user@yahoo.com",
            "user@hotmail.com",
            "user@outlook.com",
        ]
        for email in free_emails:
            assert not is_enrichable_domain(email), f"{email} not enrichable"

    def test_not_enrichable_disposable(self) -> None:
        """Test that disposable emails are not enrichable."""
        disposable_emails = [
            "user@mailinator.com",
            "user@guerrillamail.com",
            "user@10minutemail.com",
        ]
        for email in disposable_emails:
            assert not is_enrichable_domain(email), f"{email} not enrichable"

    def test_not_enrichable_invalid(self) -> None:
        """Test that invalid emails are not enrichable."""
        invalid_emails = [
            "not-an-email",
            "",
            "user@",
            "@domain.com",
        ]
        for email in invalid_emails:
            assert not is_enrichable_domain(email), f"{email} not enrichable"

    def test_not_enrichable_hosted_email_domain(self) -> None:
        """Test that hosted email domains (like onmicrosoft.com) are not enrichable."""
        hosted_emails = [
            "user@widgetco.onmicrosoft.com",
            "admin@acmecorp.onmicrosoft.com",
            "contact@testorg.mail.onmicrosoft.com",
        ]
        for email in hosted_emails:
            assert not is_enrichable_domain(email), f"{email} not enrichable"

    def test_not_enrichable_apple_relay(self) -> None:
        """Test that Apple Hide My Email addresses are not enrichable."""
        assert not is_enrichable_domain("user@privaterelay.appleid.com")


class TestIsHostedEmailDomain:
    """Tests for is_hosted_email_domain function."""

    def test_detects_onmicrosoft_domains(self) -> None:
        """Test detection of Azure AD onmicrosoft.com domains."""
        hosted_domains = [
            "widgetco.onmicrosoft.com",
            "acmecorp.onmicrosoft.com",
            "testorg.mail.onmicrosoft.com",
            "democorp.mail.onmicrosoft.com",
        ]
        for domain in hosted_domains:
            assert is_hosted_email_domain(domain) is True, f"{domain} should be hosted"

    def test_rejects_regular_domains(self) -> None:
        """Test rejection of regular company domains."""
        regular_domains = [
            "acme.com",
            "company.io",
            "enterprise.org",
            "microsoft.com",
            "onmicrosoft.com",  # Base domain without subdomain is not hosted
        ]
        for domain in regular_domains:
            assert not is_hosted_email_domain(domain), f"{domain} should not be hosted"

    def test_rejects_free_providers(self) -> None:
        """Test rejection of free email providers."""
        free_providers = [
            "gmail.com",
            "outlook.com",
            "yahoo.com",
        ]
        for domain in free_providers:
            assert not is_hosted_email_domain(domain), f"{domain} should not be hosted"

    def test_case_insensitive(self) -> None:
        """Test case insensitivity."""
        assert is_hosted_email_domain("WIDGETCO.ONMICROSOFT.COM") is True
        assert is_hosted_email_domain("Acmecorp.OnMicrosoft.Com") is True

    def test_empty_and_none(self) -> None:
        """Test handling of empty and None values."""
        assert is_hosted_email_domain("") is False
        assert is_hosted_email_domain(None) is False  # type: ignore[arg-type]

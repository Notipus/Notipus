import uuid

from core.models import Company, NotificationSettings, Workspace, validate_domain
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase


class ValidateDomainTest(TestCase):
    """Test domain validation function"""

    def test_valid_domains(self):
        """Test valid domain formats"""
        valid_domains = [
            "example.com",
            "sub.example.com",
            "test-site.co.uk",
            "my-company.org",
            "shop.mystore.com",
        ]

        for domain in valid_domains:
            with self.subTest(domain=domain):
                result = validate_domain(domain)
                self.assertTrue(len(result) > 0)

    def test_domain_cleaning(self):
        """Test that protocols and www are removed"""
        test_cases = [
            ("https://example.com", "example.com"),
            ("http://www.example.com", "example.com"),
            ("www.example.com", "example.com"),
            ("EXAMPLE.COM", "example.com"),
        ]

        for input_domain, expected in test_cases:
            with self.subTest(input_domain=input_domain):
                result = validate_domain(input_domain)
                self.assertEqual(result, expected)

    def test_invalid_domains(self):
        """Test invalid domain formats"""
        invalid_domains = [
            "invalid",
            "http://",
            "ftp://example.com",
            "",
            "example..com",
            ".example.com",
            "example.com.",
        ]

        for domain in invalid_domains:
            with self.subTest(domain=domain):
                with self.assertRaises(ValidationError):
                    validate_domain(domain)


class CompanyTest(TestCase):
    """Test Company model"""

    def test_company_creation(self):
        """Test creating a company with valid domain"""
        company = Company.objects.create(
            domain="creation-test.com", name="Example Company"
        )
        self.assertEqual(company.domain, "creation-test.com")
        self.assertEqual(company.name, "Example Company")

    def test_company_str_representation(self):
        """Test string representation of company"""
        company = Company.objects.create(domain="str-test.com", name="Example Company")
        expected = "Example Company (str-test.com)"
        self.assertEqual(str(company), expected)

    def test_company_domain_validation(self):
        """Test domain validation on company creation"""
        with self.assertRaises(ValidationError):
            Company.objects.create(domain="invalid-domain")

    def test_company_unique_constraint(self):
        """Test unique constraint on domain"""
        # Use a unique domain for this test
        unique_domain = f"test-{uuid.uuid4().hex[:8]}.com"

        # Create first company
        Company.objects.create(domain=unique_domain, name="Test Company 1")

        # Try to create duplicate - this should raise ValidationError at model level
        with self.assertRaises(ValidationError):
            Company.objects.create(domain=unique_domain, name="Test Company 2")

    def test_company_domain_cleaning(self):
        """Test domain cleaning on save"""
        company = Company.objects.create(
            domain="https://www.CLEANING-TEST.COM", name="Example"
        )
        self.assertEqual(company.domain, "cleaning-test.com")


class WorkspaceTest(TestCase):
    """Test Workspace model"""

    def setUp(self):
        """Set up test data"""
        self.workspace = Workspace.objects.create(
            name="Test Org", shop_domain="test.myshopify.com"
        )

    def test_workspace_str_representation(self):
        """Test workspace string representation"""
        expected = "Test Org (test.myshopify.com)"
        self.assertEqual(str(self.workspace), expected)


class NotificationSettingsTest(TestCase):
    """Test NotificationSettings model"""

    def setUp(self):
        """Set up test data"""
        self.workspace = Workspace.objects.create(
            name="Test Org", shop_domain="test.myshopify.com"
        )

    def test_notification_settings_creation(self):
        """Test settings are created automatically by the post_save signal"""
        settings = NotificationSettings.objects.get(workspace=self.workspace)
        self.assertEqual(settings.workspace, self.workspace)
        self.assertTrue(settings.notify_payment_success)  # Default value

    def test_notification_settings_str_representation(self):
        """Test string representation"""
        settings = NotificationSettings.objects.get(workspace=self.workspace)
        expected = f"Notification Settings for {self.workspace.name}"
        self.assertEqual(str(settings), expected)

    def test_notification_settings_defaults(self):
        """Test default values"""
        settings = NotificationSettings.objects.get(workspace=self.workspace)
        # Test some key defaults
        self.assertTrue(settings.notify_payment_success)
        self.assertTrue(settings.notify_payment_failure)
        self.assertTrue(settings.notify_subscription_created)

    def test_notification_settings_one_per_workspace(self):
        """Test one settings instance per workspace"""
        # The post_save signal already created settings for the workspace
        self.assertTrue(
            NotificationSettings.objects.filter(workspace=self.workspace).exists()
        )

        # Test unique constraint
        with self.assertRaises(IntegrityError):
            NotificationSettings.objects.create(workspace=self.workspace)

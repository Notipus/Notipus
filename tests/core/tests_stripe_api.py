"""
Tests for Stripe API service.

This module contains comprehensive tests for the StripeAPI class, which handles
Stripe customer creation using the official Stripe SDK. The tests cover:

- Successful customer creation scenarios
- All major Stripe error types (CardError, APIError, RateLimitError, etc.)
- Edge cases like empty data and missing fields
- Logging behavior verification
- API key configuration testing

Test coverage focuses on error handling, data validation, and integration
with the Stripe SDK to ensure robust payment processing.
"""

from unittest.mock import Mock, patch

from core.services.stripe import StripeAPI
from django.test import TestCase


class StripeAPITest(TestCase):
    """Test StripeAPI service"""

    def setUp(self) -> None:
        """Set up test data"""
        self.api = StripeAPI()

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_success(self, mock_create) -> None:
        """Test successful customer creation"""
        # Mock successful customer creation
        mock_customer = Mock()
        mock_customer.to_dict.return_value = {
            "id": "cus_test123",
            "email": "test@example.com",
            "name": "Test Customer",
        }
        mock_create.return_value = mock_customer

        customer_data = {
            "email": "test@example.com",
            "name": "Test Customer",
            "metadata": {"source": "test"},
        }

        result = self.api.create_stripe_customer(customer_data)

        expected = {
            "id": "cus_test123",
            "email": "test@example.com",
            "name": "Test Customer",
        }

        self.assertEqual(result, expected)
        mock_create.assert_called_once_with(api_key="sk_test_dev_key", **customer_data)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_stripe_error(self, mock_create) -> None:
        """Test customer creation with Stripe error"""
        from stripe import StripeError

        # Mock Stripe error
        mock_create.side_effect = StripeError("Test Stripe error")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_api_error(self, mock_create) -> None:
        """Test customer creation with API error"""
        from stripe import APIError

        # Mock API error
        mock_create.side_effect = APIError("API connection failed")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_card_error(self, mock_create) -> None:
        """Test customer creation with card error"""
        from stripe import CardError

        # Mock card error
        mock_create.side_effect = CardError("Card declined", None, "card_declined")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_rate_limit_error(self, mock_create) -> None:
        """Test customer creation with rate limit error"""
        from stripe import RateLimitError

        # Mock rate limit error
        mock_create.side_effect = RateLimitError("Rate limit exceeded")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_authentication_error(self, mock_create) -> None:
        """Test customer creation with authentication error"""
        from stripe import AuthenticationError

        # Mock authentication error
        mock_create.side_effect = AuthenticationError("Invalid API key")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_permission_error(self, mock_create) -> None:
        """Test customer creation with permission error"""
        from stripe import PermissionError

        # Mock permission error
        mock_create.side_effect = PermissionError("Insufficient permissions")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_general_exception(self, mock_create) -> None:
        """Test customer creation with general exception"""
        # Mock general exception
        mock_create.side_effect = Exception("Unexpected error")

        customer_data = {"email": "test@example.com", "name": "Test Customer"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_empty_data(self, mock_create) -> None:
        """Test customer creation with empty data"""
        # Mock successful creation with empty data
        mock_customer = Mock()
        mock_customer.to_dict.return_value = {"id": "cus_empty"}
        mock_create.return_value = mock_customer

        result = self.api.create_stripe_customer({})

        self.assertEqual(result, {"id": "cus_empty"})
        mock_create.assert_called_once_with(api_key="sk_test_dev_key")

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_with_metadata(self, mock_create) -> None:
        """Test customer creation with metadata"""
        # Mock successful creation
        mock_customer = Mock()
        mock_customer.to_dict.return_value = {
            "id": "cus_metadata",
            "email": "test@example.com",
            "metadata": {"user_id": "123", "source": "webapp"},
        }
        mock_create.return_value = mock_customer

        customer_data = {
            "email": "test@example.com",
            "metadata": {"user_id": "123", "source": "webapp"},
        }

        result = self.api.create_stripe_customer(customer_data)

        self.assertEqual(result["metadata"]["user_id"], "123")
        self.assertEqual(result["metadata"]["source"], "webapp")

    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_with_address(self, mock_create) -> None:
        """Test customer creation with address"""
        # Mock successful creation
        mock_customer = Mock()
        mock_customer.to_dict.return_value = {
            "id": "cus_address",
            "email": "test@example.com",
            "address": {
                "line1": "123 Main St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94102",
                "country": "US",
            },
        }
        mock_create.return_value = mock_customer

        customer_data = {
            "email": "test@example.com",
            "address": {
                "line1": "123 Main St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94102",
                "country": "US",
            },
        }

        result = self.api.create_stripe_customer(customer_data)

        self.assertEqual(result["address"]["line1"], "123 Main St")
        self.assertEqual(result["address"]["city"], "San Francisco")

    @patch("core.services.stripe.stripe.Customer.create")
    @patch("core.services.stripe.logger")
    def test_create_stripe_customer_logs_stripe_error(
        self, mock_logger, mock_create
    ) -> None:
        """Test that Stripe errors are properly logged"""
        from stripe import CardError

        # Mock card error
        error_msg = "Your card was declined"
        mock_create.side_effect = CardError(error_msg, None, "card_declined")

        customer_data = {"email": "test@example.com"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)
        mock_logger.error.assert_called_once_with(
            f"Stripe error creating customer: {error_msg}"
        )

    @patch("core.services.stripe.stripe.Customer.create")
    @patch("core.services.stripe.logger")
    def test_create_stripe_customer_logs_general_error(
        self, mock_logger, mock_create
    ) -> None:
        """Test that general errors are properly logged"""
        # Mock general exception
        error_msg = "Network timeout"
        mock_create.side_effect = Exception(error_msg)

        customer_data = {"email": "test@example.com"}

        result = self.api.create_stripe_customer(customer_data)

        self.assertIsNone(result)
        expected_msg = f"Unexpected error creating Stripe customer: {error_msg}"
        mock_logger.error.assert_called_once_with(expected_msg)

    @patch("core.services.stripe.settings.STRIPE_SECRET_KEY", "sk_test_new_key")
    @patch("core.services.stripe.stripe.Customer.create")
    def test_create_stripe_customer_uses_correct_api_key(self, mock_create) -> None:
        """Test that the correct API key is used"""
        import core.services.stripe as stripe_module

        # Mock successful creation
        mock_customer = Mock()
        mock_customer.to_dict.return_value = {"id": "cus_test"}
        mock_create.return_value = mock_customer

        customer_data = {"email": "test@example.com"}

        # Create new instance to trigger API key setting (not self.api from setUp)
        new_api = StripeAPI()
        result = new_api.create_stripe_customer(customer_data)

        # Verify API key was set
        self.assertEqual(stripe_module.stripe.api_key, "sk_test_new_key")
        self.assertIsNotNone(result)

    @patch("core.services.stripe.stripe.Account.retrieve")
    def test_get_account_info_success(self, mock_retrieve) -> None:
        """Test successful account info retrieval"""
        # Mock successful account retrieval
        mock_account = Mock()
        mock_account.id = "acct_test123"
        mock_account.email = "test@example.com"
        mock_account.country = "US"
        mock_account.default_currency = "usd"
        mock_account.business_profile = Mock()
        mock_account.business_profile.name = "Test Business"
        mock_account.business_profile.url = "https://test.com"
        mock_retrieve.return_value = mock_account

        api = StripeAPI()
        result = api.get_account_info()

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "acct_test123")
        self.assertEqual(result["email"], "test@example.com")
        self.assertEqual(result["country"], "US")
        self.assertEqual(result["business_profile"]["name"], "Test Business")

    @patch("core.services.stripe.stripe.Account.retrieve")
    def test_get_account_info_invalid_api_key(self, mock_retrieve) -> None:
        """Test account info retrieval with invalid API key"""
        from stripe import AuthenticationError

        mock_retrieve.side_effect = AuthenticationError("Invalid API key")

        api = StripeAPI("sk_test_invalid")
        result = api.get_account_info()

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Account.retrieve")
    def test_get_account_info_stripe_error(self, mock_retrieve) -> None:
        """Test account info retrieval with Stripe error"""
        from stripe import StripeError

        mock_retrieve.side_effect = StripeError("API Error")

        api = StripeAPI()
        result = api.get_account_info()

        self.assertIsNone(result)

    @patch("core.services.stripe.stripe.Account.retrieve")
    def test_get_account_info_no_business_profile(self, mock_retrieve) -> None:
        """Test account info retrieval when business profile is None"""
        mock_account = Mock()
        mock_account.id = "acct_test123"
        mock_account.email = "test@example.com"
        mock_account.country = "US"
        mock_account.default_currency = "usd"
        mock_account.business_profile = None
        mock_retrieve.return_value = mock_account

        api = StripeAPI()
        result = api.get_account_info()

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "acct_test123")
        self.assertEqual(result["business_profile"], {})

    def test_init_with_custom_api_key(self) -> None:
        """Test StripeAPI initialization with custom API key"""
        custom_key = "sk_test_custom123"
        api = StripeAPI(api_key=custom_key)

        self.assertEqual(api.api_key, custom_key)

    @patch("core.services.stripe.settings.STRIPE_SECRET_KEY", "sk_test_default")
    def test_init_with_default_api_key(self) -> None:
        """Test StripeAPI initialization with default API key from settings"""
        api = StripeAPI()

        self.assertEqual(api.api_key, "sk_test_default")

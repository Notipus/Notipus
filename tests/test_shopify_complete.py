"""
Comprehensive tests for Shopify modules to achieve 80%+ test coverage
"""

import json
from unittest.mock import Mock, patch

import pytest
from core.services.shopify import ShopifyAPI
from plugins.sources.base import CustomerNotFoundError, InvalidDataError
from plugins.sources.shopify import ShopifySourcePlugin


class TestShopifySourcePlugin:
    """Comprehensive tests for ShopifySourcePlugin"""

    @pytest.fixture
    def provider(self):
        return ShopifySourcePlugin(webhook_secret="test_secret")

    def test_init(self, provider):
        """Test provider initialization"""
        assert provider.webhook_secret == "test_secret"
        assert provider._current_webhook_data is None

    def test_validate_shopify_request_invalid_content_type(self, provider):
        """Test validation with invalid content type"""
        mock_request = Mock()
        mock_request.content_type = "text/plain"

        with pytest.raises(InvalidDataError, match="Invalid content type"):
            provider._validate_shopify_request(mock_request)

    def test_validate_shopify_request_missing_topic(self, provider):
        """Test validation with missing topic header"""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {}

        with pytest.raises(InvalidDataError, match="Missing webhook topic"):
            provider._validate_shopify_request(mock_request)

    def test_validate_shopify_request_success(self, provider):
        """Test successful request validation"""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {"X-Shopify-Topic": "orders/paid"}

        topic = provider._validate_shopify_request(mock_request)
        assert topic == "orders/paid"

    def test_parse_shopify_json_invalid_json(self, provider):
        """Test parsing invalid JSON data"""
        mock_request = Mock()
        mock_request.data = b"invalid json"

        with pytest.raises(InvalidDataError, match="Invalid JSON data"):
            provider._parse_shopify_json(mock_request)

    def test_parse_shopify_json_attribute_error(self, provider):
        """Test parsing when request.data doesn't exist"""
        mock_request = Mock()
        del mock_request.data  # Remove data attribute

        with pytest.raises(InvalidDataError, match="Invalid JSON data"):
            provider._parse_shopify_json(mock_request)

    def test_parse_shopify_json_non_dict(self, provider):
        """Test parsing when JSON is not a dictionary"""
        mock_request = Mock()
        mock_request.data = b'"just a string"'

        with pytest.raises(InvalidDataError, match="Invalid JSON data"):
            provider._parse_shopify_json(mock_request)

    def test_parse_shopify_json_empty_dict(self, provider):
        """Test parsing empty dictionary"""
        mock_request = Mock()
        mock_request.data = b"{}"

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            provider._parse_shopify_json(mock_request)

    def test_parse_shopify_json_success(self, provider):
        """Test successful JSON parsing"""
        mock_request = Mock()
        test_data = {"test": "data"}
        mock_request.data = json.dumps(test_data).encode()

        result = provider._parse_shopify_json(mock_request)
        assert result == test_data

    def test_is_test_webhook_test_topic(self, provider):
        """Test test webhook detection with test topic"""
        mock_request = Mock()

        result = provider._is_test_webhook("test", mock_request)
        assert result is True

    def test_is_test_webhook_test_header(self, provider):
        """Test test webhook detection with test header"""
        mock_request = Mock()
        mock_request.headers = {"X-Shopify-Test": "true"}

        result = provider._is_test_webhook("orders/paid", mock_request)
        assert result is True

    def test_is_test_webhook_false(self, provider):
        """Test normal webhook detection"""
        mock_request = Mock()
        mock_request.headers = {}

        result = provider._is_test_webhook("orders/paid", mock_request)
        assert result is False

    def test_extract_shopify_customer_id_from_customer(self, provider):
        """Test customer ID extraction from customer field"""
        data = {"customer": {"id": 12345}}

        customer_id = provider._extract_shopify_customer_id(data)
        assert customer_id == "12345"

    def test_extract_shopify_customer_id_from_order_customer(self, provider):
        """Test customer ID extraction from order.customer field"""
        data = {"order": {"customer": {"id": 67890}}}

        customer_id = provider._extract_shopify_customer_id(data)
        assert customer_id == "67890"

    def test_extract_shopify_customer_id_from_id_field(self, provider):
        """Test customer ID extraction from id field"""
        data = {"id": 11111}

        customer_id = provider._extract_shopify_customer_id(data)
        assert customer_id == "11111"

    def test_extract_shopify_customer_id_missing_id(self, provider):
        """Test customer ID extraction when id is missing"""
        data = {"some_field": "value"}

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            provider._extract_shopify_customer_id(data)

    def test_extract_shopify_customer_id_none_id(self, provider):
        """Test customer ID extraction when id is None"""
        data = {"id": None}

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            provider._extract_shopify_customer_id(data)

    def test_extract_shopify_customer_id_empty_string(self, provider):
        """Test customer ID extraction when id is empty string"""
        data = {"id": ""}

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            provider._extract_shopify_customer_id(data)

    def test_extract_shopify_customer_id_key_error(self, provider):
        """Test customer ID extraction with KeyError"""
        data = {"customer": {"name": "test"}}  # Missing id field

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            provider._extract_shopify_customer_id(data)

    def test_extract_shopify_customer_id_normal_behavior(self, provider):
        """Test customer ID extraction normal behavior"""
        data = {"customer": {"id": 12345}}

        customer_id = provider._extract_shopify_customer_id(data)
        assert customer_id == "12345"

    def test_build_shopify_event_data_basic(self, provider):
        """Test basic event data building"""
        event_type = "payment_success"
        customer_id = "12345"
        data = {"id": 789012, "created_at": "2024-01-01T00:00:00Z"}
        topic = "orders/paid"

        result = provider._build_shopify_event_data(
            event_type, customer_id, data, topic
        )

        assert result["type"] == event_type
        assert result["customer_id"] == customer_id
        assert result["provider"] == "shopify"
        assert result["status"] == "success"
        assert result["created_at"] == "2024-01-01T00:00:00Z"

    def test_build_shopify_event_data_has_external_id(self, provider):
        """Test that event data includes external_id for deduplication."""
        data = {"id": 820982911946154508, "created_at": "2024-01-01T00:00:00Z"}

        result = provider._build_shopify_event_data(
            "order_created", "12345", data, "orders/create"
        )

        assert "external_id" in result
        assert result["external_id"] == "820982911946154508"

    def test_build_shopify_event_data_external_id_empty_when_missing(self, provider):
        """Test that external_id defaults to empty string when id is missing."""
        data = {"created_at": "2024-01-01T00:00:00Z"}

        result = provider._build_shopify_event_data(
            "order_created", "12345", data, "orders/create"
        )

        assert "external_id" in result
        assert result["external_id"] == ""

    def test_build_shopify_event_data_with_amount(self, provider):
        """Test event data building with amount"""
        data = {"total_price": "29.99", "created_at": "2024-01-01T00:00:00Z"}

        result = provider._build_shopify_event_data(
            "payment_success", "12345", data, "orders/paid"
        )
        assert result["amount"] == 29.99

    def test_build_shopify_event_data_invalid_amount_value_error(self, provider):
        """Test event data building with amount causing ValueError"""
        data = {"total_price": "invalid", "created_at": "2024-01-01T00:00:00Z"}

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            provider._build_shopify_event_data(
                "payment_success", "12345", data, "orders/paid"
            )

    def test_build_shopify_event_data_invalid_amount_type_error(self, provider):
        """Test event data building with amount causing TypeError"""
        data = {"total_price": None, "created_at": "2024-01-01T00:00:00Z"}

        with pytest.raises(InvalidDataError, match="Missing required fields"):
            provider._build_shopify_event_data(
                "payment_success", "12345", data, "orders/paid"
            )

    def test_build_shopify_event_data_with_currency(self, provider):
        """Test event data building with currency"""
        data = {"currency": "USD", "created_at": "2024-01-01T00:00:00Z"}

        result = provider._build_shopify_event_data(
            "payment_success", "12345", data, "orders/paid"
        )
        assert result["currency"] == "USD"

    def test_build_shopify_event_data_customer_update(self, provider):
        """Test event data building for customer update"""
        data = {
            "company": "Test Corp",
            "email": "test@example.com",
            "first_name": "John",
            "last_name": "Doe",
            "orders_count": 5,
            "total_spent": "150.00",
            "created_at": "2024-01-01T00:00:00Z",
        }

        result = provider._build_shopify_event_data(
            "customers/update", "12345", data, "customers/update"
        )

        assert "customer_data" in result
        assert result["customer_data"]["company"] == "Test Corp"
        assert result["customer_data"]["email"] == "test@example.com"
        assert result["customer_data"]["first_name"] == "John"
        assert result["customer_data"]["last_name"] == "Doe"
        assert result["customer_data"]["orders_count"] == 5
        assert result["customer_data"]["total_spent"] == "150.00"

    def test_parse_webhook_unsupported_topic(self, provider):
        """Test parsing webhook with unsupported topic"""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {"X-Shopify-Topic": "unsupported/topic"}
        mock_request.data = json.dumps({"id": 123}).encode()

        with pytest.raises(
            InvalidDataError, match="Unsupported webhook topic: unsupported/topic"
        ):
            provider.parse_webhook(mock_request)

    def test_parse_webhook_test_webhook_with_x_shopify_test_true(self, provider):
        """Test parsing test webhook with X-Shopify-Test header set to true"""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {
            "X-Shopify-Topic": "orders/paid",
            "X-Shopify-Test": "true",
        }
        mock_request.data = json.dumps({"test": True}).encode()

        result = provider.parse_webhook(mock_request)
        assert result is None

    def test_get_customer_data_no_webhook_data(self, provider):
        """Test get_customer_data when no webhook data is available"""
        with pytest.raises(CustomerNotFoundError, match="No webhook data available"):
            provider.get_customer_data("12345")

    def test_get_customer_data_with_customer_field(self, provider):
        """Test get_customer_data with customer field"""
        provider._current_webhook_data = {
            "customer": {
                "company": "Test Corp",
                "email": "test@example.com",
                "first_name": "John",
                "last_name": "Doe",
                "orders_count": 5,
                "total_spent": "150.00",
                "tags": ["vip", "enterprise"],
                "note": "Important customer",
            },
            "shop_domain": "test.myshopify.com",
        }

        result = provider.get_customer_data("12345")

        assert result["company"] == "Test Corp"
        assert result["email"] == "test@example.com"
        assert result["first_name"] == "John"
        assert result["last_name"] == "Doe"
        assert result["orders_count"] == 5
        assert result["total_spent"] == "150.00"
        assert result["metadata"]["shop_domain"] == "test.myshopify.com"
        assert result["metadata"]["tags"] == ["vip", "enterprise"]
        assert result["metadata"]["note"] == "Important customer"

    def test_get_customer_data_with_order_customer(self, provider):
        """Test get_customer_data with order.customer field"""
        provider._current_webhook_data = {
            "order": {
                "customer": {
                    "company": "Order Corp",
                    "email": "order@example.com",
                    "first_name": "Jane",
                    "last_name": "Smith",
                    "orders_count": 10,
                    "total_spent": "300.00",
                }
            }
        }

        result = provider.get_customer_data("12345")

        assert result["company"] == "Order Corp"
        assert result["email"] == "order@example.com"
        assert result["first_name"] == "Jane"
        assert result["last_name"] == "Smith"

    def test_get_customer_data_defaults(self, provider):
        """Test get_customer_data with missing fields using defaults"""
        provider._current_webhook_data = {"customer": {}}

        result = provider.get_customer_data("12345")

        assert result["company"] == "Individual"
        assert result["email"] == ""
        assert result["first_name"] == ""
        assert result["last_name"] == ""
        assert result["orders_count"] == 0
        assert result["total_spent"] == "0.00"

    def test_validate_webhook_missing_hmac(self, provider):
        """Test webhook validation with missing HMAC header"""
        mock_request = Mock()
        mock_request.headers = {}

        result = provider.validate_webhook(mock_request)
        assert result is False

    def test_validate_webhook_invalid_body_type(self, provider):
        """Test webhook validation with invalid body type"""
        mock_request = Mock()
        mock_request.headers = {"X-Shopify-Hmac-SHA256": "test_hmac"}
        mock_request.body = "not bytes"  # Should be bytes

        with pytest.raises(
            TypeError, match="Expected bytes or bytearray for request body"
        ):
            provider.validate_webhook(mock_request)

    @patch("hmac.compare_digest")
    def test_validate_webhook_success(self, mock_compare_digest, provider):
        """Test webhook validation using manual method"""
        mock_request = Mock()
        mock_request.headers = {"X-Shopify-Hmac-SHA256": "test_hmac"}
        mock_request.body = b'{"test": "data"}'

        mock_compare_digest.return_value = True

        result = provider.validate_webhook(mock_request)
        assert result is True

    def test_validate_webhook_calls_manual_method(self, provider):
        """Test that validate_webhook calls the manual validation method"""
        mock_request = Mock()
        mock_request.headers = {"X-Shopify-Hmac-SHA256": "test_hmac"}
        mock_request.body = b'{"test": "data"}'

        with patch.object(
            provider, "_manual_validate_webhook", return_value=True
        ) as mock_manual:
            result = provider.validate_webhook(mock_request)
            assert result is True
            mock_manual.assert_called_once_with(mock_request)

    def test_manual_validate_webhook_missing_hmac(self, provider):
        """Test manual webhook validation with missing HMAC header"""
        mock_request = Mock()
        mock_request.headers = {}

        result = provider._manual_validate_webhook(mock_request)
        assert result is False

    def test_manual_validate_webhook_with_string_secret(self, provider):
        """Test manual webhook validation with string secret"""
        provider.webhook_secret = "string_secret"
        mock_request = Mock()
        mock_request.headers = {"X-Shopify-Hmac-SHA256": "dGVzdF9oYXNo"}
        mock_request.body = b'{"test": "data"}'

        with patch("hmac.compare_digest", return_value=True):
            result = provider._manual_validate_webhook(mock_request)
            assert result is True


class TestShopifyAPI:
    """Comprehensive tests for ShopifyAPI service"""

    @patch("core.services.shopify.shopify.Session")
    @patch("core.services.shopify.shopify.Session.temp")
    @patch("core.services.shopify.shopify.Shop.current")
    def test_get_shop_domain_success(self, mock_shop_current, mock_temp, mock_session):
        """Test successful shop domain retrieval using official library"""
        # Mock session creation
        mock_session_instance = Mock()
        mock_session_instance.domain = "test.myshopify.com"
        mock_session_instance.api_version = "2024-01"
        mock_session_instance.token = "test_token"
        mock_session.return_value = mock_session_instance

        # Mock shop object
        mock_shop = Mock()
        mock_shop.myshopify_domain = "test.myshopify.com"
        mock_shop_current.return_value = mock_shop

        result = ShopifyAPI.get_shop_domain("test.myshopify.com", "test_token")

        assert result == "test.myshopify.com"

    @patch("core.services.shopify.shopify.Session")
    def test_get_session_failure(self, mock_session):
        """Test session creation failure"""
        mock_session.side_effect = Exception("Session creation failed")

        with pytest.raises(Exception, match="Session creation failed"):
            ShopifyAPI._get_session("test.myshopify.com", "test_token")

    @patch("core.services.shopify.ShopifyAPI._get_session")
    @patch("core.services.shopify.shopify.Session.temp")
    def test_get_shop_domain_exception(self, mock_temp, mock_get_session):
        """Test shop domain retrieval with exception"""
        mock_get_session.side_effect = Exception("API error")

        result = ShopifyAPI.get_shop_domain("test.myshopify.com", "test_token")

        assert result is None

    def test_shopify_api_exists(self):
        """Test that ShopifyAPI class exists and can be instantiated"""
        api = ShopifyAPI()
        assert api is not None


class TestLogisticsEventTypes:
    """Tests for new logistics/fulfillment event type mappings."""

    @pytest.fixture
    def provider(self) -> ShopifySourcePlugin:
        """Create a ShopifySourcePlugin instance for testing."""
        return ShopifySourcePlugin(webhook_secret="test_secret")

    def test_event_type_mapping_orders_create(self, provider: ShopifySourcePlugin):
        """Test that orders/create maps to order_created."""
        assert provider.EVENT_TYPE_MAPPING["orders/create"] == "order_created"

    def test_event_type_mapping_orders_fulfilled(self, provider: ShopifySourcePlugin):
        """Test that orders/fulfilled maps to order_fulfilled."""
        assert provider.EVENT_TYPE_MAPPING["orders/fulfilled"] == "order_fulfilled"

    def test_event_type_mapping_fulfillments_create(
        self, provider: ShopifySourcePlugin
    ):
        """Test that fulfillments/create maps to fulfillment_created."""
        assert (
            provider.EVENT_TYPE_MAPPING["fulfillments/create"] == "fulfillment_created"
        )

    def test_event_type_mapping_fulfillments_update(
        self, provider: ShopifySourcePlugin
    ):
        """Test that fulfillments/update maps to fulfillment_updated."""
        assert (
            provider.EVENT_TYPE_MAPPING["fulfillments/update"] == "fulfillment_updated"
        )

    def test_event_type_mapping_customers_update(self, provider: ShopifySourcePlugin):
        """Test that customers/update maps to customer_updated."""
        assert provider.EVENT_TYPE_MAPPING["customers/update"] == "customer_updated"

    def test_fulfillment_topics_set(self, provider: ShopifySourcePlugin):
        """Test that fulfillment topics are correctly defined."""
        assert "fulfillments/create" in provider.FULFILLMENT_TOPICS
        assert "fulfillments/update" in provider.FULFILLMENT_TOPICS
        assert len(provider.FULFILLMENT_TOPICS) == 2


class TestFulfillmentWebhookParsing:
    """Tests for fulfillment webhook parsing."""

    @pytest.fixture
    def provider(self) -> ShopifySourcePlugin:
        """Create a ShopifySourcePlugin instance for testing."""
        return ShopifySourcePlugin(webhook_secret="test_secret")

    def test_build_fulfillment_event_data_basic(self, provider: ShopifySourcePlugin):
        """Test building event data from fulfillment webhook."""
        data = {
            "id": 123456,
            "order_id": 789,
            "order_number": "1001",
            "status": "success",
            "tracking_number": "1Z999AA10123456784",
            "tracking_company": "UPS",
            "tracking_url": "https://ups.com/track/1Z999AA10123456784",
            "created_at": "2025-01-24T10:00:00Z",
            "line_items": [{"name": "Test Product", "sku": "SKU123", "quantity": 2}],
        }

        result = provider._build_fulfillment_event_data(
            "fulfillment_created", "customer_123", data, "fulfillments/create"
        )

        assert result["type"] == "fulfillment_created"
        assert result["customer_id"] == "customer_123"
        assert result["provider"] == "shopify"
        assert result["metadata"]["tracking_number"] == "1Z999AA10123456784"
        assert result["metadata"]["tracking_company"] == "UPS"
        assert result["metadata"]["order_number"] == "1001"
        assert result["metadata"]["fulfillment_status"] == "success"
        assert len(result["metadata"]["line_items"]) == 1

    def test_build_fulfillment_event_data_has_external_id(
        self, provider: ShopifySourcePlugin
    ):
        """Test that fulfillment event data includes external_id for deduplication."""
        data = {
            "id": 4567890123,
            "order_id": 789,
            "status": "success",
            "created_at": "2025-01-24T10:00:00Z",
        }

        result = provider._build_fulfillment_event_data(
            "fulfillment_created", "customer_123", data, "fulfillments/create"
        )

        assert "external_id" in result
        assert result["external_id"] == "4567890123"

    def test_extract_customer_id_from_fulfillment_with_customer(
        self, provider: ShopifySourcePlugin
    ):
        """Test extracting customer ID when customer field is present."""
        data = {"customer": {"id": 12345}}
        result = provider._extract_customer_id_from_fulfillment(data)
        assert result == "12345"

    def test_extract_customer_id_from_fulfillment_with_destination(
        self, provider: ShopifySourcePlugin
    ):
        """Test extracting customer ID from destination email."""
        data = {"destination": {"email": "customer@example.com"}}
        result = provider._extract_customer_id_from_fulfillment(data)
        assert result == "customer@example.com"

    def test_extract_customer_id_from_fulfillment_with_order_id(
        self, provider: ShopifySourcePlugin
    ):
        """Test extracting customer ID from order_id fallback."""
        data = {"order_id": 789}
        result = provider._extract_customer_id_from_fulfillment(data)
        assert result == "order_789"

    def test_extract_customer_id_from_fulfillment_with_fulfillment_id(
        self, provider: ShopifySourcePlugin
    ):
        """Test extracting customer ID from fulfillment ID as last resort."""
        data = {"id": 123456}
        result = provider._extract_customer_id_from_fulfillment(data)
        assert result == "fulfillment_123456"

    def test_extract_customer_id_from_fulfillment_no_identifier(
        self, provider: ShopifySourcePlugin
    ):
        """Test that an error is raised when no identifier is found."""
        data = {}
        with pytest.raises(
            InvalidDataError, match="Cannot extract customer identifier"
        ):
            provider._extract_customer_id_from_fulfillment(data)

    def test_parse_webhook_fulfillment_create(self, provider: ShopifySourcePlugin):
        """Test parsing a fulfillments/create webhook."""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {
            "X-Shopify-Topic": "fulfillments/create",
            "X-Shopify-Test": "false",
        }
        mock_request.body = json.dumps(
            {
                "id": 123456,
                "order_id": 789,
                "order_number": "1001",
                "status": "success",
                "tracking_number": "1Z999AA10123456784",
                "customer": {"id": 12345},
            }
        ).encode()
        mock_request.data = mock_request.body

        result = provider.parse_webhook(mock_request)

        assert result is not None
        assert result["type"] == "fulfillment_created"
        assert result["customer_id"] == "12345"
        assert result["metadata"]["tracking_number"] == "1Z999AA10123456784"

    def test_parse_webhook_order_fulfilled(self, provider: ShopifySourcePlugin):
        """Test parsing an orders/fulfilled webhook."""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {
            "X-Shopify-Topic": "orders/fulfilled",
            "X-Shopify-Test": "false",
        }
        mock_request.body = json.dumps(
            {
                "id": 789,
                "order_number": "1001",
                "customer": {"id": 12345},
                "total_price": "99.99",
                "currency": "USD",
                "fulfillment_status": "fulfilled",
            }
        ).encode()
        mock_request.data = mock_request.body

        result = provider.parse_webhook(mock_request)

        assert result is not None
        assert result["type"] == "order_fulfilled"
        assert result["customer_id"] == "12345"

    def test_parse_webhook_order_created(self, provider: ShopifySourcePlugin):
        """Test parsing an orders/create webhook."""
        mock_request = Mock()
        mock_request.content_type = "application/json"
        mock_request.headers = {
            "X-Shopify-Topic": "orders/create",
            "X-Shopify-Test": "false",
        }
        mock_request.body = json.dumps(
            {
                "id": 789,
                "order_number": "1001",
                "customer": {"id": 12345},
                "total_price": "149.99",
                "currency": "USD",
            }
        ).encode()
        mock_request.data = mock_request.body

        result = provider.parse_webhook(mock_request)

        assert result is not None
        assert result["type"] == "order_created"
        assert result["amount"] == 149.99


class TestDomainNormalization:
    """Tests for shop domain normalization."""

    def test_normalize_store_name_only(self):
        """Test normalizing just a store name."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("mystore")
        assert error is None
        assert domain == "mystore.myshopify.com"

    def test_normalize_full_myshopify_url(self):
        """Test normalizing full myshopify.com URL."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("mystore.myshopify.com")
        assert error is None
        assert domain == "mystore.myshopify.com"

    def test_normalize_https_url(self):
        """Test normalizing URL with https prefix."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("https://mystore.myshopify.com")
        assert error is None
        assert domain == "mystore.myshopify.com"

    def test_normalize_url_with_path(self):
        """Test normalizing URL with path."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("mystore.myshopify.com/admin")
        assert error is None
        assert domain == "mystore.myshopify.com"

    def test_reject_custom_domain(self):
        """Test that custom domains are rejected with helpful error."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("shop.mybusiness.com")
        assert domain is None
        assert error is not None
        assert "Custom domains are not supported" in error
        expected_domain_hint = "myshopify.com"
        assert expected_domain_hint in error

    def test_reject_empty_input(self):
        """Test that empty input returns an error."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("")
        assert domain is None
        assert error is not None
        assert "enter your Shopify store URL" in error

    def test_normalize_with_hyphens(self):
        """Test normalizing store name with hyphens."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("my-store-name")
        assert error is None
        assert domain == "my-store-name.myshopify.com"

    def test_normalize_with_underscores(self):
        """Test normalizing store name with underscores."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("my_store_name")
        assert error is None
        assert domain == "my_store_name.myshopify.com"

    def test_normalize_uppercase_converted(self):
        """Test that uppercase is converted to lowercase."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("MyStore")
        assert error is None
        assert domain == "mystore.myshopify.com"

    def test_invalid_characters_rejected(self):
        """Test that invalid characters are rejected."""
        from core.views.integrations.shopify import _normalize_shop_domain

        domain, error = _normalize_shop_domain("my store!")
        assert domain is None
        assert error is not None


class TestEventCategoriesConfig:
    """Tests for event categories configuration."""

    def test_event_categories_defined(self):
        """Test that event categories are properly defined."""
        from core.views.integrations.shopify import SHOPIFY_EVENT_CATEGORIES

        assert "orders" in SHOPIFY_EVENT_CATEGORIES
        assert "fulfillment" in SHOPIFY_EVENT_CATEGORIES
        assert "customers" in SHOPIFY_EVENT_CATEGORIES

    def test_orders_category_topics(self):
        """Test that orders category has correct topics."""
        from core.views.integrations.shopify import SHOPIFY_EVENT_CATEGORIES

        orders = SHOPIFY_EVENT_CATEGORIES["orders"]
        assert "orders/create" in orders["topics"]
        assert "orders/paid" in orders["topics"]
        assert "orders/cancelled" in orders["topics"]
        assert orders["default"] is True

    def test_fulfillment_category_topics(self):
        """Test that fulfillment category has correct topics."""
        from core.views.integrations.shopify import SHOPIFY_EVENT_CATEGORIES

        fulfillment = SHOPIFY_EVENT_CATEGORIES["fulfillment"]
        assert "orders/fulfilled" in fulfillment["topics"]
        assert "fulfillments/create" in fulfillment["topics"]
        assert "fulfillments/update" in fulfillment["topics"]
        assert fulfillment["default"] is True

    def test_customers_category_topics(self):
        """Test that customers category has correct topics."""
        from core.views.integrations.shopify import SHOPIFY_EVENT_CATEGORIES

        customers = SHOPIFY_EVENT_CATEGORIES["customers"]
        assert "customers/update" in customers["topics"]
        assert customers["default"] is True

    def test_get_topics_for_categories(self):
        """Test getting topics for selected categories."""
        from core.views.integrations.shopify import _get_topics_for_categories

        topics = _get_topics_for_categories(["orders", "customers"])
        assert "orders/create" in topics
        assert "orders/paid" in topics
        assert "customers/update" in topics
        # Should NOT include fulfillment topics
        assert "fulfillments/create" not in topics

    def test_get_topics_for_empty_categories(self):
        """Test getting topics for empty category list."""
        from core.views.integrations.shopify import _get_topics_for_categories

        topics = _get_topics_for_categories([])
        assert topics == []

    def test_get_default_categories(self):
        """Test getting default enabled categories."""
        from core.views.integrations.shopify import _get_default_categories

        defaults = _get_default_categories()
        assert "orders" in defaults
        assert "fulfillment" in defaults
        assert "customers" in defaults

    def test_get_topics_for_invalid_categories(self):
        """Test that invalid categories are ignored."""
        from core.views.integrations.shopify import _get_topics_for_categories

        topics = _get_topics_for_categories(["orders", "invalid_category", "malicious"])
        # Should only include topics from valid 'orders' category
        assert "orders/create" in topics
        assert "orders/paid" in topics
        assert len(topics) == 3  # Only the 3 order topics

    def test_category_validation_filters_invalid_keys(self):
        """Test that category validation filters out invalid keys."""
        from core.views.integrations.shopify import SHOPIFY_EVENT_CATEGORIES

        # Simulate validation logic used in views
        raw_categories = ["orders", "invalid", "fulfillment", "malicious_input"]
        valid_keys = set(SHOPIFY_EVENT_CATEGORIES.keys())
        validated = [c for c in raw_categories if c in valid_keys]

        assert validated == ["orders", "fulfillment"]
        assert "invalid" not in validated
        assert "malicious_input" not in validated

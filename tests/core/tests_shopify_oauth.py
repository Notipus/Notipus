"""
Tests for Shopify OAuth integration views.

Tests cover:
- OAuth flow initiation (shopify_connect)
- OAuth callback handling (shopify_connect_callback)
- Disconnection (disconnect_shopify)
- Helper functions (_normalize_shop_domain, _is_valid_shop_domain, etc.)
"""

from unittest.mock import Mock, patch

import pytest
import requests
from core.models import Integration, UserProfile, Workspace
from core.views.integrations.shopify import (
    _is_valid_shop_domain,
    _normalize_shop_domain,
)
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse


class TestShopifyOAuthHelpers(TestCase):
    """Test helper functions for Shopify OAuth."""

    def test_normalize_shop_domain_simple_name(self) -> None:
        """Test normalizing a simple shop name."""
        result = _normalize_shop_domain("mystore")
        assert result == "mystore.myshopify.com"

    def test_normalize_shop_domain_with_suffix(self) -> None:
        """Test normalizing a shop URL with myshopify.com suffix."""
        result = _normalize_shop_domain("mystore.myshopify.com")
        assert result == "mystore.myshopify.com"

    def test_normalize_shop_domain_with_https(self) -> None:
        """Test normalizing a full HTTPS URL."""
        result = _normalize_shop_domain("https://mystore.myshopify.com")
        assert result == "mystore.myshopify.com"

    def test_normalize_shop_domain_with_http(self) -> None:
        """Test normalizing a full HTTP URL."""
        result = _normalize_shop_domain("http://mystore.myshopify.com")
        assert result == "mystore.myshopify.com"

    def test_normalize_shop_domain_with_path(self) -> None:
        """Test normalizing a URL with path."""
        result = _normalize_shop_domain("mystore.myshopify.com/admin")
        assert result == "mystore.myshopify.com"

    def test_normalize_shop_domain_uppercase(self) -> None:
        """Test normalizing uppercase shop name."""
        result = _normalize_shop_domain("MyStore")
        assert result == "mystore.myshopify.com"

    def test_normalize_shop_domain_with_hyphens(self) -> None:
        """Test normalizing shop name with hyphens."""
        result = _normalize_shop_domain("my-store")
        assert result == "my-store.myshopify.com"

    def test_normalize_shop_domain_with_underscores(self) -> None:
        """Test normalizing shop name with underscores."""
        result = _normalize_shop_domain("my_store")
        assert result == "my_store.myshopify.com"

    def test_normalize_shop_domain_invalid_empty(self) -> None:
        """Test normalizing empty shop URL returns None."""
        result = _normalize_shop_domain("")
        assert result is None

    def test_normalize_shop_domain_invalid_special_chars(self) -> None:
        """Test normalizing shop URL with invalid special characters."""
        result = _normalize_shop_domain("my@store!")
        assert result is None

    def test_is_valid_shop_domain_valid(self) -> None:
        """Test validation of valid shop domain."""
        assert _is_valid_shop_domain("mystore.myshopify.com") is True
        assert _is_valid_shop_domain("my-store.myshopify.com") is True
        assert _is_valid_shop_domain("my_store.myshopify.com") is True
        assert _is_valid_shop_domain("store123.myshopify.com") is True

    def test_is_valid_shop_domain_invalid(self) -> None:
        """Test validation of invalid shop domains."""
        assert _is_valid_shop_domain("mystore.otherdomain.com") is False
        assert _is_valid_shop_domain("mystore") is False
        assert _is_valid_shop_domain(".myshopify.com") is False
        assert _is_valid_shop_domain("-mystore.myshopify.com") is False
        assert _is_valid_shop_domain("my store.myshopify.com") is False


@pytest.mark.django_db
class TestIntegrateShopifyView:
    """Test the integrate_shopify view."""

    @pytest.fixture
    def setup_user(self, client: Client) -> tuple[User, Workspace, UserProfile]:
        """Set up test user, workspace, and profile."""
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="test@example.com",
        )
        workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )
        user_profile = UserProfile.objects.create(
            user=user,
            workspace=workspace,
        )
        return user, workspace, user_profile

    def test_integrate_shopify_requires_login(self, client: Client) -> None:
        """Test that integrate_shopify requires authentication."""
        response = client.get(reverse("core:integrate_shopify"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    @override_settings(SHOPIFY_CLIENT_ID="test_client_id")
    def test_integrate_shopify_not_connected(
        self, client: Client, setup_user: tuple
    ) -> None:
        """Test integrate_shopify shows connect form when not connected."""
        user, workspace, _ = setup_user
        client.force_login(user)

        response = client.get(reverse("core:integrate_shopify"))

        assert response.status_code == 200
        assert b"Connect Your Store" in response.content
        assert b"shop_url" in response.content

    @override_settings(SHOPIFY_CLIENT_ID="test_client_id")
    def test_integrate_shopify_already_connected(
        self, client: Client, setup_user: tuple
    ) -> None:
        """Test integrate_shopify shows connected state when already connected."""
        user, workspace, _ = setup_user
        client.force_login(user)

        # Create existing integration
        Integration.objects.create(
            workspace=workspace,
            integration_type="shopify",
            oauth_credentials={"access_token": "test_token"},
            integration_settings={"shop_domain": "teststore.myshopify.com"},
            is_active=True,
        )

        response = client.get(reverse("core:integrate_shopify"))

        assert response.status_code == 200
        assert b"Connected" in response.content
        expected_domain = b"teststore.myshopify.com"
        assert expected_domain in response.content


@pytest.mark.django_db
class TestShopifyConnectView:
    """Test the shopify_connect view."""

    @pytest.fixture
    def setup_user(self, client: Client) -> tuple[User, Workspace, UserProfile]:
        """Set up test user, workspace, and profile."""
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="test@example.com",
        )
        workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )
        user_profile = UserProfile.objects.create(
            user=user,
            workspace=workspace,
        )
        return user, workspace, user_profile

    def test_shopify_connect_requires_post(
        self, client: Client, setup_user: tuple
    ) -> None:
        """Test that shopify_connect requires POST method."""
        user, _, _ = setup_user
        client.force_login(user)

        response = client.get(reverse("core:shopify_connect"))

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")

    def test_shopify_connect_requires_login(self, client: Client) -> None:
        """Test that shopify_connect requires authentication."""
        response = client.post(reverse("core:shopify_connect"), {"shop_url": "test"})
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    @override_settings(SHOPIFY_CLIENT_ID="")
    def test_shopify_connect_not_configured(
        self, client: Client, setup_user: tuple
    ) -> None:
        """Test shopify_connect when Shopify is not configured."""
        user, _, _ = setup_user
        client.force_login(user)

        response = client.post(
            reverse("core:shopify_connect"), {"shop_url": "teststore"}
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        messages = list(get_messages(response.wsgi_request))
        assert any("not configured" in str(m).lower() for m in messages)

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_SCOPES="read_orders,read_customers",
        SHOPIFY_REDIRECT_URI="http://localhost/callback/",
    )
    def test_shopify_connect_missing_shop_url(
        self, client: Client, setup_user: tuple
    ) -> None:
        """Test shopify_connect with missing shop URL."""
        user, _, _ = setup_user
        client.force_login(user)

        response = client.post(reverse("core:shopify_connect"), {"shop_url": ""})

        assert response.status_code == 302
        assert response.url == reverse("core:integrate_shopify")
        messages = list(get_messages(response.wsgi_request))
        assert any("enter your shopify" in str(m).lower() for m in messages)

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_SCOPES="read_orders,read_customers",
        SHOPIFY_REDIRECT_URI="http://localhost/callback/",
    )
    def test_shopify_connect_invalid_shop_url(
        self, client: Client, setup_user: tuple
    ) -> None:
        """Test shopify_connect with invalid shop URL."""
        user, _, _ = setup_user
        client.force_login(user)

        response = client.post(
            reverse("core:shopify_connect"), {"shop_url": "invalid@shop!"}
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrate_shopify")
        messages = list(get_messages(response.wsgi_request))
        assert any("invalid" in str(m).lower() for m in messages)

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_SCOPES="read_orders,read_customers",
        SHOPIFY_REDIRECT_URI="http://localhost/callback/",
    )
    def test_shopify_connect_redirects_to_oauth(
        self, client: Client, setup_user: tuple
    ) -> None:
        """Test shopify_connect redirects to Shopify OAuth."""
        user, _, _ = setup_user
        client.force_login(user)

        response = client.post(
            reverse("core:shopify_connect"), {"shop_url": "teststore"}
        )

        assert response.status_code == 302
        assert "teststore.myshopify.com/admin/oauth/authorize" in response.url
        assert "client_id=test_client_id" in response.url
        assert "scope=read_orders" in response.url

        # Verify session state was set
        session = client.session
        assert "shopify_oauth_state" in session
        assert session["shopify_shop_domain"] == "teststore.myshopify.com"


@pytest.mark.django_db
class TestShopifyConnectCallbackView:
    """Test the shopify_connect_callback view."""

    @pytest.fixture
    def setup_user(self, client: Client) -> tuple[User, Workspace, UserProfile]:
        """Set up test user, workspace, and profile."""
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="test@example.com",
        )
        workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )
        user_profile = UserProfile.objects.create(
            user=user,
            workspace=workspace,
        )
        return user, workspace, user_profile

    def test_callback_with_error(self, client: Client, setup_user: tuple) -> None:
        """Test callback handles OAuth errors."""
        user, _, _ = setup_user
        client.force_login(user)

        response = client.get(
            reverse("core:shopify_connect_callback"),
            {"error": "access_denied", "error_description": "User denied access"},
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        messages = list(get_messages(response.wsgi_request))
        assert any("denied" in str(m).lower() for m in messages)

    def test_callback_missing_params(self, client: Client, setup_user: tuple) -> None:
        """Test callback handles missing parameters."""
        user, _, _ = setup_user
        client.force_login(user)

        response = client.get(reverse("core:shopify_connect_callback"))

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        messages = list(get_messages(response.wsgi_request))
        assert any("missing" in str(m).lower() for m in messages)

    def test_callback_state_mismatch(self, client: Client, setup_user: tuple) -> None:
        """Test callback handles state mismatch."""
        user, _, _ = setup_user
        client.force_login(user)

        # Set up session with different state
        session = client.session
        session["shopify_oauth_state"] = "original_state"
        session["shopify_shop_domain"] = "teststore.myshopify.com"
        session.save()

        response = client.get(
            reverse("core:shopify_connect_callback"),
            {
                "code": "test_code",
                "state": "different_state",
                "shop": "teststore.myshopify.com",
            },
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        messages = list(get_messages(response.wsgi_request))
        assert any("state" in str(m).lower() for m in messages)

    def test_callback_shop_mismatch(self, client: Client, setup_user: tuple) -> None:
        """Test callback handles shop mismatch."""
        user, _, _ = setup_user
        client.force_login(user)

        # Set up session
        session = client.session
        session["shopify_oauth_state"] = "test_state"
        session["shopify_shop_domain"] = "original-store.myshopify.com"
        session.save()

        response = client.get(
            reverse("core:shopify_connect_callback"),
            {
                "code": "test_code",
                "state": "test_state",
                "shop": "different-store.myshopify.com",
            },
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        messages = list(get_messages(response.wsgi_request))
        assert any("mismatch" in str(m).lower() for m in messages)

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_CLIENT_SECRET="test_secret",
        SHOPIFY_API_VERSION="2025-01",
        BASE_URL="http://localhost:8000",
    )
    @patch("core.views.integrations.shopify.requests.post")
    def test_callback_token_exchange_success(
        self, mock_post: Mock, client: Client, setup_user: tuple
    ) -> None:
        """Test successful token exchange in callback."""
        user, workspace, _ = setup_user
        client.force_login(user)

        # Set up session
        session = client.session
        session["shopify_oauth_state"] = "test_state"
        session["shopify_shop_domain"] = "teststore.myshopify.com"
        session.save()

        # Mock token exchange response
        token_response = Mock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "test_access_token",
            "scope": "read_orders,read_customers",
        }
        token_response.raise_for_status = Mock()

        # Mock webhook creation response
        webhook_response = Mock()
        webhook_response.status_code = 201
        webhook_response.json.return_value = {"webhook": {"id": 12345}}

        mock_post.side_effect = [token_response] + [webhook_response] * 5

        response = client.get(
            reverse("core:shopify_connect_callback"),
            {
                "code": "test_code",
                "state": "test_state",
                "shop": "teststore.myshopify.com",
            },
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")

        # Verify integration was created
        integration = Integration.objects.filter(
            workspace=workspace,
            integration_type="shopify",
            is_active=True,
        ).first()
        assert integration is not None
        assert integration.oauth_credentials["access_token"] == "test_access_token"
        assert (
            integration.integration_settings["shop_domain"] == "teststore.myshopify.com"
        )

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_CLIENT_SECRET="test_secret",
    )
    @patch("core.views.integrations.shopify.requests.post")
    def test_callback_token_exchange_failure(
        self, mock_post: Mock, client: Client, setup_user: tuple
    ) -> None:
        """Test token exchange failure in callback."""
        user, _, _ = setup_user
        client.force_login(user)

        # Set up session
        session = client.session
        session["shopify_oauth_state"] = "test_state"
        session["shopify_shop_domain"] = "teststore.myshopify.com"
        session.save()

        # Mock failed token exchange
        mock_post.side_effect = requests.exceptions.RequestException("Connection error")

        response = client.get(
            reverse("core:shopify_connect_callback"),
            {
                "code": "test_code",
                "state": "test_state",
                "shop": "teststore.myshopify.com",
            },
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")
        messages = list(get_messages(response.wsgi_request))
        assert any("failed" in str(m).lower() for m in messages)


@pytest.mark.django_db
class TestDisconnectShopifyView:
    """Test the disconnect_shopify view."""

    @pytest.fixture
    def setup_user_with_integration(
        self, client: Client
    ) -> tuple[User, Workspace, UserProfile, Integration]:
        """Set up test user with Shopify integration."""
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="test@example.com",
        )
        workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )
        user_profile = UserProfile.objects.create(
            user=user,
            workspace=workspace,
        )
        integration = Integration.objects.create(
            workspace=workspace,
            integration_type="shopify",
            oauth_credentials={"access_token": "test_token"},
            integration_settings={
                "shop_domain": "teststore.myshopify.com",
                "webhook_ids": [12345, 67890],
            },
            is_active=True,
        )
        return user, workspace, user_profile, integration

    def test_disconnect_requires_post(
        self, client: Client, setup_user_with_integration: tuple
    ) -> None:
        """Test that disconnect requires POST method."""
        user, _, _, _ = setup_user_with_integration
        client.force_login(user)

        response = client.get(reverse("core:disconnect_shopify"))

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")

    def test_disconnect_requires_login(self, client: Client) -> None:
        """Test that disconnect requires authentication."""
        response = client.post(reverse("core:disconnect_shopify"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_disconnect_no_integration(self, client: Client) -> None:
        """Test disconnect when no integration exists."""
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
        )
        workspace = Workspace.objects.create(name="Test Org")
        UserProfile.objects.create(user=user, workspace=workspace)
        client.force_login(user)

        response = client.post(reverse("core:disconnect_shopify"))

        assert response.status_code == 302
        messages = list(get_messages(response.wsgi_request))
        assert any("no active" in str(m).lower() for m in messages)

    @override_settings(SHOPIFY_API_VERSION="2025-01")
    @patch("core.views.integrations.shopify.requests.delete")
    def test_disconnect_success(
        self, mock_delete: Mock, client: Client, setup_user_with_integration: tuple
    ) -> None:
        """Test successful disconnection."""
        user, workspace, _, integration = setup_user_with_integration
        client.force_login(user)

        # Mock webhook deletion
        mock_delete.return_value = Mock(status_code=200)

        response = client.post(reverse("core:disconnect_shopify"))

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")

        # Verify integration was deactivated
        integration.refresh_from_db()
        assert integration.is_active is False

        # Verify webhooks were deleted
        assert mock_delete.call_count == 2  # Two webhook IDs

        messages = list(get_messages(response.wsgi_request))
        assert any("disconnected" in str(m).lower() for m in messages)

    @override_settings(SHOPIFY_API_VERSION="2025-01")
    @patch("core.views.integrations.shopify.requests.delete")
    def test_disconnect_webhook_deletion_failure(
        self, mock_delete: Mock, client: Client, setup_user_with_integration: tuple
    ) -> None:
        """Test disconnection succeeds even if webhook deletion fails."""
        user, workspace, _, integration = setup_user_with_integration
        client.force_login(user)

        # Mock webhook deletion failure
        mock_delete.side_effect = requests.exceptions.RequestException("API error")

        response = client.post(reverse("core:disconnect_shopify"))

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")

        # Verify integration was still deactivated
        integration.refresh_from_db()
        assert integration.is_active is False


@pytest.mark.django_db
class TestWebhookCreation:
    """Test webhook creation functionality."""

    @pytest.fixture
    def setup_user(self, client: Client) -> tuple[User, Workspace, UserProfile]:
        """Set up test user, workspace, and profile."""
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="test@example.com",
        )
        workspace = Workspace.objects.create(
            name="Test Workspace",
            shop_domain="test.myshopify.com",
        )
        user_profile = UserProfile.objects.create(
            user=user,
            workspace=workspace,
        )
        return user, workspace, user_profile

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_CLIENT_SECRET="test_secret",
        SHOPIFY_API_VERSION="2025-01",
        BASE_URL="http://localhost:8000",
    )
    @patch("core.views.integrations.shopify.requests.post")
    def test_webhook_creation_all_topics(
        self, mock_post: Mock, client: Client, setup_user: tuple
    ) -> None:
        """Test that all webhook topics are created."""
        user, workspace, _ = setup_user
        client.force_login(user)

        # Set up session
        session = client.session
        session["shopify_oauth_state"] = "test_state"
        session["shopify_shop_domain"] = "teststore.myshopify.com"
        session.save()

        # Mock responses
        token_response = Mock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "test_access_token",
            "scope": "read_orders,read_customers,write_webhooks",
        }
        token_response.raise_for_status = Mock()

        webhook_response = Mock()
        webhook_response.status_code = 201
        webhook_response.json.return_value = {"webhook": {"id": 12345}}

        # Token exchange + 5 webhook creations
        mock_post.side_effect = [token_response] + [webhook_response] * 5

        response = client.get(
            reverse("core:shopify_connect_callback"),
            {
                "code": "test_code",
                "state": "test_state",
                "shop": "teststore.myshopify.com",
            },
        )

        assert response.status_code == 302

        # Verify all 5 webhook topics were requested
        # First call is token exchange, next 5 are webhooks
        webhook_calls = mock_post.call_args_list[1:]
        assert len(webhook_calls) == 5

        # Verify webhook URL format
        for call in webhook_calls:
            call_kwargs = call[1] if len(call) > 1 else {}
            call_json = call_kwargs.get("json", {})
            webhook_data = call_json.get("webhook", {})
            assert str(workspace.uuid) in webhook_data.get("address", "")

    @override_settings(
        SHOPIFY_CLIENT_ID="test_client_id",
        SHOPIFY_CLIENT_SECRET="test_secret",
        SHOPIFY_API_VERSION="2025-01",
        BASE_URL="http://localhost:8000",
    )
    @patch("core.views.integrations.shopify.requests.post")
    def test_webhook_creation_handles_existing(
        self, mock_post: Mock, client: Client, setup_user: tuple
    ) -> None:
        """Test that existing webhooks (422) are handled gracefully."""
        user, workspace, _ = setup_user
        client.force_login(user)

        # Set up session
        session = client.session
        session["shopify_oauth_state"] = "test_state"
        session["shopify_shop_domain"] = "teststore.myshopify.com"
        session.save()

        # Mock responses
        token_response = Mock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "test_access_token",
            "scope": "read_orders,read_customers,write_webhooks",
        }
        token_response.raise_for_status = Mock()

        # Mix of success and "already exists" responses
        success_response = Mock()
        success_response.status_code = 201
        success_response.json.return_value = {"webhook": {"id": 12345}}

        exists_response = Mock()
        exists_response.status_code = 422
        exists_response.text = "Webhook already exists"

        mock_post.side_effect = [
            token_response,
            success_response,
            exists_response,
            success_response,
            exists_response,
            success_response,
        ]

        response = client.get(
            reverse("core:shopify_connect_callback"),
            {
                "code": "test_code",
                "state": "test_state",
                "shop": "teststore.myshopify.com",
            },
        )

        assert response.status_code == 302
        assert response.url == reverse("core:integrations")

        # Integration should still be created
        integration = Integration.objects.get(
            workspace=workspace,
            integration_type="shopify",
        )
        assert integration.is_active is True
        # Should have 3 successful webhook IDs
        assert len(integration.integration_settings.get("webhook_ids", [])) == 3

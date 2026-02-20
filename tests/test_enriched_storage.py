"""Tests for enriched webhook record storage.

This module tests the store_enriched_record method in DatabaseLookupService
that stores RichNotification data for dashboard display.
"""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from webhooks.models.rich_notification import (
    CompanyInfo,
    CustomerInfo,
    InsightInfo,
    NotificationSeverity,
    NotificationType,
    PaymentInfo,
    RichNotification,
)
from webhooks.services.database_lookup import DatabaseLookupService


@pytest.fixture
def db_service() -> DatabaseLookupService:
    """Create a fresh DatabaseLookupService instance.

    Returns:
        DatabaseLookupService instance for testing.
    """
    return DatabaseLookupService()


@pytest.fixture
def workspace_id() -> str:
    """Create a sample workspace ID for testing.

    Returns:
        A workspace UUID string.
    """
    return "ws-uuid-test-1234"


@pytest.fixture
def sample_event_data() -> dict:
    """Create sample event data for testing.

    Returns:
        Dictionary with sample event data.
    """
    return {
        "type": "payment_success",
        "provider": "stripe",
        "external_id": "pi_test123",
        "customer_id": "cus_test456",
        "amount": 299.00,
        "currency": "USD",
        "status": "succeeded",
        "metadata": {
            "plan_name": "Pro Plan",
            "subscription_id": "sub_test789",
        },
    }


@pytest.fixture
def sample_notification() -> RichNotification:
    """Create a sample RichNotification for testing.

    Returns:
        RichNotification instance with enriched data.
    """
    return RichNotification(
        type=NotificationType.PAYMENT_SUCCESS,
        severity=NotificationSeverity.SUCCESS,
        headline="$299.00 from Acme Corp",
        headline_icon="money",
        provider="stripe",
        provider_display="Stripe",
        customer=CustomerInfo(
            email="billing@acme.com",
            name="John Doe",
            company_name="Acme Corp",
            tenure_display="Since Mar 2024",
            ltv_display="$2.5k",
            orders_count=5,
            total_spent=2500.00,
            status_flags=["vip"],
        ),
        company=CompanyInfo(
            name="Acme Corporation",
            domain="acme.com",
            industry="Technology",
            logo_url="https://logo.clearbit.com/acme.com",
            linkedin_url="https://linkedin.com/company/acme",
        ),
        insight=InsightInfo(
            icon="celebration",
            text="First payment - Welcome aboard!",
        ),
        payment=PaymentInfo(
            amount=299.00,
            currency="USD",
            interval="monthly",
            plan_name="Pro Plan",
            subscription_id="sub_test789",
            payment_method="visa",
            card_last4="4242",
        ),
    )


class TestStoreEnrichedRecord:
    """Tests for store_enriched_record method."""

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_store_enriched_record_success(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        workspace_id: str,
        sample_event_data: dict,
        sample_notification: RichNotification,
    ) -> None:
        """Test successful enriched record storage with workspace_id."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        # Return empty list for activity list lookup
        mock_cache.get.return_value = []

        result = db_service.store_enriched_record(
            sample_event_data, sample_notification, workspace_id=workspace_id
        )

        assert result is True
        assert mock_cache.set.call_count >= 1

        # Verify the stored data structure
        first_call_args = mock_cache.set.call_args_list[0]
        stored_key = first_call_args[0][0]
        webhook_data = json.loads(first_call_args[0][1])

        # Verify key contains workspace_id
        assert workspace_id in stored_key

        # Check basic fields
        assert webhook_data["provider"] == "stripe"
        assert webhook_data["external_id"] == "pi_test123"
        assert webhook_data["customer_id"] == "cus_test456"
        assert webhook_data["amount"] == 299.00
        assert webhook_data["currency"] == "USD"
        assert webhook_data["workspace_id"] == workspace_id

        # Check enriched fields
        assert webhook_data["headline"] == "$299.00 from Acme Corp"
        assert webhook_data["severity"] == "success"
        assert webhook_data["company_name"] == "Acme Corporation"
        assert webhook_data["company_logo_url"] == "https://logo.clearbit.com/acme.com"
        assert webhook_data["company_domain"] == "acme.com"
        assert webhook_data["customer_email"] == "billing@acme.com"
        assert webhook_data["customer_name"] == "John Doe"
        assert webhook_data["customer_ltv"] == "$2.5k"
        assert webhook_data["customer_tenure"] == "Since Mar 2024"
        assert webhook_data["customer_status_flags"] == ["vip"]
        assert webhook_data["insight_text"] == "First payment - Welcome aboard!"
        assert webhook_data["insight_icon"] == "celebration"
        assert webhook_data["plan_name"] == "Pro Plan"
        assert webhook_data["payment_method"] == "visa"
        assert webhook_data["card_last4"] == "4242"

    @patch("webhooks.services.database_lookup.cache")
    def test_store_enriched_record_missing_provider(
        self,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        workspace_id: str,
        sample_notification: RichNotification,
    ) -> None:
        """Test storage fails gracefully when provider is missing."""
        event_data = {"type": "payment_success", "customer_id": "cus_123"}

        result = db_service.store_enriched_record(
            event_data, sample_notification, workspace_id=workspace_id
        )

        assert result is False
        mock_cache.set.assert_not_called()

    @patch("webhooks.services.database_lookup.cache")
    def test_store_enriched_record_missing_customer_id(
        self,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        workspace_id: str,
        sample_notification: RichNotification,
    ) -> None:
        """Test storage fails gracefully when customer_id is missing."""
        event_data = {"type": "payment_success", "provider": "stripe"}

        result = db_service.store_enriched_record(
            event_data, sample_notification, workspace_id=workspace_id
        )

        assert result is False
        mock_cache.set.assert_not_called()

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_store_enriched_record_without_company(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        workspace_id: str,
        sample_event_data: dict,
    ) -> None:
        """Test storage works without company enrichment."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        mock_cache.get.return_value = []

        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="$299.00 from Customer",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(email="test@example.com"),
        )

        result = db_service.store_enriched_record(
            sample_event_data, notification, workspace_id=workspace_id
        )

        assert result is True

        first_call_args = mock_cache.set.call_args_list[0]
        webhook_data = json.loads(first_call_args[0][1])

        assert "company_name" not in webhook_data
        assert "company_logo_url" not in webhook_data
        assert webhook_data["customer_email"] == "test@example.com"

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_store_enriched_record_without_insight(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        workspace_id: str,
        sample_event_data: dict,
    ) -> None:
        """Test storage works without insight data."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        mock_cache.get.return_value = []

        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="$299.00 from Customer",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
            customer=CustomerInfo(email="test@example.com"),
            insight=None,
        )

        result = db_service.store_enriched_record(
            sample_event_data, notification, workspace_id=workspace_id
        )

        assert result is True

        first_call_args = mock_cache.set.call_args_list[0]
        webhook_data = json.loads(first_call_args[0][1])

        assert "insight_text" not in webhook_data
        assert "insight_icon" not in webhook_data

    @patch("webhooks.services.database_lookup.cache")
    def test_store_enriched_record_handles_exception(
        self,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        workspace_id: str,
        sample_event_data: dict,
        sample_notification: RichNotification,
    ) -> None:
        """Test storage handles exceptions gracefully."""
        mock_cache.set.side_effect = Exception("Redis connection failed")

        result = db_service.store_enriched_record(
            sample_event_data,
            sample_notification,
            workspace_id=workspace_id,
        )

        assert result is False

    def test_ttl_defaults_to_7_days(self) -> None:
        """Test that default TTL is 7 days."""
        service = DatabaseLookupService()
        expected_ttl = 60 * 60 * 24 * 7  # 7 days in seconds
        assert service.ttl_seconds == expected_ttl

    def test_ttl_can_be_customized(self) -> None:
        """Test that TTL can be customized via constructor."""
        service = DatabaseLookupService(ttl_days=14)
        expected_ttl = 60 * 60 * 24 * 14  # 14 days in seconds
        assert service.ttl_seconds == expected_ttl


class TestWorkspaceIsolation:
    """Tests for workspace-scoped Redis key isolation."""

    def test_webhook_key_contains_workspace_id(
        self, db_service: DatabaseLookupService
    ) -> None:
        """Test that _get_webhook_key includes workspace_id in key."""
        key = db_service._get_webhook_key("payment", "ts123", "ws-abc")
        assert key == "webhook:ws-abc:payment:ts123"

    def test_activity_key_contains_workspace_id(
        self, db_service: DatabaseLookupService
    ) -> None:
        """Test that _get_activity_key includes workspace_id in key."""
        key = db_service._get_activity_key("2026-02-20", "ws-abc")
        assert key == "webhook_activity:ws-abc:2026-02-20"

    def test_webhook_key_defaults_to_global(
        self, db_service: DatabaseLookupService
    ) -> None:
        """Test that _get_webhook_key defaults workspace_id to 'global'."""
        key = db_service._get_webhook_key("payment", "ts123")
        assert key == "webhook:global:payment:ts123"

    def test_activity_key_defaults_to_global(
        self, db_service: DatabaseLookupService
    ) -> None:
        """Test that _get_activity_key defaults workspace_id to 'global'."""
        key = db_service._get_activity_key("2026-02-20")
        assert key == "webhook_activity:global:2026-02-20"

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_store_enriched_record_default_workspace_id(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        sample_event_data: dict,
        sample_notification: RichNotification,
    ) -> None:
        """Test that store_enriched_record defaults workspace_id to 'global'."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        mock_cache.get.return_value = []

        result = db_service.store_enriched_record(
            sample_event_data, sample_notification
        )

        assert result is True

        # Verify key contains "global" segment
        first_call_args = mock_cache.set.call_args_list[0]
        stored_key = first_call_args[0][0]
        assert ":global:" in stored_key

        # Verify stored record contains workspace_id field
        webhook_data = json.loads(first_call_args[0][1])
        assert webhook_data["workspace_id"] == "global"

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_stored_record_contains_workspace_id_field(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        sample_event_data: dict,
        sample_notification: RichNotification,
    ) -> None:
        """Test that the stored JSON contains a workspace_id field."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        mock_cache.get.return_value = []

        ws_id = "ws-specific-uuid"
        db_service.store_enriched_record(
            sample_event_data, sample_notification, workspace_id=ws_id
        )

        first_call_args = mock_cache.set.call_args_list[0]
        webhook_data = json.loads(first_call_args[0][1])
        assert webhook_data["workspace_id"] == ws_id

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_workspace_isolation_different_activity_keys(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
        sample_event_data: dict,
        sample_notification: RichNotification,
    ) -> None:
        """Test that records for different workspaces use different activity keys."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        mock_cache.get.return_value = []

        # Store for workspace A
        db_service.store_enriched_record(
            sample_event_data, sample_notification, workspace_id="ws-aaa"
        )
        # Store for workspace B
        db_service.store_enriched_record(
            sample_event_data, sample_notification, workspace_id="ws-bbb"
        )

        # Collect all keys used in cache.set calls
        all_keys = [call[0][0] for call in mock_cache.set.call_args_list]

        # Find activity keys (they contain "webhook_activity:")
        activity_keys = [k for k in all_keys if k.startswith("webhook_activity:")]

        assert len(activity_keys) == 2
        assert any("ws-aaa" in k for k in activity_keys)
        assert any("ws-bbb" in k for k in activity_keys)
        # They should be different keys
        assert activity_keys[0] != activity_keys[1]

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_get_recent_webhook_activity_filters_by_workspace(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
    ) -> None:
        """Test get_recent_webhook_activity filters by workspace."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now
        date_str = mock_now.strftime("%Y-%m-%d")

        ws_a_activity_key = f"webhook_activity:ws-aaa:{date_str}"
        ws_b_activity_key = f"webhook_activity:ws-bbb:{date_str}"

        ws_a_webhook_key = "webhook:ws-aaa:payment:20260220_120000_000000"
        ws_b_webhook_key = "webhook:ws-bbb:payment:20260220_120000_000001"

        ws_a_record = json.dumps(
            {
                "provider": "stripe",
                "timestamp": mock_now.timestamp(),
                "workspace_id": "ws-aaa",
            }
        )
        ws_b_record = json.dumps(
            {
                "provider": "shopify",
                "timestamp": mock_now.timestamp(),
                "workspace_id": "ws-bbb",
            }
        )

        def cache_get_side_effect(key: str, default: object = None) -> object:
            """Return different data based on key."""
            lookup = {
                ws_a_activity_key: json.dumps([ws_a_webhook_key]),
                ws_b_activity_key: json.dumps([ws_b_webhook_key]),
                ws_a_webhook_key: ws_a_record,
                ws_b_webhook_key: ws_b_record,
            }
            return lookup.get(key, default)

        mock_cache.get.side_effect = cache_get_side_effect

        # Query workspace A
        results_a = db_service.get_recent_webhook_activity("ws-aaa", days=1)
        assert len(results_a) == 1
        assert results_a[0]["provider"] == "stripe"
        assert results_a[0]["workspace_id"] == "ws-aaa"

        # Query workspace B
        results_b = db_service.get_recent_webhook_activity("ws-bbb", days=1)
        assert len(results_b) == 1
        assert results_b[0]["provider"] == "shopify"
        assert results_b[0]["workspace_id"] == "ws-bbb"

    @patch("webhooks.services.database_lookup.cache")
    @patch("webhooks.services.database_lookup.timezone")
    def test_get_recent_webhook_activity_empty_for_unknown_workspace(
        self,
        mock_timezone: MagicMock,
        mock_cache: MagicMock,
        db_service: DatabaseLookupService,
    ) -> None:
        """Test that querying an unknown workspace returns empty list."""
        from django.utils import timezone

        mock_now = timezone.now()
        mock_timezone.now.return_value = mock_now

        # Return empty/default for any key lookup
        mock_cache.get.return_value = []

        results = db_service.get_recent_webhook_activity("ws-nonexistent", days=7)
        assert results == []


class TestEventProcessorWorkspaceThreading:
    """Tests for EventProcessor threading workspace_id to storage."""

    @patch("webhooks.services.event_processor.DatabaseLookupService")
    @patch("webhooks.services.event_processor.DomainEnrichmentService")
    @patch("webhooks.services.event_processor.get_email_enrichment_service")
    def test_store_enriched_record_passes_workspace(
        self,
        mock_email_svc: MagicMock,
        mock_domain_svc: MagicMock,
        mock_db_cls: MagicMock,
    ) -> None:
        """Test that _store_enriched_record passes workspace_id to db_lookup."""
        from webhooks.services.event_processor import EventProcessor

        mock_db_instance = Mock()
        mock_db_cls.return_value = mock_db_instance

        processor = EventProcessor()

        # Create a mock workspace
        workspace = Mock()
        workspace.uuid = "ws-abc-123"

        event_data = {"type": "payment_success", "provider": "stripe"}
        notification = Mock()

        processor._store_enriched_record(event_data, notification, workspace)

        mock_db_instance.store_enriched_record.assert_called_once_with(
            event_data, notification, workspace_id="ws-abc-123"
        )

    @patch("webhooks.services.event_processor.DatabaseLookupService")
    @patch("webhooks.services.event_processor.DomainEnrichmentService")
    @patch("webhooks.services.event_processor.get_email_enrichment_service")
    def test_store_enriched_record_passes_global_when_no_workspace(
        self,
        mock_email_svc: MagicMock,
        mock_domain_svc: MagicMock,
        mock_db_cls: MagicMock,
    ) -> None:
        """Test that _store_enriched_record uses 'global' when workspace is None."""
        from webhooks.services.event_processor import EventProcessor

        mock_db_instance = Mock()
        mock_db_cls.return_value = mock_db_instance

        processor = EventProcessor()

        event_data = {"type": "payment_success", "provider": "stripe"}
        notification = Mock()

        processor._store_enriched_record(event_data, notification, workspace=None)

        mock_db_instance.store_enriched_record.assert_called_once_with(
            event_data, notification, workspace_id="global"
        )

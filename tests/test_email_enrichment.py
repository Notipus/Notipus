"""Tests for email enrichment plugin system (Hunter.io).

Tests cover:
- Hunter plugin interface and metadata
- Hunter API response normalization
- Email enrichment service (tier checks, caching, API calls)
- PersonInfo in RichNotification
- Person section in Slack formatter
"""

from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from core.permissions import TIER_ORDER, get_plan_tier, has_plan_or_higher
from plugins import PluginType
from plugins.base import PluginCapability
from plugins.enrichment.base_email import (
    EmailNotFoundError,
    GDPRClaimedError,
    RateLimitError,
)
from plugins.enrichment.hunter import HunterPlugin
from webhooks.models.rich_notification import PersonInfo, RichNotification

# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def hunter_plugin() -> HunterPlugin:
    """Create a Hunter plugin instance."""
    return HunterPlugin()


@pytest.fixture
def mock_hunter_response() -> dict[str, Any]:
    """Mock Hunter.io API response."""
    return {
        "data": {
            "name": {
                "fullName": "John Doe",
                "givenName": "John",
                "familyName": "Doe",
            },
            "email": "john@example.com",
            "employment": {
                "domain": "example.com",
                "title": "VP of Engineering",
                "seniority": "executive",
            },
            "linkedin": "johndoe",
            "twitter": "johndoe",
            "github": "johndoe",
            "location": "San Francisco, CA",
            "geo": {
                "city": "San Francisco",
                "state": "CA",
                "country": "United States",
            },
        }
    }


@pytest.fixture
def person_info() -> PersonInfo:
    """Create a sample PersonInfo for testing."""
    return PersonInfo(
        email="john@example.com",
        first_name="John",
        last_name="Doe",
        position="VP of Engineering",
        seniority="executive",
        company_domain="example.com",
        linkedin_url="https://linkedin.com/in/johndoe",
        twitter_handle="johndoe",
        github_handle="johndoe",
        location="San Francisco, CA",
    )


# ============================================================================
# Hunter Plugin Metadata Tests
# ============================================================================


class TestHunterPluginMetadata:
    """Tests for Hunter plugin metadata."""

    def test_metadata_name(self, hunter_plugin: HunterPlugin) -> None:
        """Test plugin name is correct."""
        metadata = hunter_plugin.get_metadata()
        assert metadata.name == "hunter"

    def test_metadata_display_name(self, hunter_plugin: HunterPlugin) -> None:
        """Test plugin display name."""
        metadata = hunter_plugin.get_metadata()
        assert metadata.display_name == "Hunter.io"

    def test_metadata_plugin_type(self, hunter_plugin: HunterPlugin) -> None:
        """Test plugin type is EMAIL_ENRICHMENT."""
        metadata = hunter_plugin.get_metadata()
        assert metadata.plugin_type == PluginType.EMAIL_ENRICHMENT

    def test_metadata_capabilities(self, hunter_plugin: HunterPlugin) -> None:
        """Test plugin capabilities include expected values."""
        metadata = hunter_plugin.get_metadata()
        expected = {
            PluginCapability.PERSON_NAME,
            PluginCapability.JOB_TITLE,
            PluginCapability.SENIORITY,
            PluginCapability.PERSON_LINKEDIN,
            PluginCapability.PERSON_TWITTER,
            PluginCapability.PERSON_GITHUB,
            PluginCapability.PERSON_LOCATION,
        }
        assert metadata.capabilities == expected

    def test_is_available_always_true(self, hunter_plugin: HunterPlugin) -> None:
        """Test is_available returns True (uses per-workspace keys)."""
        assert HunterPlugin.is_available() is True


# ============================================================================
# Hunter Plugin API Response Normalization Tests
# ============================================================================


class TestHunterPluginNormalization:
    """Tests for Hunter API response normalization."""

    def test_normalize_full_response(
        self, hunter_plugin: HunterPlugin, mock_hunter_response: dict
    ) -> None:
        """Test normalizing a complete Hunter response."""
        data = mock_hunter_response["data"]
        result = hunter_plugin._normalize_response(data, "john@example.com")

        assert result["email"] == "john@example.com"
        assert result["first_name"] == "John"
        assert result["last_name"] == "Doe"
        assert result["position"] == "VP of Engineering"
        assert result["seniority"] == "executive"
        assert result["company_domain"] == "example.com"
        assert result["linkedin_url"] == "https://linkedin.com/in/johndoe"
        assert result["twitter_handle"] == "johndoe"
        assert result["github_handle"] == "johndoe"
        assert result["location"] == "San Francisco, CA"
        assert "_raw" in result

    def test_normalize_empty_response(self, hunter_plugin: HunterPlugin) -> None:
        """Test normalizing an empty response."""
        result = hunter_plugin._normalize_response({}, "test@example.com")

        assert result["email"] == "test@example.com"
        assert result["first_name"] == ""
        assert result["last_name"] == ""
        assert result["position"] == ""
        assert result["seniority"] == ""

    def test_normalize_nested_social_objects(self, hunter_plugin: HunterPlugin) -> None:
        """Test that nested twitter/github objects yield the handle string.

        The live Hunter.io People API returns social fields as nested
        objects, not plain strings (seen in prod: the dict repr overflowed
        the varchar(100) handle columns and aborted every Person insert).
        """
        data = {
            "twitter": {"handle": "johndoe", "followers": 42, "id": 1},
            "github": {"handle": "jdoe", "followers": None},
        }
        result = hunter_plugin._normalize_response(data, "john@example.com")

        assert result["twitter_handle"] == "johndoe"
        assert result["github_handle"] == "jdoe"

    def test_normalize_nested_social_objects_with_null_handles(
        self, hunter_plugin: HunterPlugin
    ) -> None:
        """Test that all-null nested social objects normalize to empty strings."""
        data = {
            "twitter": {"handle": None, "id": None, "bio": None},
            "github": {"handle": None, "id": None},
        }
        result = hunter_plugin._normalize_response(data, "info@fcthun.ch")

        assert result["twitter_handle"] == ""
        assert result["github_handle"] == ""

    def test_extract_handle_rejects_non_string_scalars(
        self, hunter_plugin: HunterPlugin
    ) -> None:
        """Test that non-string, non-dict social values are dropped."""
        assert hunter_plugin._extract_handle(12345) == ""
        assert hunter_plugin._extract_handle(["johndoe"]) == ""
        assert hunter_plugin._extract_handle(None) == ""

    def test_build_linkedin_url_from_handle(self, hunter_plugin: HunterPlugin) -> None:
        """Test building LinkedIn URL from handle."""
        url = hunter_plugin._build_linkedin_url("johndoe")
        assert url == "https://linkedin.com/in/johndoe"

    def test_build_linkedin_url_from_full_url(
        self, hunter_plugin: HunterPlugin
    ) -> None:
        """Test passing through full LinkedIn URL."""
        full_url = "https://linkedin.com/in/johndoe"
        url = hunter_plugin._build_linkedin_url(full_url)
        assert url == full_url

    def test_build_linkedin_url_empty(self, hunter_plugin: HunterPlugin) -> None:
        """Test empty LinkedIn handle returns empty string."""
        url = hunter_plugin._build_linkedin_url(None)
        assert url == ""

    def test_build_location_from_direct_field(
        self, hunter_plugin: HunterPlugin
    ) -> None:
        """Test location from direct location field."""
        data = {"location": "San Francisco, CA"}
        location = hunter_plugin._build_location_string(data)
        assert location == "San Francisco, CA"

    def test_build_location_from_geo(self, hunter_plugin: HunterPlugin) -> None:
        """Test location from geo object."""
        data = {"geo": {"city": "San Francisco", "state": "CA"}}
        location = hunter_plugin._build_location_string(data)
        assert location == "San Francisco, CA"


# ============================================================================
# Hunter Plugin API Call Tests
# ============================================================================


class TestHunterPluginAPICall:
    """Tests for Hunter API calls."""

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_success(
        self,
        mock_get: Mock,
        hunter_plugin: HunterPlugin,
        mock_hunter_response: dict,
    ) -> None:
        """Test successful email enrichment."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_hunter_response
        mock_get.return_value.headers = {}

        result = hunter_plugin.enrich_email("john@example.com", "test-api-key")

        assert result["first_name"] == "John"
        assert result["last_name"] == "Doe"
        mock_get.assert_called_once()

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_not_found(
        self, mock_get: Mock, hunter_plugin: HunterPlugin
    ) -> None:
        """Test 404 response raises EmailNotFoundError."""
        mock_get.return_value.status_code = 404

        with pytest.raises(EmailNotFoundError):
            hunter_plugin.enrich_email("notfound@example.com", "test-api-key")

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_gdpr_claimed(
        self, mock_get: Mock, hunter_plugin: HunterPlugin
    ) -> None:
        """Test 451 response raises GDPRClaimedError."""
        mock_get.return_value.status_code = 451

        with pytest.raises(GDPRClaimedError):
            hunter_plugin.enrich_email("gdpr@example.com", "test-api-key")

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_rate_limit(
        self, mock_get: Mock, hunter_plugin: HunterPlugin
    ) -> None:
        """Test 429 response raises RateLimitError."""
        mock_get.return_value.status_code = 429
        mock_get.return_value.headers = {"Retry-After": "60"}

        with pytest.raises(RateLimitError) as exc_info:
            hunter_plugin.enrich_email("test@example.com", "test-api-key")

        assert exc_info.value.retry_after == 60

    def test_enrich_email_no_api_key(self, hunter_plugin: HunterPlugin) -> None:
        """Test enrichment without API key returns empty dict."""
        result = hunter_plugin.enrich_email("test@example.com", "")
        assert result == {}

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_free_email_provider(
        self, mock_get: MagicMock, hunter_plugin: HunterPlugin
    ) -> None:
        """Test that free email providers (Gmail) are filtered out before API call."""
        with pytest.raises(EmailNotFoundError):
            hunter_plugin.enrich_email("user@gmail.com", "test-api-key")
        mock_get.assert_not_called()

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_yahoo_filtered(
        self, mock_get: MagicMock, hunter_plugin: HunterPlugin
    ) -> None:
        """Test that Yahoo emails are filtered out before API call."""
        with pytest.raises(EmailNotFoundError):
            hunter_plugin.enrich_email("user@yahoo.com", "test-api-key")
        mock_get.assert_not_called()

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_disposable_filtered(
        self, mock_get: MagicMock, hunter_plugin: HunterPlugin
    ) -> None:
        """Test that disposable emails are filtered out before API call."""
        with pytest.raises(EmailNotFoundError):
            hunter_plugin.enrich_email("user@mailinator.com", "test-api-key")
        mock_get.assert_not_called()

    @patch("plugins.enrichment.hunter.requests.get")
    def test_enrich_email_400_bad_request(
        self, mock_get: MagicMock, hunter_plugin: HunterPlugin
    ) -> None:
        """Test 400 response (unsupported domain) raises EmailNotFoundError."""
        mock_get.return_value.status_code = 400

        # Business email that passes filter but Hunter.io rejects
        with pytest.raises(EmailNotFoundError):
            hunter_plugin.enrich_email("user@some-company.com", "test-api-key")


# ============================================================================
# PersonInfo Dataclass Tests
# ============================================================================


class TestPersonInfo:
    """Tests for PersonInfo dataclass."""

    def test_full_name_property(self, person_info: PersonInfo) -> None:
        """Test full_name computed property."""
        assert person_info.full_name == "John Doe"

    def test_full_name_first_only(self) -> None:
        """Test full_name with only first name."""
        person = PersonInfo(email="test@example.com", first_name="John")
        assert person.full_name == "John"

    def test_full_name_last_only(self) -> None:
        """Test full_name with only last name."""
        person = PersonInfo(email="test@example.com", last_name="Doe")
        assert person.full_name == "Doe"

    def test_full_name_none(self) -> None:
        """Test full_name when no name available."""
        person = PersonInfo(email="test@example.com")
        assert person.full_name is None

    def test_display_name_uses_full_name(self, person_info: PersonInfo) -> None:
        """Test display_name prefers full name."""
        assert person_info.display_name == "John Doe"

    def test_display_name_fallback_to_email(self) -> None:
        """Test display_name falls back to email."""
        person = PersonInfo(email="test@example.com")
        assert person.display_name == "test@example.com"


# ============================================================================
# Tier Check Tests
# ============================================================================


class TestTierChecks:
    """Tests for billing tier permission checks."""

    def test_tier_order_values(self) -> None:
        """Test TIER_ORDER has correct hierarchy."""
        assert TIER_ORDER["free"] == 0
        assert TIER_ORDER["trial"] == 0
        assert TIER_ORDER["basic"] == 1
        assert TIER_ORDER["pro"] == 2
        assert TIER_ORDER["enterprise"] == 3

    def test_has_plan_or_higher_pro_on_pro(self) -> None:
        """Test Pro workspace passes Pro check."""
        workspace = MagicMock()
        workspace.subscription_plan = "pro"
        assert has_plan_or_higher(workspace, "pro") is True

    def test_has_plan_or_higher_enterprise_on_pro(self) -> None:
        """Test Enterprise workspace passes Pro check."""
        workspace = MagicMock()
        workspace.subscription_plan = "enterprise"
        assert has_plan_or_higher(workspace, "pro") is True

    def test_has_plan_or_higher_basic_on_pro(self) -> None:
        """Test Basic workspace fails Pro check."""
        workspace = MagicMock()
        workspace.subscription_plan = "basic"
        assert has_plan_or_higher(workspace, "pro") is False

    def test_has_plan_or_higher_free_on_pro(self) -> None:
        """Test Free workspace fails Pro check."""
        workspace = MagicMock()
        workspace.subscription_plan = "free"
        assert has_plan_or_higher(workspace, "pro") is False

    def test_get_plan_tier(self) -> None:
        """Test get_plan_tier returns correct tier."""
        workspace = MagicMock()
        workspace.subscription_plan = "pro"
        assert get_plan_tier(workspace) == 2


# ============================================================================
# Slack Formatter Person Section Tests
# ============================================================================


class TestSlackPersonSection:
    """Tests for Slack formatter person section."""

    @pytest.fixture
    def slack_plugin(self):
        """Create Slack plugin instance."""
        from plugins.destinations.slack import SlackDestinationPlugin

        return SlackDestinationPlugin()

    def test_person_section_has_name(
        self, slack_plugin, person_info: PersonInfo
    ) -> None:
        """Test person section displays name."""
        blocks = slack_plugin._format_person_section(person_info)
        main_block = blocks[0]

        assert ":bust_in_silhouette:" in main_block["text"]["text"]
        assert "*John Doe*" in main_block["text"]["text"]

    def test_person_section_has_job_title(
        self, slack_plugin, person_info: PersonInfo
    ) -> None:
        """Test person section displays job title."""
        blocks = slack_plugin._format_person_section(person_info)
        main_block = blocks[0]

        assert "VP of Engineering" in main_block["text"]["text"]

    def test_person_section_has_seniority(
        self, slack_plugin, person_info: PersonInfo
    ) -> None:
        """Test person section displays seniority."""
        blocks = slack_plugin._format_person_section(person_info)
        main_block = blocks[0]

        # Seniority should be title-cased
        assert "Executive" in main_block["text"]["text"]

    def test_person_section_has_location(
        self, slack_plugin, person_info: PersonInfo
    ) -> None:
        """Test person section displays location."""
        blocks = slack_plugin._format_person_section(person_info)
        main_block = blocks[0]

        assert ":round_pushpin:" in main_block["text"]["text"]
        assert "San Francisco, CA" in main_block["text"]["text"]

    def test_person_section_has_linkedin_link(
        self, slack_plugin, person_info: PersonInfo
    ) -> None:
        """Test person section has LinkedIn link."""
        blocks = slack_plugin._format_person_section(person_info)

        # Should have 2 blocks: main section and links context
        assert len(blocks) == 2
        links_block = blocks[1]

        assert "linkedin.com" in links_block["elements"][0]["text"]
        assert "|LinkedIn>" in links_block["elements"][0]["text"]

    def test_person_section_has_twitter_link(
        self, slack_plugin, person_info: PersonInfo
    ) -> None:
        """Test person section has Twitter link."""
        blocks = slack_plugin._format_person_section(person_info)
        links_block = blocks[1]

        assert "twitter.com/johndoe" in links_block["elements"][0]["text"]
        assert "|Twitter>" in links_block["elements"][0]["text"]

    def test_person_section_has_github_link(
        self, slack_plugin, person_info: PersonInfo
    ) -> None:
        """Test person section has GitHub link."""
        blocks = slack_plugin._format_person_section(person_info)
        links_block = blocks[1]

        assert "github.com/johndoe" in links_block["elements"][0]["text"]
        assert "|GitHub>" in links_block["elements"][0]["text"]

    def test_person_section_no_links_without_data(self, slack_plugin) -> None:
        """Test person section without social links."""
        person = PersonInfo(
            email="test@example.com",
            first_name="Jane",
            last_name="Smith",
        )
        blocks = slack_plugin._format_person_section(person)

        # Should only have main section, no links block
        assert len(blocks) == 1

    def test_person_section_email_fallback(self, slack_plugin) -> None:
        """Test person section uses email when no name available."""
        person = PersonInfo(email="test@example.com")
        blocks = slack_plugin._format_person_section(person)
        main_block = blocks[0]

        assert "*test@example.com*" in main_block["text"]["text"]


# ============================================================================
# RichNotification Person Field Tests
# ============================================================================


class TestRichNotificationPerson:
    """Tests for person field in RichNotification."""

    def test_notification_has_person_field(self) -> None:
        """Test RichNotification accepts person field."""
        from webhooks.models.rich_notification import (
            NotificationSeverity,
            NotificationType,
        )

        person = PersonInfo(email="test@example.com", first_name="John")
        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="Test",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
            person=person,
        )

        assert notification.person is not None
        assert notification.person.first_name == "John"

    def test_notification_person_optional(self) -> None:
        """Test RichNotification works without person field."""
        from webhooks.models.rich_notification import (
            NotificationSeverity,
            NotificationType,
        )

        notification = RichNotification(
            type=NotificationType.PAYMENT_SUCCESS,
            severity=NotificationSeverity.SUCCESS,
            headline="Test",
            headline_icon="money",
            provider="stripe",
            provider_display="Stripe",
        )

        assert notification.person is None

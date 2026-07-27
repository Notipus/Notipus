"""Tests for enrichment plugin system.

Tests cover:
- Plugin interface and metadata
- Plugin registry (singleton, registration, discovery)
- Plugin lifecycle (availability, configuration)
- Data blending from multiple sources
- Integration with DomainEnrichmentService
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.services.enrichment import DataBlender, DomainEnrichmentService
from plugins import PluginRegistry, PluginType, register_plugin
from plugins.base import PluginCapability, PluginMetadata
from plugins.enrichment import BaseEnrichmentPlugin
from plugins.enrichment.brandfetch import BrandfetchPlugin

# ============================================================================
# Test Fixtures
# ============================================================================


class MockPlugin(BaseEnrichmentPlugin):
    """Mock plugin for testing."""

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        return PluginMetadata(
            name="mock",
            display_name="Mock Plugin",
            version="1.0.0",
            description="A mock plugin for testing",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.DESCRIPTION},
            priority=50,
            config_keys=["api_key"],
        )

    @classmethod
    def is_available(cls) -> bool:
        return True

    def configure(self, config: dict[str, Any]) -> None:
        self.api_key = config.get("api_key")

    def enrich_domain(self, domain: str) -> dict[str, Any]:
        return {
            "name": f"Mock Company for {domain}",
            "description": f"Mock description for {domain}",
        }


class UnavailablePlugin(BaseEnrichmentPlugin):
    """Plugin that reports as unavailable."""

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        return PluginMetadata(
            name="unavailable",
            display_name="Unavailable Plugin",
            version="1.0.0",
            description="A plugin that is never available",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.DESCRIPTION},
            priority=10,
        )

    @classmethod
    def is_available(cls) -> bool:
        return False

    def enrich_domain(self, domain: str) -> dict[str, Any]:
        return {}


class HighPriorityPlugin(BaseEnrichmentPlugin):
    """High priority plugin for testing."""

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        return PluginMetadata(
            name="high_priority",
            display_name="High Priority",
            version="1.0.0",
            description="High priority test plugin",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.LOGO, PluginCapability.DESCRIPTION},
            priority=200,
        )

    def enrich_domain(self, domain: str) -> dict[str, Any]:
        return {
            "name": "High Priority Name",
            "logo_url": "https://example.com/logo.png",
            "brand_info": {"description": "High priority description"},
        }


@pytest.fixture
def registry() -> PluginRegistry:
    """Get a fresh plugin registry for each test."""
    PluginRegistry.reset()
    return PluginRegistry.instance()


@pytest.fixture
def mock_settings():
    """Mock Django settings for plugin configuration."""
    return {
        "PLUGINS": {
            "enrichment": {
                "mock": {
                    "enabled": True,
                    "priority": 50,
                    "config": {"api_key": "test-key"},
                },
                "brandfetch": {
                    "enabled": True,
                    "priority": 100,
                    "config": {"api_key": "test-brandfetch-key"},
                },
            },
        },
        "PLUGIN_AUTODISCOVER": False,
    }


# ============================================================================
# Plugin Metadata Tests
# ============================================================================


class TestPluginMetadata:
    """Tests for PluginMetadata dataclass."""

    def test_metadata_creation(self) -> None:
        """Test creating plugin metadata."""
        metadata = PluginMetadata(
            name="test",
            display_name="Test Plugin",
            version="1.0.0",
            description="A test plugin",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.LOGO},
            priority=50,
            config_keys=["api_key"],
        )

        assert metadata.name == "test"
        assert metadata.display_name == "Test Plugin"
        assert metadata.version == "1.0.0"
        assert metadata.plugin_type == PluginType.ENRICHMENT
        assert PluginCapability.LOGO in metadata.capabilities
        assert metadata.priority == 50
        assert "api_key" in metadata.config_keys

    def test_metadata_defaults(self) -> None:
        """Test metadata default values."""
        metadata = PluginMetadata(
            name="minimal",
            display_name="Minimal",
            version="0.1.0",
            description="Minimal plugin",
            plugin_type=PluginType.ENRICHMENT,
            capabilities=set(),
        )

        assert metadata.priority == 0
        assert metadata.config_keys == []


class TestPluginCapability:
    """Tests for PluginCapability enum."""

    def test_all_capabilities_exist(self) -> None:
        """Test that expected capabilities are defined."""
        expected = [
            "LOGO",
            "DESCRIPTION",
            "INDUSTRY",
            "SOCIAL_LINKS",
            "EMPLOYEE_COUNT",
            "FUNDING",
            "COLORS",
            "YEAR_FOUNDED",
        ]
        for cap in expected:
            assert hasattr(PluginCapability, cap)

    def test_capability_values(self) -> None:
        """Test capability enum values."""
        assert PluginCapability.LOGO.value == "logo"
        assert PluginCapability.DESCRIPTION.value == "description"


# ============================================================================
# Plugin Registry Tests
# ============================================================================


class TestPluginRegistry:
    """Tests for PluginRegistry."""

    def test_singleton_pattern(self, registry: PluginRegistry) -> None:
        """Test that registry is a singleton."""
        registry2 = PluginRegistry.instance()
        assert registry is registry2

    def test_reset_creates_new_instance(self) -> None:
        """Test that reset() creates a new singleton instance."""
        registry1 = PluginRegistry.instance()
        PluginRegistry.reset()
        registry2 = PluginRegistry.instance()
        assert registry1 is not registry2

    def test_register_plugin(self, registry: PluginRegistry) -> None:
        """Test registering a plugin."""
        registry.register(MockPlugin)

        assert "mock" in registry.get_all_classes(PluginType.ENRICHMENT)
        assert registry.get_plugin_class(PluginType.ENRICHMENT, "mock") is MockPlugin

    def test_register_duplicate_replaces(self, registry: PluginRegistry) -> None:
        """Test that registering same name replaces existing."""
        registry.register(MockPlugin)
        registry.register(MockPlugin)  # Should not raise

        assert len(registry.get_all_classes(PluginType.ENRICHMENT)) == 1

    def test_unregister_plugin(self, registry: PluginRegistry) -> None:
        """Test unregistering a plugin."""
        registry.register(MockPlugin)
        assert registry.unregister(PluginType.ENRICHMENT, "mock") is True
        assert "mock" not in registry.get_all_classes(PluginType.ENRICHMENT)

    def test_unregister_nonexistent(self, registry: PluginRegistry) -> None:
        """Test unregistering a non-existent plugin."""
        assert registry.unregister(PluginType.ENRICHMENT, "nonexistent") is False

    def test_get_plugin_metadata(self, registry: PluginRegistry) -> None:
        """Test getting plugin metadata."""
        registry.register(MockPlugin)
        metadata = registry.get_metadata(PluginType.ENRICHMENT, "mock")

        assert metadata is not None
        assert metadata.name == "mock"
        assert metadata.display_name == "Mock Plugin"

    def test_get_nonexistent_metadata(self, registry: PluginRegistry) -> None:
        """Test getting metadata for non-existent plugin."""
        assert registry.get_metadata(PluginType.ENRICHMENT, "nonexistent") is None

    @patch("plugins.registry.settings")
    def test_is_plugin_enabled(
        self, mock_settings: MagicMock, registry: PluginRegistry
    ) -> None:
        """Test checking if plugin is enabled."""
        mock_settings.PLUGINS = {
            "enrichment": {
                "mock": {"enabled": True},
                "disabled": {"enabled": False},
            }
        }

        registry.register(MockPlugin)
        assert registry.is_enabled(PluginType.ENRICHMENT, "mock") is True
        assert registry.is_enabled(PluginType.ENRICHMENT, "disabled") is False
        # Default to True if not in settings
        assert registry.is_enabled(PluginType.ENRICHMENT, "unknown") is True

    def test_is_plugin_available(self, registry: PluginRegistry) -> None:
        """Test checking plugin availability."""
        registry.register(MockPlugin)
        registry.register(UnavailablePlugin)

        assert registry.is_available(PluginType.ENRICHMENT, "mock") is True
        assert registry.is_available(PluginType.ENRICHMENT, "unavailable") is False

    @patch("plugins.registry.settings")
    def test_get_instance(
        self, mock_settings: MagicMock, registry: PluginRegistry
    ) -> None:
        """Test getting plugin instance."""
        mock_settings.PLUGINS = {
            "enrichment": {"mock": {"config": {"api_key": "test-key"}}}
        }

        registry.register(MockPlugin)
        instance = registry.get(PluginType.ENRICHMENT, "mock")

        assert instance is not None
        assert isinstance(instance, MockPlugin)
        assert instance.api_key == "test-key"

    @patch("plugins.registry.settings")
    def test_get_instance_caches(
        self, mock_settings: MagicMock, registry: PluginRegistry
    ) -> None:
        """Test that get caches instances."""
        mock_settings.PLUGINS = {"enrichment": {"mock": {"config": {}}}}

        registry.register(MockPlugin)
        instance1 = registry.get(PluginType.ENRICHMENT, "mock")
        instance2 = registry.get(PluginType.ENRICHMENT, "mock")

        assert instance1 is instance2

    def test_get_instance_unavailable(self, registry: PluginRegistry) -> None:
        """Test getting instance of unavailable plugin."""
        registry.register(UnavailablePlugin)
        assert registry.get(PluginType.ENRICHMENT, "unavailable") is None

    @patch("plugins.registry.settings")
    def test_get_enabled_plugins_sorted_by_priority(
        self, mock_settings: MagicMock, registry: PluginRegistry
    ) -> None:
        """Test that enabled plugins are sorted by priority."""
        mock_settings.PLUGINS = {
            "enrichment": {
                "mock": {"enabled": True, "config": {}},
                "high_priority": {"enabled": True, "config": {}},
            }
        }

        registry.register(MockPlugin)  # priority 50
        registry.register(HighPriorityPlugin)  # priority 200

        plugins = registry.get_enabled(PluginType.ENRICHMENT)

        assert len(plugins) == 2
        # High priority should come first
        assert plugins[0].get_plugin_name() == "high_priority"
        assert plugins[1].get_plugin_name() == "mock"

    @patch("plugins.registry.settings")
    def test_get_enabled_plugins_excludes_disabled(
        self, mock_settings: MagicMock, registry: PluginRegistry
    ) -> None:
        """Test that disabled plugins are excluded."""
        mock_settings.PLUGINS = {
            "enrichment": {"mock": {"enabled": False, "config": {}}},
        }

        registry.register(MockPlugin)
        plugins = registry.get_enabled(PluginType.ENRICHMENT)

        assert len(plugins) == 0

    def test_list_plugins(self, registry: PluginRegistry) -> None:
        """Test listing all plugins with status."""
        registry.register(MockPlugin)
        registry.register(UnavailablePlugin)

        plugins = registry.list_plugins(PluginType.ENRICHMENT)

        assert len(plugins) == 2
        mock_info = next(p for p in plugins if p["name"] == "mock")
        assert mock_info["display_name"] == "Mock Plugin"
        assert mock_info["available"] is True

        unavail_info = next(p for p in plugins if p["name"] == "unavailable")
        assert unavail_info["available"] is False


class TestRegisterPluginDecorator:
    """Tests for @register_plugin decorator."""

    def test_decorator_registers_plugin(self) -> None:
        """Test that decorator registers the plugin."""
        PluginRegistry.reset()

        @register_plugin
        class DecoratedPlugin(BaseEnrichmentPlugin):
            @classmethod
            def get_metadata(cls) -> PluginMetadata:
                return PluginMetadata(
                    name="decorated",
                    display_name="Decorated",
                    version="1.0.0",
                    description="Decorated plugin",
                    plugin_type=PluginType.ENRICHMENT,
                    capabilities=set(),
                )

            def enrich_domain(self, domain: str) -> dict[str, Any]:
                return {}

        registry = PluginRegistry.instance()
        assert "decorated" in registry.get_all_classes(PluginType.ENRICHMENT)


# ============================================================================
# Brandfetch Plugin Tests
# ============================================================================


class TestBrandfetchPlugin:
    """Tests for BrandfetchPlugin."""

    def test_metadata(self) -> None:
        """Test Brandfetch plugin metadata."""
        metadata = BrandfetchPlugin.get_metadata()

        assert metadata.name == "brandfetch"
        assert metadata.display_name == "Brandfetch"
        assert metadata.priority == 100
        assert metadata.plugin_type == PluginType.ENRICHMENT
        assert PluginCapability.LOGO in metadata.capabilities
        assert PluginCapability.DESCRIPTION in metadata.capabilities
        assert "api_key" in metadata.config_keys

    @patch("plugins.enrichment.brandfetch.settings")
    def test_is_available_with_config(self, mock_settings: MagicMock) -> None:
        """Test availability check with configured API key."""
        mock_settings.PLUGINS = {
            "enrichment": {"brandfetch": {"config": {"api_key": "test-key"}}}
        }

        assert BrandfetchPlugin.is_available() is True

    @patch("plugins.enrichment.brandfetch.settings")
    def test_is_available_without_config(self, mock_settings: MagicMock) -> None:
        """Test availability check without API key."""
        mock_settings.PLUGINS = {"enrichment": {"brandfetch": {"config": {}}}}

        assert BrandfetchPlugin.is_available() is False

    def test_configure(self) -> None:
        """Test plugin configuration."""
        plugin = BrandfetchPlugin()
        plugin.configure(
            {
                "api_key": "test-key",
                "base_url": "https://custom.api.com",
                "timeout": 30,
            }
        )

        assert plugin.api_key == "test-key"
        assert plugin.base_url == "https://custom.api.com"
        assert plugin.timeout == 30

    def test_configure_defaults(self) -> None:
        """Test configuration with defaults."""
        plugin = BrandfetchPlugin()
        plugin.configure({"api_key": "test-key"})

        assert plugin.api_key == "test-key"
        assert plugin.base_url == "https://api.brandfetch.io/v2"
        assert plugin.timeout == 10

    def test_get_plugin_name(self) -> None:
        """Test plugin name from metadata."""
        plugin = BrandfetchPlugin()
        assert plugin.get_plugin_name() == "brandfetch"

    @patch("plugins.enrichment.brandfetch.requests.get")
    def test_enrich_domain_handles_404_gracefully(self, mock_get: MagicMock) -> None:
        """Test that 404 responses are handled gracefully without error logging."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        plugin = BrandfetchPlugin()
        plugin.configure({"api_key": "test-key"})

        result = plugin.enrich_domain("unknown-domain.gov")

        assert result == {}
        mock_get.assert_called_once()

    @patch("plugins.enrichment.brandfetch.requests.get")
    def test_enrich_domain_handles_429_rate_limit(self, mock_get: MagicMock) -> None:
        """Test that 429 rate limit responses are handled with warning."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "120"}
        mock_get.return_value = mock_response

        plugin = BrandfetchPlugin()
        plugin.configure({"api_key": "test-key"})

        result = plugin.enrich_domain("example.com")

        assert result == {}
        mock_get.assert_called_once()

    @patch("plugins.enrichment.brandfetch.requests.get")
    def test_enrich_domain_handles_timeout(self, mock_get: MagicMock) -> None:
        """Test that timeout exceptions are handled gracefully."""
        import requests

        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

        plugin = BrandfetchPlugin()
        plugin.configure({"api_key": "test-key"})

        result = plugin.enrich_domain("slow-domain.com")

        assert result == {}


# ============================================================================
# Data Blender Tests
# ============================================================================


class TestDataBlender:
    """Tests for DataBlender."""

    def test_blend_single_source(self) -> None:
        """Test blending data from a single source."""
        blender = DataBlender()
        source_data = {
            "brandfetch": {
                "name": "Acme Corp",
                "logo_url": "https://example.com/logo.png",
                "brand_info": {"description": "A company"},
            }
        }

        result = blender.blend(source_data)

        assert result["name"] == "Acme Corp"
        assert result["logo_url"] == "https://example.com/logo.png"
        assert result["description"] == "A company"
        assert "_sources" in result
        assert "_blended_at" in result
        assert result["_blend_version"] == 1

    def test_blend_multiple_sources_uses_priority(self) -> None:
        """Test that blending uses field priorities."""
        blender = DataBlender()
        source_data = {
            "brandfetch": {
                "name": "Brandfetch Name",
                "logo_url": "https://brandfetch.com/logo.png",
                "brand_info": {"description": "Brandfetch description"},
            },
            "openai": {
                "name": "OpenAI Name",
                "brand_info": {"description": "OpenAI description"},
            },
        }

        result = blender.blend(source_data)

        # Name should come from brandfetch (higher priority for name)
        assert result["name"] == "Brandfetch Name"
        # Logo should come from brandfetch (only source with logo capability)
        assert result["logo_url"] == "https://brandfetch.com/logo.png"
        # Description should come from openai (higher priority for description)
        assert result["description"] == "OpenAI description"

    def test_blend_preserves_sources(self) -> None:
        """Test that source data is preserved."""
        blender = DataBlender()
        source_data = {
            "brandfetch": {"name": "Test"},
            "openai": {"description": "Test desc"},
        }

        result = blender.blend(source_data)

        assert "brandfetch" in result["_sources"]
        assert "openai" in result["_sources"]
        assert result["_sources"]["brandfetch"]["raw"] == {"name": "Test"}

    def test_blend_handles_missing_fields(self) -> None:
        """Test blending with missing fields in some sources."""
        blender = DataBlender()
        source_data = {
            "brandfetch": {"logo_url": "https://example.com/logo.png"},
            "openai": {"description": "A description"},
        }

        result = blender.blend(source_data)

        assert result.get("name") is None
        assert result["logo_url"] == "https://example.com/logo.png"
        assert result["description"] == "A description"

    def test_blend_empty_sources(self) -> None:
        """Test blending with no sources."""
        blender = DataBlender()
        result = blender.blend({})

        assert "_sources" in result
        assert "_blended_at" in result
        assert len(result["_sources"]) == 0


# ============================================================================
# Domain Enrichment Service Tests
# ============================================================================


@pytest.mark.django_db
class TestDomainEnrichmentService:
    """Tests for DomainEnrichmentService."""

    @patch("core.services.enrichment.PluginRegistry")
    @patch("core.services.enrichment.settings")
    def test_initialization(
        self, mock_settings: MagicMock, mock_registry_class: MagicMock
    ) -> None:
        """Test service initialization."""
        mock_settings.PLUGIN_AUTODISCOVER = True
        mock_registry = MagicMock()
        mock_registry.get_enabled.return_value = []
        mock_registry_class.instance.return_value = mock_registry

        DomainEnrichmentService()

        mock_registry.discover.assert_called_once()
        mock_registry.get_enabled.assert_called_once()

    @patch("core.services.enrichment.PluginRegistry")
    @patch("core.services.enrichment.settings")
    def test_enrich_domain_empty(
        self, mock_settings: MagicMock, mock_registry_class: MagicMock
    ) -> None:
        """Test enriching with empty domain."""
        mock_settings.PLUGIN_AUTODISCOVER = False
        mock_registry = MagicMock()
        mock_registry.get_enabled.return_value = []
        mock_registry_class.instance.return_value = mock_registry

        service = DomainEnrichmentService()
        result = service.enrich_domain("")

        assert result is None

    @patch("core.services.enrichment.PluginRegistry")
    @patch("core.services.enrichment.settings")
    def test_enrich_domain_creates_company(
        self, mock_settings: MagicMock, mock_registry_class: MagicMock
    ) -> None:
        """Test that enrichment creates company record."""
        mock_settings.PLUGIN_AUTODISCOVER = False

        mock_plugin = MagicMock(spec=BaseEnrichmentPlugin)
        mock_plugin.get_plugin_name.return_value = "mock"
        mock_plugin.enrich_domain.return_value = {
            "name": "Test Company",
            "logo_url": "https://example.com/logo.png",
        }

        mock_registry = MagicMock()
        mock_registry.get_enabled.return_value = [mock_plugin]
        mock_registry_class.instance.return_value = mock_registry

        service = DomainEnrichmentService()
        result = service.enrich_domain("test.com")

        assert result is not None
        assert result.domain == "test.com"
        assert result.name == "Test Company"

    @patch("core.services.enrichment.PluginRegistry")
    @patch("core.services.enrichment.settings")
    def test_enrich_domain_uses_cache(
        self, mock_settings: MagicMock, mock_registry_class: MagicMock
    ) -> None:
        """Test that recent enrichment is cached."""
        from core.models import Company

        mock_settings.PLUGIN_AUTODISCOVER = False
        mock_registry = MagicMock()
        mock_registry.get_enabled.return_value = []
        mock_registry_class.instance.return_value = mock_registry

        # Create company with recent enrichment
        recent_time = datetime.now(timezone.utc).isoformat()
        company = Company.objects.create(
            domain="cached.com",
            name="Cached Company",
            brand_info={"_blended_at": recent_time},
        )

        service = DomainEnrichmentService()
        result = service.enrich_domain("cached.com")

        assert result == company
        # Plugin should not be called for cached data

    @patch("core.services.enrichment.PluginRegistry")
    @patch("core.services.enrichment.settings")
    def test_refresh_enrichment(
        self, mock_settings: MagicMock, mock_registry_class: MagicMock
    ) -> None:
        """Test forcing refresh of enrichment."""
        from core.models import Company

        mock_settings.PLUGIN_AUTODISCOVER = False

        mock_plugin = MagicMock(spec=BaseEnrichmentPlugin)
        mock_plugin.get_plugin_name.return_value = "mock"
        mock_plugin.enrich_domain.return_value = {
            "name": "Refreshed Company",
        }

        mock_registry = MagicMock()
        mock_registry.get_enabled.return_value = [mock_plugin]
        mock_registry_class.instance.return_value = mock_registry

        # Create company with old data
        Company.objects.create(
            domain="refresh.com",
            name="Old Name",
            brand_info={"_blended_at": "2020-01-01T00:00:00Z"},
        )

        service = DomainEnrichmentService()
        result = service.refresh_enrichment("refresh.com")

        assert result is not None
        assert result.name == "Refreshed Company"

    @patch("core.services.enrichment.PluginRegistry")
    @patch("core.services.enrichment.settings")
    def test_get_available_plugins(
        self, mock_settings: MagicMock, mock_registry_class: MagicMock
    ) -> None:
        """Test getting available plugins info."""
        mock_settings.PLUGIN_AUTODISCOVER = False

        mock_registry = MagicMock()
        mock_registry.get_enabled.return_value = []
        mock_registry.list_plugins.return_value = [{"name": "mock", "available": True}]
        mock_registry_class.instance.return_value = mock_registry

        service = DomainEnrichmentService()
        plugins = service.get_available_plugins()

        assert len(plugins) == 1
        assert plugins[0]["name"] == "mock"


# ============================================================================
# Integration Tests
# ============================================================================


@pytest.mark.django_db
class TestPluginIntegration:
    """Integration tests for the plugin system."""

    def test_full_plugin_lifecycle(self) -> None:
        """Test complete plugin lifecycle."""
        # Reset and get fresh registry
        PluginRegistry.reset()
        registry = PluginRegistry.instance()

        # Register plugin
        registry.register(MockPlugin)

        # Check registration
        assert "mock" in registry.get_all_classes(PluginType.ENRICHMENT)

        # Get metadata
        metadata = registry.get_metadata(PluginType.ENRICHMENT, "mock")
        assert metadata.name == "mock"

        # Check availability
        assert registry.is_available(PluginType.ENRICHMENT, "mock") is True

        # Get instance (with mocked settings)
        with patch("plugins.registry.settings") as mock_settings:
            mock_settings.PLUGINS = {
                "enrichment": {"mock": {"enabled": True, "config": {"api_key": "test"}}}
            }

            instance = registry.get(PluginType.ENRICHMENT, "mock")
            assert instance is not None
            assert instance.api_key == "test"

            # Use plugin
            result = instance.enrich_domain("example.com")
            assert result["name"] == "Mock Company for example.com"

    def test_discovery_finds_brandfetch(self) -> None:
        """Test that auto-discovery finds BrandfetchPlugin."""
        PluginRegistry.reset()
        registry = PluginRegistry.instance()

        discovered = registry.discover()

        assert "brandfetch" in discovered[PluginType.ENRICHMENT]
        assert (
            registry.get_plugin_class(PluginType.ENRICHMENT, "brandfetch")
            is BrandfetchPlugin
        )

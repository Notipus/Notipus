"""Base classes and metadata for the unified plugin system.

This module provides the foundational classes for all plugin types:
- PluginType: Enumeration of plugin categories
- PluginCapability: Capabilities a plugin can provide
- PluginMetadata: Metadata describing a plugin
- BasePlugin: Abstract base class for all plugins
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class PluginType(Enum):
    """Types of plugins in the system.

    Each type has a dedicated subpackage and base class.
    """

    ENRICHMENT = "enrichment"  # Domain/company enrichment
    EMAIL_ENRICHMENT = "email_enrichment"  # Person/email enrichment
    SOURCE = "source"
    DESTINATION = "destination"


class PluginCapability(Enum):
    """Capabilities a plugin can provide.

    Used primarily for enrichment plugins to indicate what data types
    they can retrieve, but can be extended for other plugin types.
    """

    # Domain enrichment capabilities (company data)
    LOGO = "logo"
    DESCRIPTION = "description"
    INDUSTRY = "industry"
    SOCIAL_LINKS = "social_links"
    EMPLOYEE_COUNT = "employee_count"
    FUNDING = "funding"
    COLORS = "colors"
    YEAR_FOUNDED = "year_founded"

    # Email enrichment capabilities (person data)
    PERSON_NAME = "person_name"
    JOB_TITLE = "job_title"
    SENIORITY = "seniority"
    PERSON_LINKEDIN = "person_linkedin"
    PERSON_TWITTER = "person_twitter"
    PERSON_GITHUB = "person_github"
    PERSON_LOCATION = "person_location"

    # Source capabilities
    WEBHOOK_VALIDATION = "webhook_validation"
    CUSTOMER_DATA = "customer_data"
    PAYMENT_HISTORY = "payment_history"

    # Destination capabilities
    RICH_FORMATTING = "rich_formatting"
    ATTACHMENTS = "attachments"
    ACTIONS = "actions"


@dataclass
class PluginMetadata:
    """Metadata describing a plugin.

    Attributes:
        name: Unique identifier for the plugin (e.g., "brandfetch", "stripe").
        display_name: Human-readable name (e.g., "Brandfetch", "Stripe").
        version: Semantic version string (e.g., "1.0.0").
        description: Brief description of what the plugin does.
        plugin_type: The type of plugin (enrichment, source, destination).
        capabilities: Set of capabilities this plugin provides.
        priority: Priority for ordering (higher = preferred). Default 0.
        config_keys: List of required configuration keys (e.g., ["api_key"]).
    """

    name: str
    display_name: str
    version: str
    description: str
    plugin_type: PluginType
    capabilities: set[PluginCapability] = field(default_factory=set)
    priority: int = 0
    config_keys: list[str] = field(default_factory=list)


class BasePlugin(ABC):
    """Abstract base class for all plugins.

    All plugin types (enrichment, source, destination) inherit from this class.
    Provides common functionality for metadata, availability checks, and configuration.

    Subclasses must implement:
    - get_metadata(): Return plugin metadata

    Subclasses may override:
    - is_available(): Check if plugin can be used (e.g., API key exists)
    - configure(): Configure plugin with settings from Django config
    """

    @classmethod
    @abstractmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        This is called before instantiation to get plugin information
        for registration and configuration.

        Returns:
            PluginMetadata describing this plugin.
        """
        pass

    @classmethod
    def is_available(cls) -> bool:
        """Check if plugin can be used.

        Override this to check for required configuration (e.g., API keys).
        Default implementation returns True.

        Returns:
            True if the plugin is available for use.
        """
        return True

    def configure(self, config: dict[str, Any]) -> None:
        """Configure plugin with settings.

        Called after instantiation with configuration from Django settings.
        Override to set up API keys, URLs, timeouts, etc.

        This is intentionally not abstract - plugins can choose to override
        this method if they need configuration, or use the default no-op.

        Args:
            config: Configuration dictionary from PLUGINS settings.
        """
        # Default implementation stores config for potential future use
        # Subclasses should override to extract specific configuration
        self._config = config

    def get_plugin_name(self) -> str:
        """Return unique plugin identifier.

        Returns the plugin name from metadata, falling back to class name.

        Returns:
            String identifier for this plugin.
        """
        try:
            return self.get_metadata().name
        except (NotImplementedError, AttributeError):
            return self.__class__.__name__.lower().replace("plugin", "")

    def get_plugin_type(self) -> PluginType:
        """Return the plugin type.

        Returns:
            PluginType enum value.
        """
        return self.get_metadata().plugin_type

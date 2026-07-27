"""Unified plugin registry for all plugin types.

This module provides the central registry for managing all plugins
(enrichment, source, destination), including registration, auto-discovery,
and lifecycle management.
"""

import importlib
import logging
import pkgutil
from typing import Any, TypeVar, cast

from django.conf import settings

from .base import BasePlugin, PluginMetadata, PluginType

logger = logging.getLogger(__name__)

# Type variable for plugin classes
P = TypeVar("P", bound=BasePlugin)


class PluginRegistry:
    """Unified registry for all plugin types.

    This is a singleton that manages all plugins. Plugins can be:
    - Manually registered via register()
    - Auto-discovered via discover()
    - Registered using the @register_plugin decorator

    The registry handles:
    - Plugin registration and storage by type
    - Plugin instantiation and configuration
    - Filtering by availability and enabled status

    Usage:
        registry = PluginRegistry.instance()
        registry.discover()  # Auto-discover all plugins

        # Get plugins by type
        enrichment_plugins = registry.get_enabled(PluginType.ENRICHMENT)
        source_plugin = registry.get(PluginType.SOURCE, "stripe")
    """

    _instance: "PluginRegistry | None" = None
    _plugins: dict[PluginType, dict[str, type[BasePlugin]]]
    _instances: dict[str, BasePlugin]
    _initialized: bool

    def __new__(cls) -> "PluginRegistry":
        """Create or return the singleton instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._plugins = {
                PluginType.ENRICHMENT: {},
                PluginType.EMAIL_ENRICHMENT: {},
                PluginType.SOURCE: {},
                PluginType.DESTINATION: {},
            }
            cls._instance._instances = {}
            cls._instance._initialized = False
        return cls._instance

    @classmethod
    def instance(cls) -> "PluginRegistry":
        """Get the singleton registry instance.

        Returns:
            The PluginRegistry singleton.
        """
        return cls()

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance.

        Useful for testing to get a fresh registry.
        """
        cls._instance = None

    def register(self, plugin_class: type[BasePlugin]) -> type[BasePlugin]:
        """Register a plugin class.

        Can be used as a decorator or called directly.

        Args:
            plugin_class: The plugin class to register (not an instance).

        Returns:
            The plugin class (for decorator chaining).

        Raises:
            ValueError: If plugin_class doesn't have valid metadata.
        """
        try:
            metadata = plugin_class.get_metadata()
        except Exception as e:
            logger.error(f"Failed to get metadata from {plugin_class}: {e}")
            raise ValueError(f"Invalid plugin class: {e}") from e

        plugin_type = metadata.plugin_type
        if metadata.name in self._plugins[plugin_type]:
            logger.warning(
                f"Plugin '{metadata.name}' ({plugin_type.value}) already registered, "
                f"replacing with {plugin_class.__name__}"
            )

        self._plugins[plugin_type][metadata.name] = plugin_class
        logger.debug(
            f"Registered {plugin_type.value} plugin: "
            f"{metadata.name} ({metadata.display_name})"
        )
        return plugin_class

    def unregister(self, plugin_type: PluginType, name: str) -> bool:
        """Unregister a plugin by type and name.

        Args:
            plugin_type: The plugin type.
            name: The plugin name to unregister.

        Returns:
            True if plugin was unregistered, False if not found.
        """
        if name in self._plugins[plugin_type]:
            del self._plugins[plugin_type][name]
            instance_key = f"{plugin_type.value}:{name}"
            if instance_key in self._instances:
                del self._instances[instance_key]
            logger.debug(f"Unregistered {plugin_type.value} plugin: {name}")
            return True
        return False

    def discover(self) -> dict[PluginType, list[str]]:
        """Auto-discover plugins in all plugin subpackages.

        Scans app/plugins/{enrichment,sources,destinations}/ for modules
        containing plugin subclasses and registers them.

        Returns:
            Dictionary mapping plugin types to lists of discovered plugin names.
        """
        discovered: dict[PluginType, list[str]] = {
            PluginType.ENRICHMENT: [],
            PluginType.EMAIL_ENRICHMENT: [],
            PluginType.SOURCE: [],
            PluginType.DESTINATION: [],
        }

        # Map subpackage names to allowed plugin types
        # The enrichment folder can contain both domain and email enrichment plugins
        subpackages: dict[str, list[PluginType]] = {
            "enrichment": [PluginType.ENRICHMENT, PluginType.EMAIL_ENRICHMENT],
            "sources": [PluginType.SOURCE],
            "destinations": [PluginType.DESTINATION],
        }

        for subpackage, allowed_types in subpackages.items():
            try:
                package = importlib.import_module(f"plugins.{subpackage}")
                package_path = package.__path__

                for _importer, module_name, _is_pkg in pkgutil.iter_modules(
                    package_path
                ):
                    # Skip base and __init__ modules
                    if module_name in ("base", "base_email", "__init__"):
                        continue

                    try:
                        module = importlib.import_module(
                            f"plugins.{subpackage}.{module_name}"
                        )
                        names = self._register_plugins_from_module(
                            module, allowed_types
                        )
                        for plugin_type, plugin_names in names.items():
                            discovered[plugin_type].extend(plugin_names)
                    except Exception as e:
                        logger.warning(
                            f"Failed to import plugins.{subpackage}.{module_name}: {e}"
                        )
            except ImportError as e:
                logger.debug(f"Could not import plugins.{subpackage} package: {e}")

        total = sum(len(v) for v in discovered.values())
        logger.info(f"Discovered {total} plugins: {discovered}")
        return discovered

    def _register_plugins_from_module(
        self, module: Any, allowed_types: list[PluginType]
    ) -> dict[PluginType, list[str]]:
        """Register all plugin classes from a module.

        Args:
            module: The module to scan for plugins.
            allowed_types: List of allowed plugin types for this module.

        Returns:
            Dictionary mapping plugin types to lists of registered plugin names.
        """
        registered: dict[PluginType, list[str]] = {t: [] for t in allowed_types}

        for attr_name in dir(module):
            attr = getattr(module, attr_name)

            # Check if it's a class that inherits from BasePlugin
            if (
                isinstance(attr, type)
                and issubclass(attr, BasePlugin)
                and attr is not BasePlugin
                and not attr_name.startswith("_")
                # Skip base classes from other modules
                and "Base" not in attr_name
            ):
                try:
                    metadata = attr.get_metadata()
                    # Verify plugin type is in allowed types
                    if metadata.plugin_type not in allowed_types:
                        allowed = [t.value for t in allowed_types]
                        logger.warning(
                            f"Plugin {attr_name} has type {metadata.plugin_type.value} "
                            f"but is in package that only allows {allowed}"
                        )
                        continue

                    self.register(attr)
                    registered[metadata.plugin_type].append(metadata.name)
                except Exception as e:
                    logger.warning(f"Failed to register {attr_name}: {e}")

        return registered

    def get_plugin_class(
        self, plugin_type: PluginType, name: str
    ) -> type[BasePlugin] | None:
        """Get a registered plugin class by type and name.

        Args:
            plugin_type: The plugin type.
            name: The plugin name.

        Returns:
            The plugin class, or None if not found.
        """
        return self._plugins[plugin_type].get(name)

    def get_all_classes(self, plugin_type: PluginType) -> dict[str, type[BasePlugin]]:
        """Get all registered plugin classes of a type.

        Args:
            plugin_type: The plugin type.

        Returns:
            Dictionary mapping plugin names to classes.
        """
        return dict(self._plugins[plugin_type])

    def get_metadata(self, plugin_type: PluginType, name: str) -> PluginMetadata | None:
        """Get metadata for a registered plugin.

        Args:
            plugin_type: The plugin type.
            name: The plugin name.

        Returns:
            PluginMetadata, or None if plugin not found.
        """
        plugin_class = self._plugins[plugin_type].get(name)
        if plugin_class:
            return plugin_class.get_metadata()
        return None

    def is_enabled(self, plugin_type: PluginType, name: str) -> bool:
        """Check if a plugin is enabled in settings.

        Args:
            plugin_type: The plugin type.
            name: The plugin name.

        Returns:
            True if enabled (default True if not in settings).
        """
        plugin_config = self._get_plugin_config(plugin_type, name)
        return cast(bool, plugin_config.get("enabled", True))

    def is_available(self, plugin_type: PluginType, name: str) -> bool:
        """Check if a plugin is available (has required config).

        Args:
            plugin_type: The plugin type.
            name: The plugin name.

        Returns:
            True if the plugin is available for use.
        """
        plugin_class = self._plugins[plugin_type].get(name)
        if not plugin_class:
            return False
        return plugin_class.is_available()

    def _get_plugin_config(self, plugin_type: PluginType, name: str) -> dict[str, Any]:
        """Get configuration for a plugin from Django settings.

        Args:
            plugin_type: The plugin type.
            name: The plugin name.

        Returns:
            Configuration dictionary (empty if not configured).
        """
        plugins_config = getattr(settings, "PLUGINS", {})
        type_config = plugins_config.get(plugin_type.value, {})
        return cast(dict[str, Any], type_config.get(name, {}))

    def get(
        self, plugin_type: PluginType, name: str, **init_kwargs: Any
    ) -> BasePlugin | None:
        """Get or create an instance of a plugin.

        Instances are cached for reuse (unless init_kwargs are provided).
        The plugin is configured with settings from PLUGINS.

        Args:
            plugin_type: The plugin type.
            name: The plugin name.
            **init_kwargs: Additional keyword arguments for plugin initialization.

        Returns:
            Configured plugin instance, or None if not found/unavailable.
        """
        instance_key = f"{plugin_type.value}:{name}"

        # Return cached instance if available and no init_kwargs
        if not init_kwargs and instance_key in self._instances:
            return self._instances[instance_key]

        plugin_class = self._plugins[plugin_type].get(name)
        if not plugin_class:
            logger.warning(
                f"Plugin '{name}' ({plugin_type.value}) not found in registry"
            )
            return None

        # Check availability (skip for source plugins as they need workspace config)
        if plugin_type != PluginType.SOURCE and not plugin_class.is_available():
            logger.info(
                f"Plugin '{name}' ({plugin_type.value}) not available (missing config?)"
            )
            return None

        # Create and configure instance
        try:
            instance = plugin_class(**init_kwargs)
            config = self._get_plugin_config(plugin_type, name).get("config", {})
            instance.configure(config)

            # Cache if no init_kwargs
            if not init_kwargs:
                self._instances[instance_key] = instance

            logger.debug(f"Created instance of {plugin_type.value} plugin '{name}'")
            return instance
        except Exception as e:
            logger.error(
                f"Failed to create instance of {plugin_type.value} plugin '{name}': {e}"
            )
            return None

    def get_enabled(self, plugin_type: PluginType) -> list[BasePlugin]:
        """Get all enabled and available plugin instances of a type.

        Returns plugins that are:
        - Registered in the registry
        - Enabled in settings (or not explicitly disabled)
        - Available (is_available() returns True)

        Plugins are sorted by priority (highest first).

        Args:
            plugin_type: The plugin type.

        Returns:
            List of configured plugin instances.
        """
        plugins: list[tuple[int, BasePlugin]] = []

        for name in self._plugins[plugin_type]:
            if not self.is_enabled(plugin_type, name):
                logger.debug(
                    f"Plugin '{name}' ({plugin_type.value}) is disabled in settings"
                )
                continue

            instance = self.get(plugin_type, name)
            if instance:
                # Get priority from settings or metadata
                config = self._get_plugin_config(plugin_type, name)
                priority = config.get("priority", instance.get_metadata().priority)
                plugins.append((priority, instance))

        # Sort by priority (highest first)
        plugins.sort(key=lambda x: x[0], reverse=True)

        return [plugin for _, plugin in plugins]

    def list_plugins(
        self, plugin_type: PluginType | None = None
    ) -> list[dict[str, Any]]:
        """List registered plugins with their status.

        Args:
            plugin_type: Optional filter by plugin type. If None, list all.

        Returns:
            List of dictionaries with plugin info.
        """
        result = []
        types_to_list = [plugin_type] if plugin_type else list(PluginType)

        for ptype in types_to_list:
            for name, plugin_class in self._plugins[ptype].items():
                metadata = plugin_class.get_metadata()
                result.append(
                    {
                        "name": metadata.name,
                        "display_name": metadata.display_name,
                        "type": metadata.plugin_type.value,
                        "version": metadata.version,
                        "description": metadata.description,
                        "capabilities": [cap.value for cap in metadata.capabilities],
                        "priority": metadata.priority,
                        "enabled": self.is_enabled(ptype, name),
                        "available": self.is_available(ptype, name),
                    }
                )
        return result


def register_plugin(cls: type[P]) -> type[P]:
    """Decorator to register a plugin class.

    Usage:
        @register_plugin
        class MyPlugin(BaseEnrichmentPlugin):
            ...

    Args:
        cls: The plugin class to register.

    Returns:
        The same class (for decorator chaining).
    """
    registry = PluginRegistry.instance()
    registry.register(cls)
    return cls

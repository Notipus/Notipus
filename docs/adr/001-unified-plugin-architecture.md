# ADR-001: Unified Plugin Architecture

**Status:** Accepted
**Date:** 2026-01-24
**Authors:** Team

## Context

Notipus currently has three types of extensible components:

1. **Enrichment providers** (e.g., Brandfetch) - Enrich customer data with company information
2. **Webhook sources** (e.g., Stripe, Shopify, Chargify) - Receive and process payment/order webhooks
3. **Notification destinations** (e.g., Slack) - Format and deliver notifications

Each type evolved independently with different patterns:

| Component Type | Location | Base Class | Registry Pattern |
|----------------|----------|------------|------------------|
| Enrichment | `app/core/providers/` | `BaseEnrichmentPlugin` | Auto-discovery singleton |
| Sources | `app/webhooks/providers/` | `PaymentProvider` | No registry (manual routing) |
| Destinations | `app/webhooks/formatters/` | `BaseFormatter` | Decorator-based registry |

This inconsistency makes it harder to:
- Add new plugins of any type
- Understand the codebase
- Maintain consistent configuration patterns
- Test plugins uniformly

## Decision

We will consolidate all plugin types under a unified architecture with:

1. **Single directory structure** at `app/plugins/`
2. **Unified registry** with type discrimination
3. **Shared base class** with type-specific extensions
4. **Consistent configuration** via a single `PLUGINS` Django setting

### Directory Structure

```
app/plugins/
├── __init__.py              # Package init, exposes registry
├── base.py                  # BasePlugin, PluginMetadata, PluginType
├── registry.py              # Unified PluginRegistry singleton
├── enrichment/
│   ├── __init__.py
│   ├── base.py              # BaseEnrichmentPlugin
│   └── brandfetch.py
├── sources/
│   ├── __init__.py
│   ├── base.py              # BaseSourcePlugin
│   ├── stripe.py
│   ├── shopify.py
│   └── chargify.py
└── destinations/
    ├── __init__.py
    ├── base.py              # BaseDestinationPlugin
    └── slack.py
```

### Plugin Type Hierarchy

```
BasePlugin (abstract)
├── BaseEnrichmentPlugin
│   └── BrandfetchPlugin
├── BaseSourcePlugin
│   ├── StripeSourcePlugin
│   ├── ShopifySourcePlugin
│   └── ChargifySourcePlugin
└── BaseDestinationPlugin
    └── SlackDestinationPlugin
```

### Unified Registry

```python
from enum import Enum


class PluginType(Enum):
    ENRICHMENT = "enrichment"
    SOURCE = "source"
    DESTINATION = "destination"


class PluginRegistry:
    """Singleton registry for all plugin types."""

    _instance: "PluginRegistry | None" = None

    def __init__(self) -> None:
        self._plugins: dict[PluginType, dict[str, type[BasePlugin]]] = {
            PluginType.ENRICHMENT: {},
            PluginType.SOURCE: {},
            PluginType.DESTINATION: {},
        }
        self._instances: dict[str, BasePlugin] = {}

    @classmethod
    def instance(cls) -> "PluginRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, plugin_class: type[BasePlugin]) -> type[BasePlugin]:
        """Register a plugin. Can be used as a decorator."""
        metadata = plugin_class.get_metadata()
        self._plugins[metadata.plugin_type][metadata.name] = plugin_class
        return plugin_class

    def discover(self) -> None:
        """Auto-discover plugins in app.plugins subpackages."""
        ...

    def get(self, plugin_type: PluginType, name: str) -> BasePlugin | None:
        """Get a configured plugin instance."""
        ...

    def get_all(self, plugin_type: PluginType) -> list[BasePlugin]:
        """Get all enabled plugins of a type, sorted by priority."""
        ...
```

### Configuration

Single `PLUGINS` setting in Django settings:

```python
PLUGINS = {
    "enrichment": {
        "brandfetch": {
            "enabled": True,
            "priority": 100,
            "config": {
                "api_key": os.environ.get("BRANDFETCH_API_KEY", ""),
                "base_url": "https://api.brandfetch.io/v2",
                "timeout": 10,
            },
        },
    },
    "sources": {
        "stripe": {"enabled": True},
        "shopify": {"enabled": True},
        "chargify": {"enabled": True},
    },
    "destinations": {
        "slack": {"enabled": True},
    },
}
```

### Plugin Contracts

#### BasePlugin (all plugins)

```python
class BasePlugin(ABC):
    @classmethod
    @abstractmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata including type, name, version."""
        ...

    @classmethod
    def is_available(cls) -> bool:
        """Check if plugin can be used (has required config, etc.)."""
        return True

    def configure(self, config: dict[str, Any]) -> None:
        """Configure plugin with settings from PLUGINS config."""
        self.config = config
```

#### BaseEnrichmentPlugin

```python
class BaseEnrichmentPlugin(BasePlugin):
    @abstractmethod
    def enrich_domain(self, domain: str) -> dict[str, Any]:
        """Enrich a domain with company data."""
        ...
```

#### BaseSourcePlugin

```python
class BaseSourcePlugin(BasePlugin):
    @abstractmethod
    def validate_webhook(self, request: HttpRequest) -> bool:
        """Validate webhook signature."""
        ...

    @abstractmethod
    def parse_webhook(self, request: HttpRequest, **kwargs) -> dict[str, Any] | None:
        """Parse webhook into normalized event format."""
        ...

    def get_customer_data(self, customer_id: str) -> dict[str, Any]:
        """Retrieve customer data from source."""
        return {}
```

#### BaseDestinationPlugin

```python
class BaseDestinationPlugin(BasePlugin):
    @abstractmethod
    def format(self, notification: RichNotification) -> Any:
        """Format notification for this destination."""
        ...

    @abstractmethod
    def send(self, formatted: Any, credentials: dict[str, Any]) -> bool:
        """Send formatted notification to destination."""
        ...
```

## Consequences

### Positive

- **Consistency**: Single pattern for all plugin types makes the codebase easier to understand
- **Discoverability**: Auto-discovery means adding a new plugin is just creating a file in the right directory
- **Configuration**: Unified `PLUGINS` setting provides one place for all plugin configuration
- **Extensibility**: Clear contracts for adding new sources (Paddle, LemonSqueezy), destinations (Discord, Email, Teams), or enrichment providers (Clearbit, Apollo)
- **Testability**: Unified registry makes mocking easier; one pattern to test

### Neutral

- **Source plugins remain workspace-scoped**: While the registry is global, source plugins (webhooks) still need per-workspace secrets stored in the `Integration` model
- **Destination plugins have dual config**: Global enablement in `PLUGINS`, per-workspace credentials in `Integration` model

## Alternatives Considered

### Keep Current Structure, Unify Interfaces Only

- **Rejected because**: Still leaves code scattered across different directories, making it harder to find and understand all plugins

### Separate Registries per Plugin Type

- **Rejected because**: Adds complexity without benefit; a unified registry with type discrimination is simpler and provides a single entry point for plugin management

## References

- [Plugin Pattern](https://refactoring.guru/design-patterns/strategy)
- [Service Locator Pattern](https://martinfowler.com/articles/injection.html#UsingAServiceLocator)

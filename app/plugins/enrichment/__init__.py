"""Enrichment plugins for domain/company and email/person data enrichment.

Enrichment plugins retrieve information from external APIs:

- Domain enrichment: Company information (logos, descriptions, industry)
- Email enrichment: Person information (name, job title, social profiles)

Usage:
    from plugins.enrichment import BaseEnrichmentPlugin, BaseEmailEnrichmentPlugin
    from plugins import PluginRegistry, PluginType

    # Get all enabled domain enrichment plugins
    registry = PluginRegistry.instance()
    plugins = registry.get_enabled(PluginType.ENRICHMENT)

    # Enrich a domain
    for plugin in plugins:
        data = plugin.enrich_domain("example.com")

    # Get email enrichment plugins
    email_plugins = registry.get_enabled(PluginType.EMAIL_ENRICHMENT)

    # Enrich an email (requires workspace API key)
    for plugin in email_plugins:
        data = plugin.enrich_email("john@example.com", api_key="...")
"""

from plugins.enrichment.base import BaseEnrichmentPlugin, EnrichmentCapability
from plugins.enrichment.base_email import (
    BaseEmailEnrichmentPlugin,
    EmailEnrichmentCapability,
    EmailEnrichmentError,
    EmailNotFoundError,
    GDPRClaimedError,
    RateLimitError,
)

__all__ = [
    # Domain enrichment
    "BaseEnrichmentPlugin",
    "EnrichmentCapability",
    # Email enrichment
    "BaseEmailEnrichmentPlugin",
    "EmailEnrichmentCapability",
    "EmailEnrichmentError",
    "EmailNotFoundError",
    "GDPRClaimedError",
    "RateLimitError",
]

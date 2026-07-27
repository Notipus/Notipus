"""Destination plugins for notification delivery.

Destination plugins format and deliver notifications to various platforms
(Slack, Email, Discord, Telegram, etc.).

Usage:
    from plugins.destinations import BaseDestinationPlugin
    from plugins import PluginRegistry, PluginType

    # Get a specific destination plugin
    registry = PluginRegistry.instance()
    slack_plugin = registry.get(PluginType.DESTINATION, "slack")
    telegram_plugin = registry.get(PluginType.DESTINATION, "telegram")

    # Format and send a notification
    formatted = slack_plugin.format(notification)
    success = slack_plugin.send(formatted, credentials={"webhook_url": "..."})
"""

from plugins.destinations.base import BaseDestinationPlugin
from plugins.destinations.telegram import TelegramDestinationPlugin

__all__ = [
    "BaseDestinationPlugin",
    "TelegramDestinationPlugin",
]

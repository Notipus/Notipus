"""Base class for destination plugins.

Destination plugins format and deliver notifications to various platforms
(Slack, Email, Discord, etc.).
"""

import logging
from abc import abstractmethod
from typing import Any

from plugins.base import BasePlugin, PluginMetadata
from webhooks.models.rich_notification import RichNotification

logger = logging.getLogger(__name__)


class BaseDestinationPlugin(BasePlugin):
    """Base class for destination plugins.

    Destination plugins are responsible for:
    1. Formatting RichNotification objects into platform-specific formats
    2. Sending formatted notifications to the destination platform

    Subclasses must implement:
    - get_metadata(): Return plugin metadata with plugin_type=DESTINATION
    - format(): Convert RichNotification to platform-specific format
    - send(): Deliver the formatted notification

    Note: The send() method returns a dict with {"success": bool, ...} to support
    additional response metadata like thread_ts for Slack threading. This replaced
    the previous bool return type.

    Example:
        class MyDestinationPlugin(BaseDestinationPlugin):
            @classmethod
            def get_metadata(cls) -> PluginMetadata:
                return PluginMetadata(
                    name="my_destination",
                    display_name="My Destination",
                    version="1.0.0",
                    description="Sends notifications to My Destination",
                    plugin_type=PluginType.DESTINATION,
                )

            def format(self, notification: RichNotification) -> dict[str, Any]:
                # Format for the platform
                return {"text": notification.headline}

            def send(self, formatted: Any, credentials: dict[str, Any]) -> dict:
                # Send to the platform and return result
                return {"success": True, "message_id": "..."}
    """

    @classmethod
    @abstractmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Must set plugin_type=PluginType.DESTINATION.

        Returns:
            PluginMetadata describing this destination plugin.
        """
        pass

    @abstractmethod
    def format(self, notification: RichNotification) -> Any:
        """Format a notification for this destination platform.

        Args:
            notification: RichNotification to format.

        Returns:
            Platform-specific format (dict for Slack, str for email, etc.).
        """
        pass

    @abstractmethod
    def send(
        self,
        formatted: Any,
        credentials: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a formatted notification to the destination.

        Args:
            formatted: Platform-specific formatted notification.
            credentials: Destination-specific credentials (webhook_url, api_key, etc.).
            options: Optional send options (e.g., thread_ts for threading).

        Returns:
            Result dict with at minimum {"success": bool}.
            May include additional fields like thread_ts, channel, message_ts.

        Raises:
            ValueError: If required credentials are missing.
            RuntimeError: If the send operation fails.
        """
        pass

    def format_and_send(
        self,
        notification: RichNotification,
        credentials: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Format and send a notification in one call.

        Convenience method that combines format() and send().

        Args:
            notification: RichNotification to send.
            credentials: Destination-specific credentials.
            options: Optional send options (e.g., thread_ts for threading).

        Returns:
            Result dict with at minimum {"success": bool}.
        """
        formatted = self.format(notification)
        return self.send(formatted, credentials, options)

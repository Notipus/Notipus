"""Shared per-workspace lookups for notification destination credentials.

Both the immediate router (:mod:`webhooks.webhook_router`) and the delayed
delivery path (:class:`webhooks.services.pending_event_queue.PendingEventQueue`)
resolve the same per-workspace credentials. Keeping the logic here means the
two paths can't drift apart (different filtering, log levels, or field names)
as more destinations are added.
"""

import logging

from core.models import Integration, Workspace

logger = logging.getLogger(__name__)


def get_telegram_credentials(workspace: Workspace | None) -> dict[str, str] | None:
    """Return the Telegram bot credentials for a workspace, if configured.

    Args:
        workspace: The workspace to look up, or None.

    Returns:
        Dict with ``bot_token`` and ``chat_id`` when an active Telegram
        integration has both, otherwise None.
    """
    if not workspace:
        return None

    try:
        integration = Integration.objects.get(
            workspace=workspace,
            integration_type="telegram_notifications",
            is_active=True,
        )
    except Integration.DoesNotExist:
        logger.debug(
            f"No active Telegram integration found for workspace {workspace.uuid}"
        )
        return None

    bot_token = integration.oauth_credentials.get("bot_token")
    chat_id = integration.oauth_credentials.get("chat_id")
    if bot_token and chat_id:
        return {"bot_token": bot_token, "chat_id": chat_id}
    return None

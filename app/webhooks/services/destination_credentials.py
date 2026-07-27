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


def get_teams_credentials(workspace: Workspace | None) -> dict[str, str] | None:
    """Return the Microsoft Teams webhook URL for a workspace, if configured.

    The credential is a Power Automate "Workflows" incoming-webhook URL
    (the successor to the retiring Office 365 connector webhooks). It is a
    bearer secret — treat it like the Slack webhook URL.

    Args:
        workspace: The workspace to look up, or None.

    Returns:
        Dict with ``webhook_url`` when an active Teams integration has one,
        otherwise None.
    """
    if not workspace:
        return None

    try:
        integration = Integration.objects.get(
            workspace=workspace,
            integration_type="teams_notifications",
            is_active=True,
        )
    except Integration.DoesNotExist:
        logger.debug(
            f"No active Teams integration found for workspace {workspace.uuid}"
        )
        return None

    webhook_url = integration.oauth_credentials.get("webhook_url")
    if webhook_url:
        return {"webhook_url": webhook_url}
    return None


def get_slack_credentials(workspace: Workspace | None) -> dict[str, str] | None:
    """Return the Slack incoming-webhook credential for a workspace, if set.

    The webhook URL lives at ``oauth_credentials["incoming_webhook"]["url"]``
    and is a bearer secret — treat it like the Teams webhook URL.

    Args:
        workspace: The workspace to look up, or None.

    Returns:
        Dict with ``webhook_url`` when an active Slack integration has one,
        otherwise None.
    """
    if not workspace:
        return None

    try:
        integration = Integration.objects.get(
            workspace=workspace,
            integration_type="slack_notifications",
            is_active=True,
        )
    except Integration.DoesNotExist:
        logger.debug(
            f"No active Slack integration found for workspace {workspace.uuid}"
        )
        return None

    webhook_url = integration.oauth_credentials.get("incoming_webhook", {}).get("url")
    if webhook_url:
        return {"webhook_url": webhook_url}
    return None


def collect_destinations(
    workspace: Workspace | None,
) -> list[tuple[str, dict[str, str]]]:
    """Return every destination a workspace has enabled.

    Shared by the immediate (:mod:`webhooks.webhook_router`) and delayed
    (:class:`webhooks.services.pending_event_queue.PendingEventQueue`)
    delivery paths so the two can't drift in which destinations they attempt,
    the order they attempt them, or the credential shape they pass.

    Args:
        workspace: The workspace to look up, or None.

    Returns:
        A list of ``(plugin_name, credentials)`` tuples, one per configured
        destination, in a stable order (slack, telegram, teams).
    """
    destinations: list[tuple[str, dict[str, str]]] = []
    slack = get_slack_credentials(workspace)
    if slack:
        destinations.append(("slack", slack))
    telegram = get_telegram_credentials(workspace)
    if telegram:
        destinations.append(("telegram", telegram))
    teams = get_teams_credentials(workspace)
    if teams:
        destinations.append(("teams", teams))
    return destinations

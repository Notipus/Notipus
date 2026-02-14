"""Thread mapping service for managing ticket-to-Slack-thread mappings.

This service provides an abstraction layer for managing the relationship
between external entities (like Zendesk tickets) and their corresponding
Slack message threads, enabling threaded updates for ongoing conversations.
"""

import logging
from dataclasses import dataclass

from core.models import Workspace
from webhooks.models import SlackThreadMapping

logger = logging.getLogger(__name__)


@dataclass
class ThreadInfo:
    """Information about a Slack thread.

    Attributes:
        channel_id: Slack channel ID where the thread exists.
        thread_ts: Slack thread timestamp (message ID).
    """

    channel_id: str
    thread_ts: str


class ThreadMappingService:
    """Service for managing ticket-to-Slack-thread mappings.

    This service provides CRUD operations for SlackThreadMapping records,
    enabling threaded notifications for support tickets and other entities.
    """

    def get_thread_ts(
        self,
        workspace: Workspace,
        entity_type: str,
        entity_id: str,
    ) -> ThreadInfo | None:
        """Lookup existing thread for an entity.

        Args:
            workspace: The workspace to search in.
            entity_type: Type of entity (e.g., "zendesk_ticket").
            entity_id: External identifier for the entity.

        Returns:
            ThreadInfo if found, None otherwise.
        """
        try:
            mapping = SlackThreadMapping.objects.get(
                workspace=workspace,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            return ThreadInfo(
                channel_id=mapping.slack_channel_id,
                thread_ts=mapping.slack_thread_ts,
            )
        except SlackThreadMapping.DoesNotExist:
            return None

    def store_thread_ts(
        self,
        workspace: Workspace,
        entity_type: str,
        entity_id: str,
        channel_id: str,
        thread_ts: str,
    ) -> SlackThreadMapping:
        """Store a new thread mapping.

        Args:
            workspace: The workspace this mapping belongs to.
            entity_type: Type of entity (e.g., "zendesk_ticket").
            entity_id: External identifier for the entity.
            channel_id: Slack channel ID where the thread exists.
            thread_ts: Slack thread timestamp (message ID).

        Returns:
            The created SlackThreadMapping instance.
        """
        mapping = SlackThreadMapping.objects.create(
            workspace=workspace,
            entity_type=entity_type,
            entity_id=entity_id,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
        )
        logger.info(
            f"Stored thread mapping: {entity_type}:{entity_id} -> "
            f"{channel_id}/{thread_ts}"
        )
        return mapping

    def update_thread_ts(
        self,
        workspace: Workspace,
        entity_type: str,
        entity_id: str,
        thread_ts: str,
    ) -> bool:
        """Update an existing thread mapping's timestamp.

        Args:
            workspace: The workspace this mapping belongs to.
            entity_type: Type of entity (e.g., "zendesk_ticket").
            entity_id: External identifier for the entity.
            thread_ts: New Slack thread timestamp.

        Returns:
            True if update was successful, False if mapping not found.
        """
        updated = SlackThreadMapping.objects.filter(
            workspace=workspace,
            entity_type=entity_type,
            entity_id=entity_id,
        ).update(slack_thread_ts=thread_ts)

        if updated:
            logger.info(
                f"Updated thread mapping: {entity_type}:{entity_id} -> {thread_ts}"
            )
        return updated > 0

    def get_or_create_thread(
        self,
        workspace: Workspace,
        entity_type: str,
        entity_id: str,
        channel_id: str,
        thread_ts: str | None = None,
    ) -> tuple[ThreadInfo | None, bool]:
        """Get existing thread or create a new mapping.

        This is a convenience method that combines lookup and creation.
        If a mapping exists, returns the existing thread info.
        If thread_ts is provided and no mapping exists, creates a new mapping.

        Uses Django's get_or_create for atomic operation to avoid race conditions.

        Args:
            workspace: The workspace this mapping belongs to.
            entity_type: Type of entity (e.g., "zendesk_ticket").
            entity_id: External identifier for the entity.
            channel_id: Slack channel ID (used for creation).
            thread_ts: Slack thread timestamp (used for creation, optional).

        Returns:
            Tuple of (ThreadInfo or None, was_created).
            If thread_ts is None and no mapping exists, returns (None, False).
        """
        if not thread_ts:
            # Can't create without thread_ts, just try to get existing
            existing = self.get_thread_ts(workspace, entity_type, entity_id)
            return existing, False

        # Use atomic get_or_create to avoid race conditions
        mapping, created = SlackThreadMapping.objects.get_or_create(
            workspace=workspace,
            entity_type=entity_type,
            entity_id=entity_id,
            defaults={
                "slack_channel_id": channel_id,
                "slack_thread_ts": thread_ts,
            },
        )

        if created:
            logger.info(
                f"Created thread mapping: {entity_type}:{entity_id} -> "
                f"{channel_id}/{thread_ts}"
            )

        return ThreadInfo(
            channel_id=mapping.slack_channel_id,
            thread_ts=mapping.slack_thread_ts,
        ), created

    def delete_thread_mapping(
        self,
        workspace: Workspace,
        entity_type: str,
        entity_id: str,
    ) -> bool:
        """Delete a thread mapping.

        Args:
            workspace: The workspace this mapping belongs to.
            entity_type: Type of entity (e.g., "zendesk_ticket").
            entity_id: External identifier for the entity.

        Returns:
            True if deletion was successful, False if mapping not found.
        """
        deleted, _ = SlackThreadMapping.objects.filter(
            workspace=workspace,
            entity_type=entity_type,
            entity_id=entity_id,
        ).delete()

        if deleted:
            logger.info(f"Deleted thread mapping: {entity_type}:{entity_id}")
        return deleted > 0


# Singleton instance for convenience
thread_mapping_service = ThreadMappingService()

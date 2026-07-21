import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class WebhooksConfig(AppConfig):
    """Django app configuration for webhooks."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "webhooks"
    label = "webhooks"

    def ready(self) -> None:
        """Called when Django starts - recover orphaned webhook events.

        On ephemeral infrastructure, servers can die at any time. When a new
        server starts, we check Redis for any pending webhook events that
        were queued by a previous server instance and process them, then
        start the periodic recovery sweep that retries failed deliveries
        for as long as the events' retry window lasts.

        This prevents notification loss during deployments, restarts, and
        destination (Slack) outages.
        """
        import os

        # Skip during migrations, tests, or management commands
        # RUN_MAIN is set by Django's runserver to avoid double execution
        if os.environ.get("RUN_MAIN") != "true":
            # Check if we're in a context where recovery should run
            # (production server, not runserver's outer process)
            import sys

            # Skip for management commands except runserver
            if len(sys.argv) > 1 and sys.argv[1] not in ("runserver", "gunicorn"):
                return

            # For non-runserver production (gunicorn/uvicorn), run recovery
            if "runserver" in sys.argv:
                return  # Let the inner process handle it

        self._recover_orphaned_events()
        self._start_periodic_recovery()

    def _recover_orphaned_events(self) -> None:
        """Recover orphaned events from Redis."""
        try:
            from webhooks.services.pending_event_queue import pending_event_queue

            count = pending_event_queue.recover_orphaned_events()
            if count > 0:
                logger.info(
                    f"Startup recovery: processed {count} orphaned webhook event groups"
                )
        except Exception as e:
            # Don't prevent server startup if recovery fails
            logger.error(f"Failed to recover orphaned events on startup: {e}")

    def _start_periodic_recovery(self) -> None:
        """Start the background sweep that retries undelivered events."""
        try:
            from webhooks.services.pending_event_queue import pending_event_queue

            pending_event_queue.start_periodic_recovery()
        except Exception as e:
            # Don't prevent server startup if the sweeper fails to start
            logger.error(f"Failed to start periodic webhook recovery: {e}")

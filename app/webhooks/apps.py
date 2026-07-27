import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# Executable basenames that serve HTTP traffic. Recovery must run in
# these processes; production runs uvicorn (see Dockerfile CMD).
_SERVER_EXECUTABLES = ("gunicorn", "uvicorn", "daphne", "hypercorn")


def _is_serving_process() -> bool:
    """Return True when this process will serve HTTP traffic.

    Recovery and the periodic retry sweep must run in serving processes
    only - not in tests, migrations, or other management commands. The
    decision is based on the executable name rather than positional
    arguments: under uvicorn/gunicorn, ``sys.argv[1]`` is a flag or the
    app module (e.g. "--host" or "django_notipus.asgi:application"), so
    argument-based checks wrongly skip production servers.

    Returns:
        True for ASGI/WSGI servers and runserver's serving process.
    """
    argv0 = os.path.basename(sys.argv[0])

    if any(server in argv0 for server in _SERVER_EXECUTABLES):
        return True

    if "manage.py" in argv0 or "django-admin" in argv0:
        if len(sys.argv) > 1 and sys.argv[1] == "runserver":
            # With autoreload, ready() fires in both the watcher and the
            # serving child; only the child (RUN_MAIN=true) serves. With
            # --noreload there is a single process and no RUN_MAIN.
            return os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv
        return False

    # Anything else (pytest, mypy plugins, scripts importing Django, ...)
    return False


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
        if not _is_serving_process():
            return

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

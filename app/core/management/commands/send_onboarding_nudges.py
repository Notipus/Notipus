"""Email workspaces that have a notification channel but no event source.

Intended to run on a schedule. Sending is one-shot per workspace, so
running it more often than needed costs nothing but a query.

    python manage.py send_onboarding_nudges --dry-run
    python manage.py send_onboarding_nudges --limit 5
    python manage.py send_onboarding_nudges
"""

from typing import Any

from core.services.onboarding_nudge import (
    DEFAULT_MIN_AGE_HOURS,
    find_stalled_workspaces,
    send_pending_nudges,
)
from core.services.recipients import admin_emails
from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    """Send the half-finished-setup reminder."""

    help = "Email workspaces that have a notification channel but no event source"

    def add_arguments(self, parser: CommandParser) -> None:
        """Register command-line flags.

        Args:
            parser: Argument parser supplied by Django.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List who would be emailed without sending anything",
        )
        parser.add_argument(
            "--min-age-hours",
            type=int,
            default=DEFAULT_MIN_AGE_HOURS,
            help=(
                "Leave workspaces younger than this alone "
                f"(default: {DEFAULT_MIN_AGE_HOURS})"
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Send at most this many, for a cautious first run",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the command.

        Args:
            *args: Unused positional arguments.
            **options: Parsed command-line options.
        """
        min_age_hours: int = options["min_age_hours"]
        limit: int | None = options["limit"]

        if options["dry_run"]:
            workspaces = find_stalled_workspaces(min_age_hours)
            if limit is not None:
                workspaces = workspaces[:limit]

            count = 0
            for workspace in workspaces:
                recipients = admin_emails(workspace) or ["(no admin address)"]
                self.stdout.write(
                    f"{workspace.name} [{workspace.subscription_plan}] "
                    f"created {workspace.created_at:%Y-%m-%d} "
                    f"-> {', '.join(recipients)}"
                )
                count += 1

            self.stdout.write(self.style.WARNING(f"Dry run: {count} would be emailed"))
            return

        sent = send_pending_nudges(min_age_hours=min_age_hours, limit=limit)
        self.stdout.write(self.style.SUCCESS(f"Nudged {sent} workspace(s)"))

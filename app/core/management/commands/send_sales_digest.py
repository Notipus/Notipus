"""Post the weekly / monthly sales snapshot to opted-in workspaces.

Run hourly. Each workspace's digest goes out once, after 09:00 local time
on Monday (weekly) or the 1st (monthly), using the workspace timezone:

    python manage.py send_sales_digest
    python manage.py send_sales_digest --dry-run
    python manage.py send_sales_digest --workspace <uuid> --period week --force
"""

from typing import Any

from core.models import Workspace
from core.services.sales_digest import due_digests, send_digest
from core.services.sales_stats import PERIODS, archive_closed_periods
from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    """Send due sales digests."""

    help = "Post the weekly and monthly sales snapshot to opted-in workspaces"

    def add_arguments(self, parser: CommandParser) -> None:
        """Register command-line flags.

        Args:
            parser: Argument parser supplied by Django.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the digests that are due, but store and send nothing",
        )
        parser.add_argument(
            "--workspace",
            help="Only this workspace (UUID); with --period, ignores the schedule",
        )
        parser.add_argument(
            "--period",
            choices=PERIODS,
            help="With --workspace: send this period's digest now",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even if the digest for the period already went out",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the command.

        Args:
            *args: Unused positional arguments.
            **options: Parsed command-line options.
        """
        workspaces = Workspace.objects.all()
        if options["workspace"]:
            workspaces = workspaces.filter(uuid=options["workspace"])

        if not options["dry_run"]:
            archive_closed_periods(workspaces)

        if options["workspace"] and options["period"]:
            pairs = [(workspace, options["period"]) for workspace in workspaces]
        else:
            opted_in = workspaces.filter(sales_digest_weekly=True) | workspaces.filter(
                sales_digest_monthly=True
            )
            pairs = due_digests(opted_in.distinct())

        counts: dict[str, int] = {}
        for workspace, kind in pairs:
            result = send_digest(
                workspace, kind, force=options["force"], dry_run=options["dry_run"]
            )
            counts[result] = counts.get(result, 0) + 1
            self.stdout.write(f"{workspace.uuid} {kind}: {result}")

        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none due"
        self.stdout.write(self.style.SUCCESS(f"Sales digests: {summary}"))

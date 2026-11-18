"""Delete sales-ledger rows and snapshots past their retention.

Run daily. Raw transaction rows (which hold encrypted customer details)
live for ``RETENTION_DAYS`` (35); PII-free period snapshots for ~13 months.

    python manage.py purge_sales_ledger
"""

from typing import Any

from core.services.sales_ledger import RETENTION_DAYS, purge_expired
from core.services.sales_stats import SNAPSHOT_RETENTION_DAYS, purge_old_snapshots
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Enforce sales-ledger retention."""

    help = "Delete sales ledger rows and snapshots past their retention window"

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the command.

        Args:
            *args: Unused positional arguments.
            **options: Parsed command-line options.
        """
        events = purge_expired()
        snapshots = purge_old_snapshots()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {events} ledger rows older than {RETENTION_DAYS} days and "
                f"{snapshots} snapshots older than {SNAPSHOT_RETENTION_DAYS} days"
            )
        )

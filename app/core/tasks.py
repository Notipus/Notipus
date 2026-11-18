"""Scheduled jobs for the sales snapshot (run by Celery beat).

The schedule lives in ``CELERY_BEAT_SCHEDULE`` in settings. Both tasks
are safe to run more often than scheduled: digests are sent once per
workspace and period (``SalesSnapshot.digest_sent_at``), and purging
only deletes what is past retention.
"""

import logging

from celery import shared_task
from django.core.cache import cache
from django.db.models import Q

logger = logging.getLogger(__name__)

# Longer than any realistic run, shorter than the 15-minute schedule.
_DIGEST_LOCK_SECONDS = 60 * 14


@shared_task(ignore_result=True)
def send_due_sales_digests() -> dict[str, str]:
    """Send every opted-in workspace's digest that is due now.

    Returns:
        ``{"<workspace>:<period>": outcome}`` for each digest attempted.
    """
    from core.models import Workspace
    from core.services.sales_digest import due_digests, send_digest

    # Overlapping runs would race on the same digest before
    # digest_sent_at is written.
    if not cache.add("tasks:send_due_sales_digests", 1, _DIGEST_LOCK_SECONDS):
        return {}
    try:
        opted_in = Workspace.objects.filter(
            Q(sales_digest_weekly=True) | Q(sales_digest_monthly=True)
        )
        results: dict[str, str] = {}
        for workspace, kind in due_digests(opted_in):
            try:
                results[f"{workspace.uuid}:{kind}"] = send_digest(workspace, kind)
            except Exception:
                logger.exception("Sales digest failed for workspace %s", workspace.uuid)
                results[f"{workspace.uuid}:{kind}"] = "error"
        return results
    finally:
        cache.delete("tasks:send_due_sales_digests")


@shared_task(ignore_result=True)
def purge_sales_ledger() -> dict[str, int]:
    """Delete ledger rows and snapshots past their retention.

    Returns:
        Counts of deleted ledger rows and snapshots.
    """
    from core.services.sales_ledger import purge_expired
    from core.services.sales_stats import purge_old_snapshots

    counts = {"events": purge_expired(), "snapshots": purge_old_snapshots()}
    logger.info("Sales ledger retention: %s", counts)
    return counts

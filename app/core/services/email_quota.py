"""Per-workspace daily caps on emails a workspace can make us send.

Invitations and setup-instruction emails go to any address an admin
types, so without a cap a single workspace could use the app as a mail
cannon, burning sender reputation for everyone. Counters live in the
Django cache (Redis in production) and reset each UTC day.
"""

from django.core.cache import cache
from django.utils import timezone

# Emails of one kind a workspace may send per UTC day.
DAILY_EMAIL_CAP = 20

# Counters outlive their day slightly so a late increment still expires.
_COUNTER_TTL_SECONDS = 2 * 24 * 60 * 60

# Kinds of capped email.
INVITATION = "invitation"
SETUP_INSTRUCTIONS = "setup_instructions"


def _counter_key(workspace_id: int, kind: str) -> str:
    """Cache key for today's counter.

    Args:
        workspace_id: Workspace primary key.
        kind: Email kind (``INVITATION`` or ``SETUP_INSTRUCTIONS``).

    Returns:
        The cache key.
    """
    day = timezone.now().date().isoformat()
    return f"email-quota:{kind}:{workspace_id}:{day}"


def consume_daily_email_quota(workspace_id: int, kind: str) -> bool:
    """Record one email of ``kind`` for the workspace if under today's cap.

    Args:
        workspace_id: Workspace primary key.
        kind: Email kind (``INVITATION`` or ``SETUP_INSTRUCTIONS``).

    Returns:
        True if the email may be sent; False once the cap is reached.
    """
    key = _counter_key(workspace_id, kind)
    cache.add(key, 0, _COUNTER_TTL_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:
        # The counter expired between add and incr; start it again.
        cache.add(key, 1, _COUNTER_TTL_SECONDS)
        count = 1
    return int(count) <= DAILY_EMAIL_CAP

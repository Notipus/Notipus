"""Record billing events into the short-lived sales ledger.

Every parsed webhook event that moves money (or ends a customer
relationship) becomes one :class:`core.models.TransactionEvent`. The ledger
is recorded per event, before the Stripe pending-event queue merges related
events into a single notification, so a declined first attempt and its
later success are both kept for the payment-recovery statistics.

SIGNAL CONTRACT (same rule as ``webhooks.services.insight_detector``): new
vs existing is only ever set from a field the provider actually sends.
Nothing is inferred from absence; unprovable events are ``unknown`` and the
snapshot shows them as their own "Unclassified" bucket.

* Stripe: invoice ``billing_reason`` ``subscription_create`` -> new;
  ``subscription_cycle`` / ``subscription_update`` /
  ``subscription_threshold`` -> existing; a trial conversion -> new.
* Chargify: ``is_signup_payment`` -> new; ``is_renewal`` (renewal_success /
  renewal_failure) -> existing.
* Shopify: customer ``orders_count`` == 1 -> new, > 1 -> existing;
  subscription billing attempts -> existing.
* Cancellations are always existing: only a customer can cancel.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from core.models import TransactionEvent
from core.utils.email_domain import extract_domain, is_enrichable_domain
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

if TYPE_CHECKING:
    from core.models import Workspace

logger = logging.getLogger(__name__)

# How long raw ledger rows live. Covers a full calendar month (31 days) plus
# a few days for a monthly digest that runs late.
RETENTION_DAYS = 35

_HMAC_SALT = "notipus.sales_ledger"

# Typed aliases: mypy runs without the Django plugin, so model attributes
# are Any to it.
KIND_SALE: str = TransactionEvent.KIND_SALE
KIND_FAILURE: str = TransactionEvent.KIND_FAILURE
KIND_REFUND: str = TransactionEvent.KIND_REFUND
KIND_CANCELLATION: str = TransactionEvent.KIND_CANCELLATION
KIND_TRIAL_STARTED: str = TransactionEvent.KIND_TRIAL_STARTED
SEGMENT_NEW: str = TransactionEvent.SEGMENT_NEW
SEGMENT_EXISTING: str = TransactionEvent.SEGMENT_EXISTING
SEGMENT_UNKNOWN: str = TransactionEvent.SEGMENT_UNKNOWN

SALE_EVENT_TYPES = frozenset(
    {"payment_success", "invoice_paid", "subscription_renewed"}
)
FAILURE_EVENT_TYPES = frozenset({"payment_failure", "payment_action_required"})
CANCELLATION_EVENT_TYPES = frozenset(
    {"subscription_canceled", "subscription_deleted", "customer_churned"}
)

_NEW_BILLING_REASONS = frozenset({"subscription_create"})
_EXISTING_BILLING_REASONS = frozenset(
    {"subscription_cycle", "subscription_update", "subscription_threshold"}
)


def keyed_hash(*parts: Any) -> str:
    """Return a stable, non-reversible reference for identifying values.

    Args:
        *parts: Values joined into the hashed message.

    Returns:
        A 64-character hex HMAC-SHA256 keyed with ``SECRET_KEY``.
    """
    message = "\x1f".join(str(part) for part in parts)
    return salted_hmac(_HMAC_SALT, message, algorithm="sha256").hexdigest()


def classify_kind(event_data: dict[str, Any]) -> str | None:
    """Map a normalized event to a ledger kind.

    Args:
        event_data: Parsed event data.

    Returns:
        A ``TransactionEvent.KIND_*`` value, or None when the event is not
        part of the sales picture (fulfilment, usage, a zero-amount
        subscription change, ...).
    """
    event_type = event_data.get("type", "")
    metadata = event_data.get("metadata") or {}
    if event_type in SALE_EVENT_TYPES:
        return KIND_SALE
    # Chargify reports a paid signup as subscription_created with the
    # revenue the signup collected; it is the customer's first sale.
    if event_type == "subscription_created" and metadata.get("is_signup_payment"):
        return KIND_SALE
    if event_type in FAILURE_EVENT_TYPES:
        return KIND_FAILURE
    if event_type == "refund_issued":
        return KIND_REFUND
    if event_type in CANCELLATION_EVENT_TYPES:
        return KIND_CANCELLATION
    if event_type == "trial_started":
        return KIND_TRIAL_STARTED
    return None


def classify_segment(
    event_data: dict[str, Any], customer_data: dict[str, Any], kind: str
) -> str:
    """Decide new vs existing from signals the provider actually sent.

    Args:
        event_data: Parsed event data.
        customer_data: Customer data from the same payload.
        kind: The ledger kind already chosen for the event.

    Returns:
        A ``TransactionEvent.SEGMENT_*`` value.
    """
    metadata = event_data.get("metadata") or {}
    if kind == KIND_CANCELLATION:
        return SEGMENT_EXISTING
    if kind == KIND_TRIAL_STARTED:
        return SEGMENT_NEW
    if metadata.get("is_trial_conversion") or metadata.get("is_signup_payment"):
        return SEGMENT_NEW
    billing_reason = metadata.get("billing_reason")
    if billing_reason in _NEW_BILLING_REASONS:
        return SEGMENT_NEW
    if billing_reason in _EXISTING_BILLING_REASONS or metadata.get("is_renewal"):
        return SEGMENT_EXISTING
    if event_data.get("type") == "subscription_renewed":
        return SEGMENT_EXISTING
    orders_count = customer_data.get("orders_count")
    if isinstance(orders_count, int) and not isinstance(orders_count, bool):
        if orders_count == 1:
            return SEGMENT_NEW
        if orders_count > 1:
            return SEGMENT_EXISTING
    return SEGMENT_UNKNOWN


def _customer_email(event_data: dict[str, Any], customer_data: dict[str, Any]) -> str:
    """Return the customer's email from whichever payload carried it."""
    email = customer_data.get("email") or event_data.get("customer_email") or ""
    return str(email).strip().lower()


def _customer_name(customer_data: dict[str, Any]) -> str:
    """Return a display name for the customer, or an empty string."""
    full = " ".join(
        str(part)
        for part in (customer_data.get("first_name"), customer_data.get("last_name"))
        if part
    ).strip()
    return full or str(customer_data.get("name") or "").strip()


def _charge_source(event_data: dict[str, Any]) -> str:
    """Return the raw id that ties a payment's attempts together.

    Stripe retries the same invoice, so the invoice id (the event's
    ``external_id``) links its failures to the eventual success. Chargify
    and Shopify retries are new transactions on the same subscription or
    contract, so those link by subscription.
    """
    metadata = event_data.get("metadata") or {}
    if event_data.get("provider") == "stripe" and event_data.get("external_id"):
        return str(event_data["external_id"])
    for key in ("subscription_id", "subscription_contract_id", "order_id"):
        if metadata.get(key):
            return str(metadata[key])
    return str(event_data.get("customer_id") or "")


def _event_source_id(event_data: dict[str, Any]) -> str:
    """Return the provider's id for this specific occurrence, if any."""
    metadata = event_data.get("metadata") or {}
    for value in (
        metadata.get("transaction_id"),
        event_data.get("external_id"),
        event_data.get("event_id"),
    ):
        if value:
            return str(value)
    return ""


def _to_amount(value: Any) -> Decimal:
    """Return a non-negative Decimal amount in major units."""
    try:
        return abs(Decimal(str(value or 0))).quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _to_attempt_count(value: Any) -> int | None:
    """Return a sane attempt count, or None."""
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if 0 <= count < 1000 else None


def _to_datetime(value: Any) -> datetime | None:
    """Convert a Unix timestamp to an aware datetime, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def record_event(
    workspace: Workspace | None,
    event_data: dict[str, Any],
    customer_data: dict[str, Any] | None = None,
) -> TransactionEvent | None:
    """Record one parsed event in the sales ledger.

    Never raises: the ledger must not be able to fail webhook delivery.

    Args:
        workspace: The workspace the event belongs to (None for the global
            billing endpoint, which is never recorded).
        event_data: Parsed event data from a source plugin.
        customer_data: Customer data from the same payload.

    Returns:
        The stored row, or None when the event was skipped, a duplicate,
        or storage failed.
    """
    if workspace is None:
        return None
    try:
        return _record_event(workspace, event_data, customer_data or {})
    except Exception:
        logger.warning("Failed to record sales ledger event", exc_info=True)
        return None


def _record_event(
    workspace: Workspace,
    event_data: dict[str, Any],
    customer_data: dict[str, Any],
) -> TransactionEvent | None:
    """Do the work of :func:`record_event`, letting errors propagate."""
    kind = classify_kind(event_data)
    if kind is None:
        return None

    provider = str(event_data.get("provider") or "unknown").lower()[:20]
    amount = _to_amount(event_data.get("amount"))
    if kind in (KIND_SALE, KIND_REFUND) and not amount:
        # A zero-amount "sale" (free plan invoice, $0 proration) is not a sale.
        return None

    customer_id = str(event_data.get("customer_id") or "")
    email = _customer_email(event_data, customer_data)
    domain = ""
    is_consumer = False
    if email and extract_domain(email):
        if is_enrichable_domain(email):
            domain = extract_domain(email) or ""
        else:
            # Free, disposable or hosted mailbox: a person, not a company.
            is_consumer = True

    metadata = event_data.get("metadata") or {}
    attempt_count = _to_attempt_count(metadata.get("attempt_count"))
    source_id = _event_source_id(event_data)
    if source_id:
        dedupe_key = keyed_hash(
            workspace.pk, provider, kind, source_id, attempt_count or 0
        )
    else:
        # Nothing to deduplicate on; every delivery is its own row.
        dedupe_key = keyed_hash(workspace.pk, provider, kind, uuid.uuid4().hex)

    customer_ref_source = customer_id or email
    if not customer_ref_source:
        return None

    details = {
        "name": _customer_name(customer_data),
        "email": email,
        "company_name": str(customer_data.get("company_name") or ""),
        "customer_id": customer_id,
        "external_id": str(event_data.get("external_id") or ""),
        "plan_name": str(metadata.get("plan_name") or ""),
        "failure_reason": str(metadata.get("failure_reason") or ""),
    }

    row = TransactionEvent(
        workspace=workspace,
        provider=provider,
        kind=kind,
        segment=classify_segment(event_data, customer_data, kind),
        occurred_at=timezone.now(),
        amount=amount,
        currency=str(event_data.get("currency") or "USD").upper()[:3],
        company_domain=domain[:255],
        is_consumer=is_consumer,
        customer_ref=keyed_hash(workspace.pk, provider, customer_ref_source),
        charge_ref=keyed_hash(workspace.pk, provider, _charge_source(event_data)),
        dedupe_key=dedupe_key,
        attempt_count=attempt_count,
        next_attempt_at=_to_datetime(metadata.get("next_payment_attempt")),
        details={key: value for key, value in details.items() if value},
    )
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        # Same event already recorded (redelivery, or Stripe's paired
        # invoice.payment_succeeded / invoice.paid).
        return None
    return row


def purge_expired(now: datetime | None = None) -> int:
    """Delete ledger rows past the retention window.

    Args:
        now: Reference time (defaults to the current time).

    Returns:
        Number of rows deleted.
    """
    from datetime import timedelta

    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    deleted, _ = TransactionEvent.objects.filter(occurred_at__lt=cutoff).delete()
    return int(deleted)

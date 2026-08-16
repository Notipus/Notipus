"""Data access and erasure for Shopify's mandatory privacy webhooks.

Shopify requires every public app to answer three topics:
``customers/data_request``, ``customers/redact`` and ``shop/redact``.
This module does the actual work behind them - finding what Notipus
holds about a person or a shop, and deleting it.

What Notipus holds, and how each part is handled:

* Enriched webhook records in Redis, keyed
  ``webhook:{workspace}:{type}:{timestamp}`` and indexed by a per-day
  activity list. These carry customer names, emails and order detail,
  so they are scanned and deleted.
* ``Person`` rows - per-workspace PII from email enrichment - deleted
  for the workspace and email in question.
* ``Company`` rows are deliberately untouched: they hold public brand
  data fetched by domain, are shared across workspaces by design, and
  contain no personal data (see the model docstring).
* Raw captured webhook bodies expire on their own 7-day TTL.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from core.encrypted_cache import decrypt_cache_value
from core.models import Person, Workspace
from core.services.mail import send_email
from core.services.recipients import admin_emails
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# How far back to scan the daily activity indexes. Records expire on a
# 7-day TTL, so a slightly wider window covers clock skew and any index
# entry written just before midnight.
SCAN_DAYS = 10


def _activity_key(workspace_uuid: str, date_str: str) -> str:
    """Return the daily activity index key for a workspace.

    Args:
        workspace_uuid: The workspace UUID.
        date_str: Date in YYYY-MM-DD form.

    Returns:
        The Redis key holding that day's webhook key list.
    """
    return f"webhook_activity:{workspace_uuid}:{date_str}"


def _iter_activity(workspace_uuid: str) -> list[tuple[str, list[str]]]:
    """Read every retained daily activity index for a workspace.

    Args:
        workspace_uuid: The workspace UUID.

    Returns:
        List of (activity_key, webhook_keys) pairs.
    """
    days: list[tuple[str, list[str]]] = []
    for offset in range(SCAN_DAYS):
        date_str = (timezone.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        key = _activity_key(workspace_uuid, date_str)
        keys = cache.get(key, [])
        if isinstance(keys, str):
            try:
                keys = json.loads(keys)
            except json.JSONDecodeError:
                keys = []
        if keys:
            days.append((key, list(keys)))
    return days


def _load_record(webhook_key: str) -> dict[str, Any] | None:
    """Load and decrypt one stored webhook record.

    Args:
        webhook_key: The Redis key for the record.

    Returns:
        The record, or None if absent or unreadable.
    """
    raw = decrypt_cache_value(cache.get(webhook_key))
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw if isinstance(raw, dict) else None


def _matches_customer(
    record: dict[str, Any], customer_id: str | None, email: str | None
) -> bool:
    """Check whether a stored record belongs to a given customer.

    Shopify identifies the person by id, but guest checkouts are keyed
    by email instead, so both have to be considered.

    Args:
        record: A stored webhook record.
        customer_id: Shopify customer id, if known.
        email: Customer email, if known.

    Returns:
        True when the record is about that customer.
    """
    if customer_id and str(record.get("customer_id", "")) == str(customer_id):
        return True
    if not email:
        return False

    lowered = email.lower()
    if str(record.get("customer_id", "")).lower() == lowered:
        return True
    for field in ("customer_email", "email"):
        if str(record.get(field, "")).lower() == lowered:
            return True
    customer = record.get("customer")
    if isinstance(customer, dict):
        return str(customer.get("email", "")).lower() == lowered
    return False


def collect_customer_data(
    workspace: Workspace, customer_id: str | None, email: str | None
) -> dict[str, Any]:
    """Gather everything Notipus holds about one customer.

    Answers ``customers/data_request``. Shopify gives the merchant 30
    days to supply this, so the result is returned for the operator to
    forward rather than sent anywhere automatically.

    Args:
        workspace: The workspace whose data to search.
        customer_id: Shopify customer id, if supplied.
        email: Customer email, if supplied.

    Returns:
        A dictionary of the stored records and enrichment rows.
    """
    records: list[dict[str, Any]] = []
    for _, webhook_keys in _iter_activity(str(workspace.uuid)):
        for webhook_key in webhook_keys:
            record = _load_record(webhook_key)
            if record and _matches_customer(record, customer_id, email):
                records.append(record)

    people: list[dict[str, Any]] = []
    if email:
        for person in Person.objects.filter(workspace=workspace, email__iexact=email):
            people.append(
                {
                    "email": person.email,
                    "first_name": getattr(person, "first_name", ""),
                    "last_name": getattr(person, "last_name", ""),
                }
            )

    return {
        "workspace": workspace.name,
        "customer_id": customer_id,
        "email": email,
        "notification_records": records,
        "enriched_people": people,
        "notes": (
            "Company brand data is public, fetched by domain and shared "
            "across workspaces; it holds no personal data and is not "
            "included."
        ),
    }


def fulfil_data_request(
    workspace: Workspace,
    customer_id: str | None,
    email: str | None,
    request_id: Any = None,
) -> dict[str, Any]:
    """Send the merchant everything Notipus holds about one customer.

    Shopify requires the app to supply this within 30 days, and it must
    go to the merchant - they are the data controller and the only party
    who can verify who asked. Acknowledging the webhook is not
    fulfilment, so the export is emailed to the workspace's owners and
    admins as a JSON attachment.

    Args:
        workspace: The workspace whose data to search.
        customer_id: Shopify customer id, if supplied.
        email: Customer email, if supplied.
        request_id: Shopify's data request id, for the merchant's records.

    Returns:
        Counts of what was found, plus whether the mail was accepted.
    """
    collected = collect_customer_data(workspace, customer_id, email)
    record_count = len(collected["notification_records"])
    people_count = len(collected["enriched_people"])

    recipients = admin_emails(workspace)
    subject = f"[Notipus] Shopify customer data request for {workspace.name}"
    identifier = email or customer_id or "unidentified customer"
    body = (
        f"Shopify forwarded a customer data request (id {request_id}) for "
        f"{identifier}.\n\n"
        f"Attached is everything Notipus holds for that customer in the "
        f'"{workspace.name}" workspace: {record_count} notification '
        f"record(s) and {people_count} enrichment record(s).\n\n"
        "Notipus is a processor here, not the controller. Forward this to "
        "the customer yourself once you have verified their identity - "
        "Shopify gives you 30 days from the request.\n\n"
        "Company brand data is not included: it is public information "
        "looked up by domain and holds nothing personal.\n\n"
        "If the customer has also asked to be erased, Shopify sends that "
        "separately as a redaction request and Notipus acts on it "
        "automatically.\n"
    )

    sent = send_email(
        subject=subject,
        text_body=body,
        recipients=recipients,
        attachments=[
            (
                f"notipus-data-request-{request_id or 'export'}.json",
                json.dumps(collected, indent=2, default=str),
                "application/json",
            )
        ],
    )

    if not sent:
        # Loud, and with every identifier needed to redo it by hand: this
        # is a legal obligation with a deadline, and the webhook is only
        # delivered once.
        logger.error(
            "UNFULFILLED Shopify data request: workspace=%s request_id=%s "
            "customer_id=%s email=%s recipients=%s records=%d people=%d",
            workspace.uuid,
            request_id,
            customer_id,
            email,
            recipients or "NONE",
            record_count,
            people_count,
        )
    else:
        logger.info(
            "Fulfilled Shopify data request %s for workspace %s: "
            "%d records, %d people sent to %d recipient(s)",
            request_id,
            workspace.uuid,
            record_count,
            people_count,
            len(recipients),
        )

    return {"records": record_count, "people": people_count, "delivered": sent}


def redact_customer(
    workspace: Workspace, customer_id: str | None, email: str | None
) -> dict[str, int]:
    """Delete everything Notipus holds about one customer.

    Answers ``customers/redact``.

    Args:
        workspace: The workspace to erase from.
        customer_id: Shopify customer id, if supplied.
        email: Customer email, if supplied.

    Returns:
        Counts of what was deleted.
    """
    deleted_records = 0
    for activity_key, webhook_keys in _iter_activity(str(workspace.uuid)):
        survivors: list[str] = []
        for webhook_key in webhook_keys:
            record = _load_record(webhook_key)
            if record and _matches_customer(record, customer_id, email):
                cache.delete(webhook_key)
                deleted_records += 1
            else:
                survivors.append(webhook_key)
        if len(survivors) != len(webhook_keys):
            # Rewrite the index so deleted keys stop being referenced.
            cache.set(activity_key, json.dumps(survivors), timeout=60 * 60 * 24 * 10)

    deleted_people = 0
    if email:
        deleted_people, _ = Person.objects.filter(
            workspace=workspace, email__iexact=email
        ).delete()

    logger.info(
        "Shopify customers/redact for workspace %s: %d records, %d people deleted",
        workspace.uuid,
        deleted_records,
        deleted_people,
    )
    return {"records": deleted_records, "people": deleted_people}


def redact_shop(workspace: Workspace) -> dict[str, int]:
    """Delete everything Notipus holds for a shop.

    Answers ``shop/redact``, which Shopify sends 48 hours after an
    uninstall. Every stored record for the workspace goes, along with
    its per-workspace enrichment rows, and the Shopify integration is
    deactivated so nothing tries to reach the store again.

    Args:
        workspace: The workspace to erase.

    Returns:
        Counts of what was deleted.
    """
    deleted_records = 0
    for activity_key, webhook_keys in _iter_activity(str(workspace.uuid)):
        for webhook_key in webhook_keys:
            cache.delete(webhook_key)
            deleted_records += 1
        cache.delete(activity_key)

    deleted_people, _ = Person.objects.filter(workspace=workspace).delete()

    deactivated = workspace.integrations.filter(
        integration_type="shopify", is_active=True
    ).update(is_active=False)

    logger.info(
        "Shopify shop/redact for workspace %s: %d records, %d people deleted, "
        "%d integrations deactivated",
        workspace.uuid,
        deleted_records,
        deleted_people,
        deactivated,
    )
    return {
        "records": deleted_records,
        "people": deleted_people,
        "integrations": deactivated,
    }

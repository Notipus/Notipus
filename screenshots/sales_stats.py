"""Capture the weekly sales snapshot (desktop and mobile).

Seeds two weeks of Initech's sales ledger - new and existing customers
across a handful of industries, a refund, a churn, and payment failures
that recovered, are still being retried, or were lost - then captures
last week's snapshot with the Software & SaaS drill-down open.
"""

from datetime import timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from conftest import shoot
from core.models import Company, TransactionEvent, Workspace
from core.services.sales_stats import current_period, workspace_tz
from django.utils import timezone
from playwright.sync_api import Page

COMPANIES = {
    "initrode.example": ("Initrode", "Software & SaaS"),
    "penetrode.example": ("Penetrode", "Software & SaaS"),
    "intertrode.example": ("Intertrode", "Software & SaaS"),
    "chotchkies.example": ("Chotchkie's", "Food & Beverage"),
    "flingers.example": ("Flingers", "Food & Beverage"),
    "swingline.example": ("Swingline", "Manufacturing & Industrial"),
    "bobs.example": ("The Bobs Consulting", "Professional Services"),
    "lumbergh.example": ("Lumbergh Health", "Healthcare & Life Sciences"),
}

_keys = count()


def _row(workspace: Workspace, at: Any, kind: str, domain: str, **extra: Any) -> None:
    """Create one ledger row for a demo company."""
    name = COMPANIES[domain][0] if domain in COMPANIES else ""
    fields: dict[str, Any] = {
        "workspace": workspace,
        "provider": "stripe",
        "kind": kind,
        "segment": "existing",
        "occurred_at": at,
        "amount": Decimal("0"),
        "currency": "USD",
        "company_domain": domain if domain in COMPANIES else "",
        "is_consumer": domain == "consumer",
        "customer_ref": extra.pop("customer", domain),
        "charge_ref": extra.pop("charge", f"inv-{domain}-{next(_keys)}"),
        "dedupe_key": f"demo-{next(_keys)}",
        "details": {"company_name": name} if name else {"name": "Milton W."},
    }
    fields.update(extra)
    TransactionEvent.objects.create(**fields)


def _seed_week(workspace: Workspace, monday: Any, scale: Decimal) -> None:
    """Seed one week of sales, refunds, churn and payment recovery."""
    day = timedelta(days=1)
    sales = [
        ("initrode.example", "new", "4800"),
        ("penetrode.example", "existing", "1200"),
        ("intertrode.example", "new", "2400"),
        ("chotchkies.example", "existing", "650"),
        ("flingers.example", "new", "320"),
        ("swingline.example", "existing", "3900"),
        ("bobs.example", "new", "900"),
        ("lumbergh.example", "existing", "6100"),
        ("consumer", "new", "49"),
    ]
    for offset, (domain, segment, amount) in enumerate(sales):
        _row(
            workspace,
            monday + day * (offset % 5) + timedelta(hours=10),
            "sale",
            domain,
            segment=segment,
            amount=Decimal(amount) * scale,
        )
    _row(workspace, monday + 2 * day, "refund", "chotchkies.example", amount=150)
    _row(workspace, monday + 3 * day, "cancellation", "flingers.example")


def _seed_recovery(workspace: Workspace, monday: Any, now: Any) -> None:
    """Payment failures: recovered after two declines, at risk, and lost."""
    day = timedelta(days=1)
    _row(
        workspace,
        monday,
        "failure",
        "penetrode.example",
        charge="inv-p",
        attempt_count=1,
        amount=1200,
    )
    _row(
        workspace,
        monday + day,
        "failure",
        "penetrode.example",
        charge="inv-p",
        attempt_count=2,
        amount=1200,
    )
    _row(
        workspace,
        monday + 3 * day,
        "sale",
        "penetrode.example",
        charge="inv-p",
        amount=1200,
    )
    _row(
        workspace,
        monday + 4 * day,
        "failure",
        "chotchkies.example",
        charge="inv-c",
        attempt_count=2,
        amount=650,
        next_attempt_at=now + 2 * day,
        details={"company_name": "Chotchkie's", "failure_reason": "card_declined"},
    )
    _row(
        workspace,
        monday,
        "failure",
        "flingers.example",
        charge="inv-f",
        attempt_count=1,
        amount=320,
    )


@pytest.mark.django_db(transaction=True)
def sales_stats(
    page: Page, mobile_page: Page, office_space_data: dict[str, Any]
) -> None:
    workspace: Workspace = office_space_data["workspace"]
    workspace.sales_digest_weekly = True
    workspace.save()
    for domain, (name, industry) in COMPANIES.items():
        Company.objects.create(
            domain=domain, name=name, brand_info={"industry": industry}
        )

    now = timezone.now()
    last_week = current_period("week", workspace_tz(workspace), now).previous()
    _seed_week(workspace, last_week.previous().start_dt, Decimal("0.85"))
    _seed_week(workspace, last_week.start_dt, Decimal("1"))
    _seed_recovery(workspace, last_week.start_dt, now)

    path = (
        f"/stats/?period=week&start={last_week.start.isoformat()}"
        "&industry=Software+%26+SaaS"
    )
    shoot(page, "sales-stats", path, full_page=True)
    shoot(mobile_page, "sales-stats-mobile", path)

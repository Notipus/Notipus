"""Tests for payment-service (bill-pay / AP automation) domain handling.

A payment made through Bill.com, Tipalti and the like arrives with the
service's address as the customer email. These tests pin down that such
a domain is tagged, never enriched as the customer's company, and never
counted as a company or a consumer in the sales statistics.
"""

from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from core.models import Company, TransactionEvent, Workspace
from core.services.sales_ledger import record_event
from core.services.sales_stats import (
    INDUSTRY_CONSUMER,
    INDUSTRY_UNKNOWN,
    compute_snapshot,
    period_containing,
)
from core.utils.email_domain import is_enrichable_domain, is_payment_service_email
from disposable_email_domains import blocklist
from plugins.destinations.slack import SlackDestinationPlugin
from webhooks.services.event_processor import EventProcessor
from webhooks.services.notification_builder import NotificationBuilder
from webhooks.utils.email_classifier import EmailTag, classify_email
from webhooks.utils.free_email_domains import FREE_EMAIL_DOMAINS
from webhooks.utils.payment_service_domains import (
    PAYMENT_SERVICE_DOMAINS,
    is_payment_service_domain,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
WEEK = period_containing("week", NOW.date(), ZoneInfo("UTC"))
_seq = count()


class TestDomainMatching:
    """Label-based matching of payment-service domains."""

    @pytest.mark.parametrize(
        "domain",
        [
            "bill.com",
            "bills.com",
            "hq.bill.com",
            "BILL.COM",
            "bill.com.",
            "tipalti.com",
            "ansmtp.ariba.com",
        ],
    )
    def test_matches(self, domain: str) -> None:
        """Listed domains and their subdomains match.

        Args:
            domain: Domain to check.
        """
        assert is_payment_service_domain(domain)

    @pytest.mark.parametrize(
        "domain", ["mybill.com", "bill.co", "com", "", None, "acme.example"]
    )
    def test_does_not_match(self, domain: str | None) -> None:
        """Lookalikes, bare TLDs and empty values don't match.

        Args:
            domain: Domain to check.
        """
        assert not is_payment_service_domain(domain)

    def test_database_is_normalized_and_disjoint(self) -> None:
        """Entries are lowercase registrable domains in no other list."""
        for domain in PAYMENT_SERVICE_DOMAINS:
            assert domain == domain.strip().lower()
            assert not domain.startswith((".", "@"))
            assert "." in domain
            assert domain not in FREE_EMAIL_DOMAINS
            assert domain not in blocklist


class TestClassificationAndBadge:
    """The email is tagged and the Slack footer says so."""

    def test_tagged_as_payment_service(self) -> None:
        """A bill.com address gets exactly the payment-service tag."""
        assert classify_email("payments@bill.com") == [EmailTag.PAYMENT_SERVICE]
        assert classify_email("ar@hq.bill.com") == [EmailTag.PAYMENT_SERVICE]

    def test_slack_renders_badge(self) -> None:
        """The customer footer carries the payment-service badge."""
        notification = NotificationBuilder().build(
            {
                "type": "payment_success",
                "provider": "stripe",
                "customer_id": "cus_1",
                "amount": 49.0,
                "currency": "USD",
                "metadata": {},
            },
            {"email": "payments@bill.com"},
        )
        result = SlackDestinationPlugin().format(notification)
        assert ":bank: Paid via payment service" in str(result)


class TestEnrichment:
    """The intermediary is never enriched as the customer's company."""

    def test_not_enrichable(self) -> None:
        """Payment-service emails are filtered out of enrichment."""
        assert is_payment_service_email("payments@bill.com")
        assert not is_payment_service_email("jane@acme.example")
        assert not is_enrichable_domain("payments@bill.com")
        assert not is_enrichable_domain("x@tipalti.com")

    def test_event_processor_skips_company_lookup(self) -> None:
        """The enrichment service is never asked about bill.com."""
        processor = EventProcessor()
        processor.enrichment_service = MagicMock()
        assert processor._enrich_company({"email": "payments@bill.com"}) is None
        processor.enrichment_service.enrich_domain.assert_not_called()


@pytest.mark.django_db
class TestSalesStatistics:
    """Payment-service payers are unknown companies, not bill.com."""

    @pytest.fixture
    def workspace(self) -> Workspace:
        """A plain workspace.

        Returns:
            The workspace.
        """
        return Workspace.objects.create(name="Payments Co")

    def test_ledger_row_is_unknown_company(self, workspace: Workspace) -> None:
        """No domain is stored and the payer is not a consumer.

        Args:
            workspace: Workspace fixture.
        """
        row = record_event(
            workspace,
            {
                "type": "payment_success",
                "provider": "stripe",
                "customer_id": "cus_1",
                "external_id": "in_1",
                "amount": 99.0,
                "currency": "usd",
                "metadata": {"billing_reason": "subscription_create"},
            },
            {"email": "payments@bill.com"},
        )
        assert row is not None
        assert row.company_domain == ""
        assert not row.is_consumer

    def _row(
        self, workspace: Workspace, customer: str, domain: str, day: int = 16
    ) -> None:
        """Create a sale ledger row directly.

        Args:
            workspace: Owning workspace.
            customer: Customer reference.
            domain: Stored company domain.
            day: Day of November 2026 it happened.
        """
        TransactionEvent.objects.create(
            workspace=workspace,
            provider="stripe",
            kind="sale",
            segment="existing",
            occurred_at=datetime(2026, 11, day, 12, tzinfo=UTC),
            amount=Decimal("100"),
            currency="USD",
            company_domain=domain,
            is_consumer=False,
            customer_ref=customer,
            charge_ref=f"charge-{customer}-{day}",
            dedupe_key=f"pay{next(_seq)}",
            details={},
        )

    def test_legacy_rows_are_not_counted_as_bill_com(
        self, workspace: Workspace
    ) -> None:
        """Rows stored with bill.com before this change land in Unknown.

        Args:
            workspace: Workspace fixture.
        """
        Company.objects.create(
            domain="bill.com", name="Bill", brand_info={"industry": "Fintech"}
        )
        self._row(workspace, "a", "bill.com")
        snap = compute_snapshot(workspace, WEEK, now=NOW)
        industries = [r["industry"] for r in snap["industries"]]
        assert industries == [INDUSTRY_UNKNOWN]
        assert INDUSTRY_CONSUMER not in industries

    def test_customer_keeps_its_real_company(self, workspace: Workspace) -> None:
        """A customer who once paid directly keeps that company's industry.

        Args:
            workspace: Workspace fixture.
        """
        Company.objects.create(
            domain="saas.example", name="SaaS", brand_info={"industry": "Software"}
        )
        Company.objects.create(
            domain="bill.com", name="Bill", brand_info={"industry": "Fintech"}
        )
        self._row(workspace, "a", "saas.example", day=2)
        self._row(workspace, "a", "bill.com")
        snap = compute_snapshot(workspace, WEEK, now=NOW)
        assert [r["industry"] for r in snap["industries"]] == ["Software"]

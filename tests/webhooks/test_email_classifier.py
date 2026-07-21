"""Tests for the email domain classifier and free-provider database.

Covers ``webhooks.utils.email_classifier`` (suffix matching for
government/education/military/healthcare domains, free-provider and
disposable tagging with precedence) plus the end-to-end path from
NotificationBuilder into the Slack customer footer badges.
"""

from typing import Any

import pytest
from plugins.destinations.slack import SlackDestinationPlugin
from webhooks.services.notification_builder import NotificationBuilder
from webhooks.utils.email_classifier import EmailTag, classify_email
from webhooks.utils.free_email_domains import FREE_EMAIL_DOMAINS


class TestInstitutionalTags:
    """Suffix-based government/education/military/healthcare tagging."""

    @pytest.mark.parametrize(
        "email",
        [
            "jane@nasa.gov",  # US .gov
            "clerk@westminster.gov.uk",  # generic gov.XX
            "ana@fazenda.gov.br",  # generic gov.XX
            "pierre@economie.gouv.fr",  # France
            "juan@hacienda.gob.es",  # Spain
            "maria@sat.gob.mx",  # Mexico
            "hans@bmi.bund.de",  # German federal
            "urs@bk.admin.ch",  # Swiss federal
            "franz@bmf.gv.at",  # Austria
            "jan@minfin.fgov.be",  # Belgian federal
            "carla@mef.governo.it",  # Italy
            "kim@moef.go.kr",  # South Korea
            "sato@meti.go.jp",  # Japan central government
            "suzuki@city.yokohama.lg.jp",  # Japan local government
            "amit@cbdt.nic.in",  # India NIC
            "pat@treasury.govt.nz",  # New Zealand
            "alex@cra-arc.gc.ca",  # Government of Canada
            "sam@fin.canada.ca",  # Government of Canada
            "mp@commons.parliament.uk",  # UK Parliament
            "pc@met.police.uk",  # UK police
            "li@mof.gov.cn",  # China
            "nur@mof.gov.sg",  # Singapore
            "ivan@minfin.gov.ua",  # Ukraine
            "fatima@mof.gov.ae",  # UAE
        ],
    )
    def test_government_domains(self, email: str) -> None:
        """Test that government suffixes worldwide get the government tag."""
        assert classify_email(email) == [EmailTag.GOVERNMENT]

    @pytest.mark.parametrize(
        "email",
        [
            "kid@stanford.edu",  # US .edu
            "bob@cam.ac.uk",  # generic ac.XX
            "tanaka@u-tokyo.ac.jp",  # generic ac.XX
            "raj@iitb.edu.in",  # generic edu.XX
            "lucy@sydney.edu.au",  # generic edu.XX
            "tama@wellington.school.nz",  # NZ schools
            "head@stmarys.sch.uk",  # UK schools
            "amal@gems.sch.ae",  # UAE schools
            "yuki@shibuya.ed.jp",  # Japanese schools
            "teach@lincoln.k12.ca.us",  # US K-12 district
        ],
    )
    def test_education_domains(self, email: str) -> None:
        """Test that education suffixes worldwide get the education tag."""
        assert classify_email(email) == [EmailTag.EDUCATION]

    @pytest.mark.parametrize(
        "email",
        [
            "sgt@army.mil",  # US .mil
            "maj@mod.uk",  # UK Ministry of Defence
            "cpl@forces.gc.ca",  # Canadian Armed Forces (3-label suffix)
            "lt@mil.be",  # generic mil.XX
            "cpt@idf.il",  # Israel Defense Forces
            "col@tsk.tr",  # Turkish Armed Forces
        ],
    )
    def test_military_domains(self, email: str) -> None:
        """Test that military suffixes get the military tag."""
        assert classify_email(email) == [EmailTag.MILITARY]

    @pytest.mark.parametrize(
        "email",
        [
            "gp@doctors.nhs.uk",  # NHS England
            "nurse@ggc.nhs.scot",  # NHS Scotland
            "admin@nhs.net",  # NHSmail
        ],
    )
    def test_healthcare_domains(self, email: str) -> None:
        """Test that public health service suffixes get the healthcare tag."""
        assert classify_email(email) == [EmailTag.HEALTHCARE]

    @pytest.mark.parametrize(
        "email",
        [
            "acme@acme.com",  # plain business domain
            "someone@random.uk",  # bare .uk is not gov.uk
            "fan@bundesliga.de",  # substring safety vs bund.de
            "x@notgov.fr",  # substring safety vs generic gov pattern
            "info@governor.com",  # gov prefix on a .com
            "a@education.com",  # education-themed business domain
            "b@milwaukee.com",  # mil prefix on a .com
        ],
    )
    def test_non_institutional_domains(self, email: str) -> None:
        """Test that lookalike or generic domains get no institutional tag."""
        assert classify_email(email) == []


class TestProviderTags:
    """Free-provider and disposable tagging with precedence."""

    def test_free_provider_tagged(self) -> None:
        """Test that a major free provider gets exactly the free tag."""
        assert classify_email("kid@gmail.com") == [EmailTag.FREE]

    def test_disposable_tagged(self) -> None:
        """Test that a disposable domain gets exactly the disposable tag."""
        assert classify_email("x@mailinator.com") == [EmailTag.DISPOSABLE]

    def test_disposable_beats_free_when_domain_in_both(self) -> None:
        """Test that a domain in both databases is tagged disposable only.

        21cn.com is present in FREE_EMAIL_DOMAINS and in the
        disposable-email-domains blocklist, so it proves precedence.
        """
        from disposable_email_domains import blocklist

        assert "21cn.com" in FREE_EMAIL_DOMAINS
        assert "21cn.com" in blocklist
        assert classify_email("x@21cn.com") == [EmailTag.DISPOSABLE]

    def test_cc_free_provider_is_not_institutional(self) -> None:
        """Test a ccTLD free provider gets only the free tag."""
        assert classify_email("a@yahoo.co.uk") == [EmailTag.FREE]


class TestInputHandling:
    """Robustness against malformed input."""

    @pytest.mark.parametrize(
        "email",
        [
            "",
            "   ",
            "no-at-sign",
            "@",
            "user@",
            "user@nodot",
            "user@foo..gov",  # consecutive dots must not match .gov
            "user@.gov",  # leading dot
            "user@gov.",  # trailing dot
            "user@..",
            None,
        ],
    )
    def test_invalid_emails_get_no_tags(self, email: str | None) -> None:
        """Test that invalid or missing emails yield no tags."""
        assert classify_email(email) == []

    def test_case_and_whitespace_are_normalized(self) -> None:
        """Test that case and surrounding whitespace do not affect tags."""
        assert classify_email("  JANE@NASA.GOV  ") == [EmailTag.GOVERNMENT]
        assert classify_email("Kid@GMail.Com") == [EmailTag.FREE]

    def test_subaddressing_is_irrelevant(self) -> None:
        """Test that plus-addressing does not change classification."""
        assert classify_email("kid+tag@gmail.com") == [EmailTag.FREE]


class TestFreeEmailDatabase:
    """Sanity checks for the curated free-provider database."""

    @pytest.mark.parametrize(
        "domain",
        [
            "gmail.com",
            "outlook.com",
            "hotmail.co.uk",
            "yahoo.co.jp",
            "icloud.com",
            "proton.me",
            "qq.com",
            "163.com",
            "naver.com",
            "web.de",
            "orange.fr",
            "seznam.cz",
            "wp.pl",
            "uol.com.br",
            "mail.ru",
            "rediffmail.com",
        ],
    )
    def test_major_providers_present(self, domain: str) -> None:
        """Test that major worldwide free providers are in the database."""
        assert domain in FREE_EMAIL_DOMAINS

    def test_all_entries_lowercase_and_plausible(self) -> None:
        """Test that every entry is a lowercase dotted domain."""
        for domain in FREE_EMAIL_DOMAINS:
            assert domain == domain.strip().lower()
            assert "." in domain
            assert "@" not in domain

    def test_business_domains_absent(self) -> None:
        """Test that common business domains are not misfiled as free."""
        for domain in ("acme.com", "example.com", "microsoft.com"):
            assert domain not in FREE_EMAIL_DOMAINS


class TestSlackBadgeRendering:
    """End-to-end: builder populates tags, Slack renders badges."""

    @staticmethod
    def _footer_text(result: dict[str, Any]) -> str:
        """Extract the concatenated context-block text from a message.

        Args:
            result: Formatted Slack message dict.

        Returns:
            Joined mrkdwn text of all context blocks.
        """
        if "blocks" in result:
            blocks = result["blocks"]
        else:
            blocks = result["attachments"][0].get("blocks", [])
        texts: list[str] = []
        for block in blocks:
            if block["type"] == "context":
                texts.extend(el["text"] for el in block["elements"])
        return " ".join(texts)

    def _render(self, email: str) -> str:
        """Build a notification for the email and render it to Slack text.

        Args:
            email: Customer email address for the event.

        Returns:
            Concatenated context-block text of the rendered message.
        """
        builder = NotificationBuilder()
        event_data = {
            "type": "payment_success",
            "provider": "stripe",
            "customer_id": "cus_123",
            "amount": 49.00,
            "currency": "USD",
            "metadata": {"plan_name": "Starter"},
        }
        customer_data = {"email": email, "first_name": "Jane", "last_name": "Doe"}
        notification = builder.build(event_data, customer_data)
        result = SlackDestinationPlugin().format(notification)
        return self._footer_text(result)

    def test_edu_email_renders_education_badge(self) -> None:
        """Test that a .edu customer email renders the Education badge."""
        footer = self._render("jane@stanford.edu")
        assert ":bust_in_silhouette: jane@stanford.edu" in footer
        assert ":mortar_board: Education" in footer
        assert "jane@stanford.edu · :mortar_board: Education" in footer

    def test_gov_email_renders_government_badge(self) -> None:
        """Test that a .gov customer email renders the Government badge."""
        footer = self._render("jane@nasa.gov")
        assert ":classical_building: Government" in footer

    def test_free_email_renders_free_badge(self) -> None:
        """Test that a free-provider email renders the Free email badge."""
        footer = self._render("jane@gmail.com")
        assert ":mailbox: Free email" in footer

    def test_disposable_email_renders_disposable_badge(self) -> None:
        """Test that a disposable email renders the Disposable badge."""
        footer = self._render("jane@mailinator.com")
        assert ":wastebasket: Disposable email" in footer
        assert ":mailbox: Free email" not in footer

    def test_business_email_renders_no_badge(self) -> None:
        """Test that a business-domain email renders no badges."""
        footer = self._render("jane@acme-widgets.com")
        assert ":bust_in_silhouette: jane@acme-widgets.com" in footer
        for badge in (
            "Government",
            "Education",
            "Military",
            "Healthcare",
            "Free email",
            "Disposable email",
        ):
            assert badge not in footer

    def test_context_element_stays_within_slack_limits(self) -> None:
        """Test that the footer element stays far below Slack's 3000 chars."""
        footer = self._render("jane@stanford.edu")
        assert len(footer) < 3000

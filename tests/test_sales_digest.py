"""Tests for the sales digest, its schedule, the snapshot page and settings."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from itertools import count
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.models import (
    Company,
    SalesSnapshot,
    TransactionEvent,
    Workspace,
    WorkspaceMember,
)
from core.services.sales_digest import build_digest, due_digests, is_due, send_digest
from core.services.sales_stats import compute_snapshot, period_containing
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from plugins.destinations.slack import SlackDestinationPlugin
from plugins.destinations.teams import TeamsDestinationPlugin
from plugins.destinations.telegram import TelegramDestinationPlugin

# Monday 2026-11-23, 10:00 UTC: the week of Nov 16-22 has just closed.
MONDAY = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
_seq = count()


def _sale(workspace: Workspace, **overrides: Any) -> TransactionEvent:
    """Create a sale in the week of Nov 16-22.

    Args:
        workspace: Owning workspace.
        **overrides: Field overrides.

    Returns:
        The saved row.
    """
    fields: dict[str, Any] = {
        "workspace": workspace,
        "provider": "stripe",
        "kind": "sale",
        "segment": "new",
        "occurred_at": datetime(2026, 11, 17, 12, tzinfo=UTC),
        "amount": Decimal("250"),
        "currency": "USD",
        "company_domain": "saas.example",
        "customer_ref": "a",
        "charge_ref": "inv-a",
        "dedupe_key": f"d{next(_seq)}",
        "details": {"email": "jane@saas.example", "company_name": "Saas Example"},
    }
    fields.update(overrides)
    return TransactionEvent.objects.create(**fields)


@pytest.fixture
def workspace() -> Workspace:
    """A workspace with a classified customer and one sale.

    Returns:
        The workspace.
    """
    workspace = Workspace.objects.create(name="Digest Co", sales_digest_weekly=True)
    Company.objects.create(
        domain="saas.example", name="Saas", brand_info={"industry": "Software & SaaS"}
    )
    _sale(workspace)
    return workspace


class TestSchedule:
    """Digests are due at 09:00 local on Monday / the 1st."""

    @pytest.mark.parametrize(
        ("tz", "now", "kind", "due"),
        [
            ("UTC", datetime(2026, 11, 23, 9, 5, tzinfo=UTC), "week", True),
            ("UTC", datetime(2026, 11, 23, 8, 55, tzinfo=UTC), "week", False),
            ("UTC", datetime(2026, 11, 24, 9, 5, tzinfo=UTC), "week", False),
            # 09:05 UTC Monday is still 01:05 in Los Angeles.
            (
                "America/Los_Angeles",
                datetime(2026, 11, 23, 9, 5, tzinfo=UTC),
                "week",
                False,
            ),
            ("UTC", datetime(2026, 12, 1, 9, 0, tzinfo=UTC), "month", True),
            ("UTC", datetime(2026, 12, 2, 9, 0, tzinfo=UTC), "month", False),
        ],
    )
    def test_is_due(self, tz: str, now: datetime, kind: str, due: bool) -> None:
        """The schedule follows the workspace's own clock.

        Args:
            tz: Workspace timezone.
            now: Current time.
            kind: Period kind.
            due: Expected result.
        """
        assert is_due(Workspace(name="x", timezone=tz), kind, now) is due

    def test_only_opted_in_periods_are_due(self) -> None:
        """A weekly-only workspace gets no monthly digest."""
        workspace = Workspace(name="x", sales_digest_weekly=True)
        first_monday = datetime(2027, 2, 1, 10, tzinfo=UTC)
        assert due_digests([workspace], first_monday) == [(workspace, "week")]


@pytest.mark.django_db
class TestDigest:
    """Building and delivering the digest."""

    def test_digest_has_aggregates_but_no_people(self, workspace: Workspace) -> None:
        """The digest names industries, never customers.

        Args:
            workspace: Workspace fixture.
        """
        week = period_containing("week", MONDAY.date(), workspace_tz_utc())
        snapshot = compute_snapshot(workspace, week.previous(), now=MONDAY)
        notification = build_digest(workspace, snapshot, None)

        assert "Weekly sales snapshot" in notification.headline
        text = json.dumps(
            [
                {
                    "title": s.title,
                    "text": s.text,
                    "fields": [(f.label, f.value) for f in s.fields],
                }
                for s in notification.detail_sections
            ]
        )
        assert "Software & SaaS" in text
        assert "$250" in text
        assert "jane@" not in text and "Saas Example" not in text

    @pytest.mark.parametrize(
        "plugin_class",
        [SlackDestinationPlugin, TeamsDestinationPlugin, TelegramDestinationPlugin],
    )
    def test_every_destination_can_format_it(
        self, workspace: Workspace, plugin_class: type
    ) -> None:
        """The digest is a plain RichNotification every destination renders.

        Args:
            workspace: Workspace fixture.
            plugin_class: Destination plugin under test.
        """
        week = period_containing("week", MONDAY.date(), workspace_tz_utc())
        snapshot = compute_snapshot(workspace, week.previous(), now=MONDAY)
        formatted = plugin_class().format(build_digest(workspace, snapshot, None))
        rendered = json.dumps(formatted, default=str, ensure_ascii=False)
        # Telegram escapes "&" as HTML; the industry name is what matters.
        assert "Software &" in rendered and "SaaS" in rendered
        assert "Payment recovery" in rendered or "Summary" in rendered

    def test_send_once_per_period(self, workspace: Workspace) -> None:
        """A sent digest is recorded and not sent again.

        Args:
            workspace: Workspace fixture.
        """
        plugin = MagicMock(spec=SlackDestinationPlugin)
        plugin.format.return_value = {"blocks": []}
        registry = MagicMock()
        registry.get.return_value = plugin
        with (
            patch(
                "webhooks.services.destination_credentials.collect_destinations",
                return_value=[("slack", {"webhook_url": "https://example.com"})],
            ),
            patch("plugins.PluginRegistry.instance", return_value=registry),
        ):
            assert send_digest(workspace, "week", now=MONDAY) == "sent"
            assert send_digest(workspace, "week", now=MONDAY) == "already_sent"
            assert send_digest(workspace, "week", now=MONDAY, force=True) == "sent"

        assert plugin.send.call_count == 2
        snapshot = SalesSnapshot.objects.get(workspace=workspace, period="week")
        assert snapshot.digest_sent_at == MONDAY
        assert snapshot.start.isoformat() == "2026-11-16"

    def test_no_destinations(self, workspace: Workspace) -> None:
        """Without a destination nothing is marked as sent.

        Args:
            workspace: Workspace fixture.
        """
        with patch(
            "webhooks.services.destination_credentials.collect_destinations",
            return_value=[],
        ):
            assert send_digest(workspace, "week", now=MONDAY) == "no_destinations"
        assert not SalesSnapshot.objects.filter(digest_sent_at__isnull=False).exists()

    def test_command_dry_run(self, workspace: Workspace) -> None:
        """The command builds due digests without storing in a dry run.

        Args:
            workspace: Workspace fixture.
        """
        out = StringIO()
        call_command(
            "send_sales_digest",
            "--workspace",
            str(workspace.uuid),
            "--period",
            "week",
            "--dry-run",
            stdout=out,
        )
        assert "dry_run=1" in out.getvalue()
        assert not SalesSnapshot.objects.exists()

    def test_purge_command(self, workspace: Workspace) -> None:
        """The purge command reports what it deleted.

        Args:
            workspace: Workspace fixture.
        """
        out = StringIO()
        call_command("purge_sales_ledger", stdout=out)
        assert "Deleted" in out.getvalue()


def workspace_tz_utc() -> Any:
    """Return UTC as a ZoneInfo.

    Returns:
        ZoneInfo("UTC").
    """
    from zoneinfo import ZoneInfo

    return ZoneInfo("UTC")


def _member(workspace: Workspace, username: str, role: str = "user") -> Client:
    """Log a new member of ``workspace`` into a test client.

    Args:
        workspace: The workspace.
        username: Username to create.
        role: Membership role.

    Returns:
        A logged-in client.
    """
    user = User.objects.create_user(username=username, email=f"{username}@example.com")
    WorkspaceMember.objects.create(user=user, workspace=workspace, role=role)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestSalesStatsPage:
    """The /stats/ page."""

    def test_requires_login(self) -> None:
        """Anonymous visitors are sent to log in."""
        response = Client().get(reverse("core:sales_stats"))
        assert response.status_code == 302

    def test_renders_breakdown_and_drill_down(self, workspace: Workspace) -> None:
        """Members see industries, and customer names in the drill-down.

        Args:
            workspace: Workspace fixture.
        """
        client = _member(workspace, "member")
        url = reverse("core:sales_stats")
        response = client.get(
            url,
            {"period": "week", "start": "2026-11-16", "industry": "Software & SaaS"},
        )
        assert response.status_code == 200
        body = response.content.decode()
        assert "Customers by industry" in body
        assert "Software &amp; SaaS" in body
        assert "Saas Example" in body
        assert "Payment recovery" in body

    def test_empty_state_and_isolation(self, workspace: Workspace) -> None:
        """Another workspace's sales never show up.

        Args:
            workspace: Workspace fixture.
        """
        other = Workspace.objects.create(name="Other")
        client = _member(other, "outsider")
        response = client.get(
            reverse("core:sales_stats"), {"period": "week", "start": "2026-11-16"}
        )
        body = response.content.decode()
        assert "No sales recorded" in body
        assert "Saas Example" not in body

    def test_bad_params_fall_back(self, workspace: Workspace) -> None:
        """Junk query parameters don't break the page.

        Args:
            workspace: Workspace fixture.
        """
        client = _member(workspace, "member")
        response = client.get(
            reverse("core:sales_stats"), {"period": "year", "start": "not-a-date"}
        )
        assert response.status_code == 200

    def test_settings_save_timezone_and_digest(self, workspace: Workspace) -> None:
        """Admins set the timezone and digest opt-in in workspace settings.

        Args:
            workspace: Workspace fixture.
        """
        client = _member(workspace, "owner", role="owner")
        response = client.post(
            reverse("core:workspace_settings"),
            {
                "name": workspace.name,
                "timezone": "Europe/Stockholm",
                "sales_digest_monthly": "1",
            },
        )
        assert response.status_code == 302
        workspace.refresh_from_db()
        assert workspace.timezone == "Europe/Stockholm"
        assert workspace.sales_digest_monthly
        assert not workspace.sales_digest_weekly

        client.post(
            reverse("core:workspace_settings"),
            {"name": workspace.name, "timezone": "Not/AZone"},
        )
        workspace.refresh_from_db()
        assert workspace.timezone == "Europe/Stockholm"

"""Tests for usage alert emails and soft limit enforcement.

Covers the FAQ-promised behavior: admins are warned by email when
approaching the monthly event limit, delivery continues past the limit
(with an exceeded email and an operator copy), and a hard per-plan
grace cap pauses delivery only after those warnings have fired.
"""

from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from core.models import Plan, Workspace, WorkspaceMember
from core.services.usage_alerts import (
    hard_limit,
    maybe_send_usage_alerts,
    warning_count,
)
from django.contrib.auth.models import User
from django.core import mail
from django.test import override_settings
from webhooks.services.rate_limiter import RateLimiter, RateLimitException


@pytest.fixture
def workspace(db: None) -> Workspace:
    """Create a free-plan workspace with an owner, an admin, and a member.

    The plain member and the email-less admin must never receive usage
    alerts; only owner@example.com and admin@example.com should.
    """
    workspace = Workspace.objects.create(
        name="Acme",
        subscription_plan="free",
        subscription_status="active",
    )
    owner = User.objects.create_user(username="owner", email="owner@example.com")
    admin = User.objects.create_user(username="admin", email="admin@example.com")
    no_email_admin = User.objects.create_user(username="quiet", email="")
    member = User.objects.create_user(username="member", email="member@example.com")
    WorkspaceMember.objects.create(user=owner, workspace=workspace, role="owner")
    WorkspaceMember.objects.create(user=admin, workspace=workspace, role="admin")
    WorkspaceMember.objects.create(
        user=no_email_admin, workspace=workspace, role="admin"
    )
    WorkspaceMember.objects.create(user=member, workspace=workspace, role="user")
    return workspace


class TestThresholds:
    """Threshold helpers derive alert points from the plan limit."""

    def test_warning_count_is_80_percent(self) -> None:
        """The warning fires at 80% of the plan limit."""
        assert warning_count(20) == 16
        assert warning_count(10000) == 8000

    def test_warning_count_never_below_one(self) -> None:
        """Tiny limits still get a positive warning threshold."""
        assert warning_count(1) == 1

    @pytest.mark.django_db
    def test_hard_limit_uses_plan_grace_multiplier(self) -> None:
        """The hard cap comes from the plan's grace_multiplier row."""
        Plan.objects.update_or_create(
            name="free",
            defaults={
                "display_name": "Free",
                "price_monthly": 0,
                "grace_multiplier": Decimal("1.50"),
            },
        )
        assert hard_limit(20, "free") == 30

    @pytest.mark.django_db
    def test_hard_limit_defaults_without_plan_row(self) -> None:
        """A missing Plan row falls back to the default 2x grace factor."""
        assert hard_limit(20, "nonexistent") == 40

    @pytest.mark.django_db
    def test_hard_limit_never_below_plan_limit(self) -> None:
        """A sub-1 multiplier cannot push the hard cap below the limit."""
        Plan.objects.update_or_create(
            name="free",
            defaults={
                "display_name": "Free",
                "price_monthly": 0,
                "grace_multiplier": Decimal("0.50"),
            },
        )
        assert hard_limit(20, "free") == 20


@pytest.mark.django_db
class TestUsageAlertEmails:
    """Alert emails fire exactly at threshold crossings."""

    def test_warning_email_at_80_percent(self, workspace: Workspace) -> None:
        """Crossing 80% emails owners and admins that have an address."""
        maybe_send_usage_alerts(workspace, new_usage=16, limit=20)

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert "16 of 20" in message.subject
        assert sorted(message.to) == ["admin@example.com", "owner@example.com"]
        assert "upgrade" in message.body.lower()

    def test_no_email_off_thresholds(self, workspace: Workspace) -> None:
        """Counts that are not exact crossings send nothing."""
        for usage in (1, 15, 17, 20, 22, 39, 41):
            maybe_send_usage_alerts(workspace, new_usage=usage, limit=20)

        assert mail.outbox == []

    @override_settings(USAGE_ALERT_OPERATOR_EMAIL="ops@notipus.com")
    def test_exceeded_email_at_limit_plus_one(self, workspace: Workspace) -> None:
        """The first event over the limit emails admins and the operator."""
        maybe_send_usage_alerts(workspace, new_usage=21, limit=20)

        assert len(mail.outbox) == 2
        admin_message, operator_message = mail.outbox
        assert "exceeded" in admin_message.subject.lower()
        assert "still being delivered" in admin_message.body.lower()
        assert operator_message.to == ["ops@notipus.com"]
        assert "reach out" in operator_message.body.lower()

    def test_exceeded_without_operator_configured(self, workspace: Workspace) -> None:
        """No operator address means only the admin email is sent."""
        maybe_send_usage_alerts(workspace, new_usage=21, limit=20)

        assert len(mail.outbox) == 1

    @override_settings(USAGE_ALERT_OPERATOR_EMAIL="ops@notipus.com")
    def test_paused_email_at_hard_cap(self, workspace: Workspace) -> None:
        """Reaching the grace cap emails admins and the operator."""
        maybe_send_usage_alerts(workspace, new_usage=40, limit=20)

        assert len(mail.outbox) == 2
        admin_message, operator_message = mail.outbox
        assert "paused" in admin_message.subject.lower()
        assert operator_message.to == ["ops@notipus.com"]

    def test_send_failure_does_not_raise(self, workspace: Workspace) -> None:
        """A broken mail backend must never break webhook processing."""
        with patch(
            "core.services.usage_alerts.send_mail",
            side_effect=Exception("smtp down"),
        ):
            maybe_send_usage_alerts(workspace, new_usage=16, limit=20)

    def test_no_recipients_does_not_raise(self, db: None) -> None:
        """A workspace with no admin emails skips sending without error."""
        lonely = Workspace.objects.create(
            name="Lonely",
            subscription_plan="free",
            subscription_status="active",
        )
        maybe_send_usage_alerts(lonely, new_usage=16, limit=20)

        assert mail.outbox == []


class TestSoftLimitEnforcement:
    """The rate limiter allows a grace window before rejecting."""

    def _org(self) -> Mock:
        """Build a minimal organization stub on the free plan (limit 20)."""
        return Mock(uuid="org-abc", subscription_plan="free", name="Acme")

    def test_over_limit_is_allowed_within_grace(self) -> None:
        """Usage past the plan limit does not raise; it is flagged instead."""
        limiter = RateLimiter()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch("core.services.usage_alerts.maybe_send_usage_alerts") as mock_alerts,
        ):
            mock_cache.get.return_value = 20  # at the soft limit
            mock_cache.incr.return_value = 21
            info = limiter.enforce_rate_limit(self._org())

        assert info["over_limit"] is True
        assert info["current_usage"] == 21
        assert info["remaining"] == 0
        mock_alerts.assert_called_once()

    def test_under_limit_not_flagged(self) -> None:
        """Normal usage is not flagged as over the limit."""
        limiter = RateLimiter()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch("core.services.usage_alerts.maybe_send_usage_alerts"),
        ):
            mock_cache.get.return_value = 5
            mock_cache.incr.return_value = 6
            info = limiter.enforce_rate_limit(self._org())

        assert info["over_limit"] is False

    def test_hard_cap_rejects(self) -> None:
        """Usage at the grace cap raises RateLimitException."""
        limiter = RateLimiter()

        with patch("webhooks.services.rate_limiter.cache") as mock_cache:
            mock_cache.get.return_value = 40  # free limit 20 * default 2x
            with pytest.raises(RateLimitException) as exc_info:
                limiter.enforce_rate_limit(self._org())

        assert "hard limit" in str(exc_info.value).lower()

    def test_alerts_receive_usage_and_limit(self) -> None:
        """The alert hook gets the post-increment count and the plan limit."""
        limiter = RateLimiter()
        org = self._org()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch("core.services.usage_alerts.maybe_send_usage_alerts") as mock_alerts,
        ):
            mock_cache.get.return_value = 15
            mock_cache.incr.return_value = 16
            limiter.enforce_rate_limit(org)

        mock_alerts.assert_called_once_with(org, 16, 20)

    def test_alerts_skipped_on_cache_outage(self) -> None:
        """Fallback-mode counts must not drive alert emails."""
        limiter = RateLimiter()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch("core.services.usage_alerts.maybe_send_usage_alerts") as mock_alerts,
        ):
            mock_cache.get.side_effect = Exception("redis down")
            mock_cache.incr.side_effect = Exception("redis down")
            limiter.enforce_rate_limit(self._org())

        mock_alerts.assert_not_called()

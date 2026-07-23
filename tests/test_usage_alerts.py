"""Tests for usage alert emails and soft limit enforcement.

Covers the FAQ-promised behavior: admins are warned by email when
approaching the monthly event limit, delivery continues past the limit
(with an exceeded email), and a hard per-plan grace cap pauses delivery
only after those warnings have fired.
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

    def test_warning_count_rounds_up_to_at_least_80_percent(self) -> None:
        """A fractional 80% point rounds up so the warning never fires early.

        Flooring would warn at 16/21 (~76%); the ceiling puts it at
        17/21 (~81%).
        """
        assert warning_count(21) == 17

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
    def test_hard_limit_rounds_fractional_caps_up(self) -> None:
        """A fractional limit x multiplier product rounds up, not down.

        Truncation would cut customers off earlier than the configured
        cap; ceiling rounding always errs in the customer's favor.
        """
        Plan.objects.update_or_create(
            name="free",
            defaults={
                "display_name": "Free",
                "price_monthly": 0,
                "grace_multiplier": Decimal("1.25"),
            },
        )
        assert hard_limit(2, "free") == 3  # 2 * 1.25 = 2.5 -> 3

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

    def test_exceeded_email_at_limit_plus_one(self, workspace: Workspace) -> None:
        """The first event over the limit emails the workspace admins."""
        maybe_send_usage_alerts(workspace, new_usage=21, limit=20)

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert "exceeded" in message.subject.lower()
        assert "still being delivered" in message.body.lower()
        assert sorted(message.to) == ["admin@example.com", "owner@example.com"]

    def test_paused_email_at_hard_cap(self, workspace: Workspace) -> None:
        """Reaching the grace cap emails the workspace admins."""
        maybe_send_usage_alerts(workspace, new_usage=40, limit=20)

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert "paused" in message.subject.lower()
        assert sorted(message.to) == ["admin@example.com", "owner@example.com"]

    def test_paused_email_at_limit_when_no_grace_window(
        self, workspace: Workspace
    ) -> None:
        """With no grace window, landing on the limit sends the paused email.

        When grace_multiplier <= 1 the cap coincides with the plan
        limit, so the final allowed event is the paused crossing —
        rejection must not start unannounced.
        """
        Plan.objects.update_or_create(
            name="free",
            defaults={
                "display_name": "Free",
                "price_monthly": 0,
                "grace_multiplier": Decimal("1.00"),
            },
        )
        maybe_send_usage_alerts(workspace, new_usage=20, limit=20)

        assert len(mail.outbox) == 1
        assert "paused" in mail.outbox[0].subject.lower()

    def test_paused_wins_when_cap_is_limit_plus_one(self, workspace: Workspace) -> None:
        """A cap of limit + 1 sends the paused email, not the exceeded one.

        Both crossings coincide there; "exceeded, still delivering"
        would be false since the very next request is rejected.
        """
        Plan.objects.update_or_create(
            name="free",
            defaults={
                "display_name": "Free",
                "price_monthly": 0,
                "grace_multiplier": Decimal("1.05"),  # 20 * 1.05 = 21
            },
        )
        maybe_send_usage_alerts(workspace, new_usage=21, limit=20)

        assert len(mail.outbox) == 1
        assert "paused" in mail.outbox[0].subject.lower()

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
        org = self._org()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch("core.services.usage_alerts.maybe_send_usage_alerts") as mock_alerts,
        ):
            mock_cache.get.return_value = 20  # at the soft limit
            mock_cache.incr.return_value = 21
            info = limiter.enforce_rate_limit(org)

        assert info["over_limit"] is True
        assert info["current_usage"] == 21
        assert info["remaining"] == 0
        # The over-limit branch already fetched the hard cap; the alert
        # hook must receive it rather than re-querying the plan row.
        mock_alerts.assert_called_once_with(org, 21, 20, hard_at=40)

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

    def test_racing_increment_past_hard_cap_rejects(self) -> None:
        """A concurrent overshoot of the hard cap is caught post-increment.

        Two racers can both observe hard_cap - 1 and pass the pre-check;
        the atomic increment then puts one of them past the cap, which
        must reject rather than deliver.
        """
        limiter = RateLimiter()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch("core.services.usage_alerts.maybe_send_usage_alerts") as mock_alerts,
        ):
            mock_cache.get.return_value = 39  # hard cap (40) not yet reached
            mock_cache.incr.return_value = 41  # a racer got there first
            with pytest.raises(RateLimitException):
                limiter.enforce_rate_limit(self._org())

        mock_alerts.assert_not_called()

    def test_racing_past_soft_limit_with_no_grace_rejects(self) -> None:
        """A racer slipping past the pre-check cannot bypass a no-grace cap.

        With grace_multiplier <= 1 the cap equals the limit. Two racers
        at limit - 1 both pass the pre-check without fetching the cap;
        the post-increment check must fetch it and still reject the one
        that landed over the limit.
        """
        limiter = RateLimiter()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch.object(limiter, "get_hard_limit", return_value=20),
            patch("core.services.usage_alerts.maybe_send_usage_alerts") as mock_alerts,
        ):
            mock_cache.get.return_value = 19  # within the plan limit
            mock_cache.incr.return_value = 21  # a racer got there first
            with pytest.raises(RateLimitException):
                limiter.enforce_rate_limit(self._org())

        mock_alerts.assert_not_called()

    def test_increment_landing_exactly_on_hard_cap_is_allowed(self) -> None:
        """The request that lands exactly on the cap still delivers.

        It is the one that observes the crossing and sends the paused
        alert; only counts beyond the cap reject.
        """
        limiter = RateLimiter()
        org = self._org()

        with (
            patch("webhooks.services.rate_limiter.cache") as mock_cache,
            patch("core.services.usage_alerts.maybe_send_usage_alerts") as mock_alerts,
        ):
            mock_cache.get.return_value = 39
            mock_cache.incr.return_value = 40
            info = limiter.enforce_rate_limit(org)

        assert info["current_usage"] == 40
        mock_alerts.assert_called_once_with(org, 40, 20, hard_at=40)

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

        mock_alerts.assert_called_once_with(org, 16, 20, hard_at=None)

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

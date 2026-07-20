"""Tests for currency-aware amount conversion and display formatting.

Covers the ``webhooks.utils.currency`` module and its integration into
the Stripe parser, the Chargify parser, the notification builder
headlines, and the ARR display (issue #79): zero-decimal currencies must
not be divided by 100, and non-USD amounts must not render as dollars.
"""

from decimal import Decimal
from typing import Any

import pytest
from plugins.sources.chargify import ChargifySourcePlugin
from plugins.sources.stripe import StripeSourcePlugin
from webhooks.models.rich_notification import PaymentInfo
from webhooks.services.notification_builder import NotificationBuilder
from webhooks.utils.currency import (
    THREE_DECIMAL_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
    currency_decimals,
    format_money,
    from_minor_units,
)


class TestFromMinorUnits:
    """Unit tests for the minor-unit to Decimal conversion."""

    def test_two_decimal_currency_divides_by_100(self) -> None:
        """Test that USD cents convert to exact Decimal dollars."""
        assert from_minor_units(2999, "USD") == Decimal("29.99")

    def test_zero_decimal_currency_is_not_divided(self) -> None:
        """Test that a JPY amount stays in whole yen."""
        assert from_minor_units(1000, "JPY") == Decimal("1000")

    def test_three_decimal_currency_divides_by_1000(self) -> None:
        """Test that BHD thousandths convert to exact Decimal dinars."""
        assert from_minor_units(12345, "BHD") == Decimal("12.345")

    def test_three_decimal_currency_small_amount(self) -> None:
        """Test that sub-unit KWD amounts keep three decimal places."""
        assert from_minor_units(500, "KWD") == Decimal("0.500")

    def test_currency_code_is_case_insensitive(self) -> None:
        """Test that lower-case codes (Stripe style) are recognized."""
        assert from_minor_units(1000, "jpy") == Decimal("1000")

    def test_string_amounts_are_accepted(self) -> None:
        """Test that form-encoded string amounts convert correctly."""
        assert from_minor_units("2999", "EUR") == Decimal("29.99")

    def test_currency_sets_are_disjoint(self) -> None:
        """Test that the zero- and three-decimal sets do not overlap."""
        assert not ZERO_DECIMAL_CURRENCIES & THREE_DECIMAL_CURRENCIES

    @pytest.mark.parametrize(
        ("currency", "expected"),
        [("USD", 2), ("JPY", 0), ("KRW", 0), ("BHD", 3), ("EUR", 2)],
    )
    def test_currency_decimals(self, currency: str, expected: int) -> None:
        """Test the decimal exponent lookup.

        Args:
            currency: ISO 4217 currency code.
            expected: Expected number of decimal places.
        """
        assert currency_decimals(currency) == expected


class TestFormatMoney:
    """Unit tests for currency display formatting."""

    def test_usd_uses_dollar_symbol(self) -> None:
        """Test that USD renders with a dollar sign and two decimals."""
        assert format_money(Decimal("29.99"), "USD") == "$29.99"

    def test_eur_uses_euro_symbol(self) -> None:
        """Test that EUR renders with a euro sign."""
        assert format_money(29.99, "EUR") == "€29.99"

    def test_jpy_renders_without_decimals(self) -> None:
        """Test that zero-decimal JPY renders as whole yen with grouping."""
        assert format_money(Decimal("1000"), "JPY") == "¥1,000"

    def test_unknown_currency_falls_back_to_code(self) -> None:
        """Test that currencies without a symbol render as CODE amount."""
        assert format_money(42, "CHF") == "CHF 42.00"

    def test_negative_amounts_keep_sign_before_symbol(self) -> None:
        """Test that negative amounts render with a leading minus."""
        assert format_money(-5, "USD") == "-$5.00"

    def test_decimals_override(self) -> None:
        """Test that the decimals override drops the fractional part."""
        assert format_money(3588.0, "EUR", 0) == "€3,588"


@pytest.mark.django_db
class TestStripeCurrencyParsing:
    """Stripe parser must respect the event's currency (no blind /100)."""

    @pytest.fixture
    def plugin(self) -> StripeSourcePlugin:
        """Create a Stripe plugin instance for testing."""
        return StripeSourcePlugin(webhook_secret="whsec_test")

    def test_jpy_invoice_amount_not_divided(self, plugin: StripeSourcePlugin) -> None:
        """Test that a ¥1000 JPY invoice yields Decimal("1000")."""
        data: dict[str, Any] = {
            "id": "in_test_jpy",
            "customer": "cus_test",
            "currency": "jpy",
            "amount_paid": 1000,
            "billing_reason": "subscription_create",
        }

        amount = plugin._handle_stripe_billing("payment_success", data)

        assert amount == Decimal("1000")
        assert format_money(amount, "JPY") == "¥1,000"

    def test_jpy_event_data_carries_currency_and_amount(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that the built event keeps JPY and the undivided amount."""
        data: dict[str, Any] = {
            "id": "in_test_jpy",
            "customer": "cus_test",
            "currency": "jpy",
            "amount_paid": 1000,
        }

        amount = plugin._handle_stripe_billing("payment_success", data)
        event_data = plugin._build_stripe_event_data(
            "payment_success", "cus_test", data, amount
        )

        assert event_data["currency"] == "JPY"
        assert event_data["amount"] == 1000.0

    def test_usd_invoice_still_divided_by_100(self, plugin: StripeSourcePlugin) -> None:
        """Test that USD invoices still convert cents to dollars."""
        data: dict[str, Any] = {
            "id": "in_test_usd",
            "customer": "cus_test",
            "currency": "usd",
            "amount_paid": 2999,
        }

        amount = plugin._handle_stripe_billing("payment_success", data)

        assert amount == Decimal("29.99")

    def test_trial_plan_amount_uses_event_currency(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that trial plan_amount metadata respects JPY."""
        data: dict[str, Any] = {
            "currency": "jpy",
            "_is_trial": True,
            "_plan_amount_cents": 5000,
        }
        metadata: dict[str, Any] = {}

        plugin._add_trial_metadata(metadata, data)

        assert metadata["plan_amount"] == 5000.0

    def test_previous_amount_uses_event_currency(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that previous_amount metadata respects JPY."""
        data: dict[str, Any] = {
            "id": "sub_test",
            "currency": "jpy",
            "plan": {"interval": "month"},
            "_previous_attributes": {"plan": {"amount": 5000}},
        }
        metadata: dict[str, Any] = {}

        plugin._add_subscription_metadata(metadata, "subscription_updated", data)

        assert metadata["previous_amount"] == 5000.0

    def test_previous_plan_currency_and_interval_stored(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that the previous plan's currency and interval are kept
        in metadata so formatters can render the old side correctly."""
        data: dict[str, Any] = {
            "id": "sub_test",
            "currency": "usd",
            "plan": {"interval": "month"},
            "_previous_attributes": {
                "plan": {"amount": 5000, "currency": "eur", "interval": "year"}
            },
        }
        metadata: dict[str, Any] = {}

        plugin._add_subscription_metadata(metadata, "subscription_updated", data)

        assert metadata["previous_amount"] == 50.0
        assert metadata["previous_currency"] == "EUR"
        assert metadata["previous_billing_period"] == "annual"

    def test_zero_trial_plan_amount_kept_in_metadata(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that a $0 trial plan amount is surfaced, not dropped."""
        data: dict[str, Any] = {
            "currency": "usd",
            "_is_trial": True,
            "_plan_amount_cents": 0,
        }
        metadata: dict[str, Any] = {}

        plugin._add_trial_metadata(metadata, data)

        assert metadata["plan_amount"] == 0.0

    def test_nested_plan_currency_used_when_top_level_missing(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that a subscription without top-level currency reads the
        plan's currency and converts accordingly."""
        data: dict[str, Any] = {
            "id": "sub_test",
            "customer": "cus_test",
            "status": "active",
            "plan": {"amount": 5000, "currency": "jpy", "interval": "month"},
        }

        amount = plugin._handle_stripe_billing("subscription_created", data)

        assert plugin._event_currency(data) == "JPY"
        assert amount == Decimal("5000")

    def test_nested_item_price_currency_used_as_fallback(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that item price currency is found for multi-item
        subscriptions with no top-level or plan currency."""
        data: dict[str, Any] = {
            "plan": None,
            "items": {
                "data": [
                    {"price": {"unit_amount": 2999, "currency": "eur"}},
                ]
            },
        }

        assert plugin._event_currency(data) == "EUR"

    def test_missing_currency_everywhere_defaults_to_usd(
        self, plugin: StripeSourcePlugin
    ) -> None:
        """Test that USD remains the default with no currency anywhere."""
        assert plugin._event_currency({"plan": {"amount": 1000}}) == "USD"


class TestChargifyCurrencyParsing:
    """Chargify parser must thread the payload currency through."""

    @pytest.fixture
    def plugin(self) -> ChargifySourcePlugin:
        """Create a Chargify plugin instance for testing."""
        return ChargifySourcePlugin(webhook_secret="test_secret")

    @pytest.fixture
    def eur_payment_data(self) -> dict[str, str]:
        """Form data for a €29.99 EUR payment_success webhook."""
        return {
            "event": "payment_success",
            "payload[subscription][id]": "sub_123",
            "payload[subscription][currency]": "EUR",
            "payload[subscription][customer][id]": "cust_456",
            "payload[subscription][customer][email]": "test@example.com",
            "payload[subscription][customer][organization]": "Acme GmbH",
            "payload[subscription][product][name]": "Pro Plan",
            "payload[transaction][id]": "txn_789",
            "payload[transaction][amount_in_cents]": "2999",
        }

    def test_eur_payment_carries_currency(
        self, plugin: ChargifySourcePlugin, eur_payment_data: dict[str, str]
    ) -> None:
        """Test that a €29.99 payment parses with EUR and displays €."""
        plugin._current_webhook_data = eur_payment_data

        result = plugin._parse_payment_success(eur_payment_data)

        assert result["currency"] == "EUR"
        assert result["amount"] == 29.99
        assert format_money(result["amount"], result["currency"]) == "€29.99"

    def test_transaction_currency_used_as_fallback(
        self, plugin: ChargifySourcePlugin, eur_payment_data: dict[str, str]
    ) -> None:
        """Test that the transaction currency is read when the
        subscription currency is absent."""
        del eur_payment_data["payload[subscription][currency]"]
        eur_payment_data["payload[transaction][currency]"] = "GBP"
        plugin._current_webhook_data = eur_payment_data

        result = plugin._parse_payment_success(eur_payment_data)

        assert result["currency"] == "GBP"

    def test_missing_currency_falls_back_to_usd_with_warning(
        self,
        plugin: ChargifySourcePlugin,
        eur_payment_data: dict[str, str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that a payload without currency falls back to USD and
        logs a warning."""
        del eur_payment_data["payload[subscription][currency]"]
        plugin._current_webhook_data = eur_payment_data

        with caplog.at_level("WARNING"):
            result = plugin._parse_payment_success(eur_payment_data)

        assert result["currency"] == "USD"
        assert any("falling back to USD" in r.message for r in caplog.records)

    def test_payment_failure_carries_currency(
        self, plugin: ChargifySourcePlugin, eur_payment_data: dict[str, str]
    ) -> None:
        """Test that payment_failure events also carry the currency."""
        eur_payment_data["payload[transaction][failure_message]"] = "Card declined"
        plugin._current_webhook_data = eur_payment_data

        result = plugin._parse_payment_failure(eur_payment_data)

        assert result["currency"] == "EUR"
        assert result["amount"] == 29.99


class TestHeadlineCurrencyFormatting:
    """Notification headlines must use the event currency and interval."""

    @pytest.fixture
    def builder(self) -> NotificationBuilder:
        """Create a NotificationBuilder instance."""
        return NotificationBuilder()

    @pytest.fixture
    def customer_data(self) -> dict[str, Any]:
        """Minimal customer data for building notifications."""
        return {"email": "test@example.com", "first_name": "Jane"}

    def test_eur_payment_headline_shows_euro(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that a EUR payment headline renders with a euro sign."""
        event_data = {
            "type": "payment_success",
            "provider": "chargify",
            "amount": 29.99,
            "currency": "EUR",
            "metadata": {},
        }

        result = builder.build(event_data, customer_data)

        assert "€29.99" in result.headline
        assert "$" not in result.headline

    def test_jpy_payment_headline_has_no_decimals(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that a JPY payment headline renders whole yen."""
        event_data = {
            "type": "payment_success",
            "provider": "stripe",
            "amount": 1000.0,
            "currency": "JPY",
            "metadata": {},
        }

        result = builder.build(event_data, customer_data)

        assert "¥1,000 received" == result.headline

    def test_annual_upgrade_headline_renders_per_year(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that an annual subscription upgrade renders /yr."""
        event_data = {
            "type": "subscription_updated",
            "provider": "stripe",
            "amount": 1188.0,
            "currency": "USD",
            "metadata": {
                "change_direction": "upgrade",
                "billing_period": "annual",
            },
        }

        result = builder.build(event_data, customer_data)

        assert "/yr" in result.headline
        assert "/mo" not in result.headline

    def test_monthly_fallback_when_billing_period_missing(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that a missing billing period falls back to /mo."""
        event_data = {
            "type": "subscription_updated",
            "provider": "stripe",
            "amount": 99.0,
            "currency": "USD",
            "metadata": {"change_direction": "upgrade"},
        }

        result = builder.build(event_data, customer_data)

        assert "/mo" in result.headline

    def test_quarterly_upgrade_headline_uses_qtr_suffix(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that quarterly billing renders the unified /qtr suffix."""
        event_data = {
            "type": "subscription_updated",
            "provider": "stripe",
            "amount": 300.0,
            "currency": "USD",
            "metadata": {
                "change_direction": "upgrade",
                "billing_period": "quarterly",
            },
        }

        result = builder.build(event_data, customer_data)

        assert "/qtr" in result.headline

    def test_zero_amount_payment_headline_renders_amount(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that a $0 payment renders "$0.00 received", not the
        amountless fallback."""
        event_data = {
            "type": "payment_success",
            "provider": "stripe",
            "amount": 0.0,
            "currency": "USD",
            "metadata": {},
        }

        result = builder.build(event_data, customer_data)

        assert result.headline == "$0.00 received"

    def test_upgrade_headline_uses_previous_currency_for_old_side(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that a EUR-to-USD upgrade shows each side's currency."""
        event_data = {
            "type": "subscription_updated",
            "provider": "stripe",
            "amount": 120.0,
            "currency": "USD",
            "metadata": {
                "change_direction": "upgrade",
                "previous_amount": 100.0,
                "previous_currency": "EUR",
                "billing_period": "monthly",
            },
        }

        result = builder.build(event_data, customer_data)

        assert result.headline == "Upgraded: €100.00/mo to $120.00/mo"

    def test_downgrade_headline_uses_previous_interval_for_old_side(
        self, builder: NotificationBuilder, customer_data: dict[str, Any]
    ) -> None:
        """Test that an annual-to-monthly downgrade shows /yr on the
        old side and /mo on the new side."""
        event_data = {
            "type": "subscription_updated",
            "provider": "stripe",
            "amount": 99.0,
            "currency": "USD",
            "metadata": {
                "change_direction": "downgrade",
                "previous_amount": 1188.0,
                "previous_billing_period": "annual",
                "billing_period": "monthly",
            },
        }

        result = builder.build(event_data, customer_data)

        assert result.headline == "Downgraded: $1,188.00/yr to $99.00/mo"


class TestLtvCurrencyDisplay:
    """LTV display must use the event's currency, not a hardcoded $."""

    @pytest.fixture
    def builder(self) -> NotificationBuilder:
        """Create a NotificationBuilder instance."""
        return NotificationBuilder()

    def test_eur_ltv_abbreviated_with_symbol(
        self, builder: NotificationBuilder
    ) -> None:
        """Test that a large EUR lifetime value renders as €7.1k."""
        assert builder._format_ltv(7100.0, "EUR") == "€7.1k"

    def test_unknown_currency_ltv_falls_back_to_code(
        self, builder: NotificationBuilder
    ) -> None:
        """Test that currencies without a symbol render as CODE 7.1k."""
        assert builder._format_ltv(7100.0, "CHF") == "CHF 7.1k"

    def test_small_ltv_renders_in_currency(self, builder: NotificationBuilder) -> None:
        """Test that sub-1000 lifetime values keep the currency symbol."""
        assert builder._format_ltv(150.0, "EUR") == "€150"

    def test_ltv_display_uses_event_currency(
        self, builder: NotificationBuilder
    ) -> None:
        """Test that the built notification's LTV follows the event
        currency end to end."""
        event_data = {
            "type": "payment_success",
            "provider": "stripe",
            "amount": 100.0,
            "currency": "EUR",
            "metadata": {},
        }
        customer_data = {"email": "test@example.com", "total_spent": 7100.0}

        result = builder.build(event_data, customer_data)

        assert result.customer is not None
        assert result.customer.ltv_display == "€7.1k"


class TestArrCurrencyDisplay:
    """ARR display must use the same currency on both sides."""

    def test_monthly_eur_arr_uses_euro_both_sides(self) -> None:
        """Test that a monthly EUR payment shows EUR ARR, not dollars."""
        payment = PaymentInfo(amount=299.0, currency="EUR", interval="monthly")

        assert payment.format_amount_with_arr() == "€299.00/mo = €3,588 ARR"

    def test_quarterly_arr_uses_same_currency(self) -> None:
        """Test that quarterly ARR keeps the payment currency."""
        payment = PaymentInfo(amount=300.0, currency="GBP", interval="quarterly")

        assert payment.format_amount_with_arr() == "£300.00/qtr = £1,200 ARR"

    def test_annual_amount_formatted_in_currency(self) -> None:
        """Test that annual amounts render in their own currency."""
        payment = PaymentInfo(amount=1188.0, currency="USD", interval="annual")

        assert payment.format_amount_with_arr() == "$1,188.00/yr ARR"

    def test_zero_decimal_currency_amount(self) -> None:
        """Test that JPY amounts render without a fractional part."""
        payment = PaymentInfo(amount=10000.0, currency="JPY", interval="monthly")

        assert payment.format_amount_with_arr() == "¥10,000/mo = ¥120,000 ARR"

    def test_zero_amount_recurring_keeps_interval_and_arr(self) -> None:
        """Test that a $0 monthly plan still renders /mo and $0 ARR."""
        payment = PaymentInfo(amount=0.0, currency="USD", interval="monthly")

        assert payment.format_amount_with_arr() == "$0.00/mo = $0 ARR"

    def test_non_recurring_amount_has_no_arr(self) -> None:
        """Test that one-time payments render the bare amount only."""
        payment = PaymentInfo(amount=50.0, currency="USD", interval=None)

        assert payment.format_amount_with_arr() == "$50.00"

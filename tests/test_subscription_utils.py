"""Tests for the shared Stripe subscription payload extraction utils.

Pins the module contract: every extractor returns None when the payload
does not prove the value — including malformed amounts, which must
degrade rather than raise out of webhook processing.
"""

from typing import Any

from webhooks.utils.subscriptions import (
    extract_item_amount,
    subscription_currency,
    subscription_recurring_amount_cents,
    subscription_recurring_interval,
    sum_item_amounts,
)


class TestAmountExtraction:
    """Amounts come from proven payload fields or not at all."""

    def test_item_amount_prefers_legacy_plan_shape(self) -> None:
        """item.plan.amount wins over item.price.unit_amount."""
        item: dict[str, Any] = {
            "plan": {"amount": 2900},
            "price": {"unit_amount": 100},
        }
        assert extract_item_amount(item) == 2900

    def test_item_amount_reads_prices_api_shape(self) -> None:
        """item.price.unit_amount is used when plan carries no amount."""
        item: dict[str, Any] = {"price": {"unit_amount": 1500}}
        assert extract_item_amount(item) == 1500

    def test_malformed_item_amount_falls_through_not_raises(self) -> None:
        """A non-numeric plan amount degrades to the price shape."""
        item: dict[str, Any] = {
            "plan": {"amount": "not-a-number"},
            "price": {"unit_amount": 1500},
        }
        assert extract_item_amount(item) == 1500

    def test_malformed_amounts_everywhere_yield_none(self) -> None:
        """Non-numeric amounts in every shape return None, never raise."""
        item: dict[str, Any] = {
            "plan": {"amount": "abc"},
            "price": {"unit_amount": [2900]},
        }
        assert extract_item_amount(item) is None

    def test_boolean_amount_is_not_a_number(self) -> None:
        """True must not be silently read as 1 cent."""
        assert extract_item_amount({"plan": {"amount": True}}) is None

    def test_subscription_amount_malformed_plan_yields_none(self) -> None:
        """A malformed top-level plan amount returns None, never raises."""
        sub: dict[str, Any] = {"plan": {"amount": "abc"}}
        assert subscription_recurring_amount_cents(sub) is None

    def test_subscription_amount_sums_items_with_quantity(self) -> None:
        """Item amounts multiply by quantity and sum across items."""
        sub: dict[str, Any] = {
            "items": {
                "data": [
                    {"price": {"unit_amount": 1000}, "quantity": 2},
                    {"plan": {"amount": 500}},
                ]
            }
        }
        assert subscription_recurring_amount_cents(sub) == 2500

    def test_sum_skips_malformed_items(self) -> None:
        """Malformed items are skipped; the sum uses the provable ones."""
        items: list[Any] = [
            {"price": {"unit_amount": "abc"}},
            {"price": {"unit_amount": 1000}},
            "not-a-dict",
        ]
        assert sum_item_amounts(items) == 1000


class TestCurrencyAndInterval:
    """Currency and interval come from known payload shapes only."""

    def test_currency_prefers_top_level(self) -> None:
        """The subscription's own currency field wins."""
        sub: dict[str, Any] = {
            "currency": "eur",
            "plan": {"currency": "usd"},
        }
        assert subscription_currency(sub) == "eur"

    def test_currency_from_item_price(self) -> None:
        """Prices-API payloads carry currency on the item price."""
        sub: dict[str, Any] = {"items": {"data": [{"price": {"currency": "jpy"}}]}}
        assert subscription_currency(sub) == "jpy"

    def test_no_currency_anywhere_is_none(self) -> None:
        """No currency in any shape returns None — never a guess."""
        assert subscription_currency({"plan": {"amount": 2900}}) is None

    def test_interval_from_legacy_plan(self) -> None:
        """The legacy top-level plan interval is read first."""
        sub: dict[str, Any] = {"plan": {"interval": "month"}}
        assert subscription_recurring_interval(sub) == "month"

    def test_interval_from_item_price_recurring(self) -> None:
        """Prices-API payloads carry interval under price.recurring."""
        sub: dict[str, Any] = {
            "items": {"data": [{"price": {"recurring": {"interval": "year"}}}]}
        }
        assert subscription_recurring_interval(sub) == "year"

    def test_no_interval_anywhere_is_none(self) -> None:
        """No interval in any shape returns None."""
        assert subscription_recurring_interval({"plan": {"amount": 1}}) is None

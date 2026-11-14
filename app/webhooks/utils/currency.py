"""Currency-aware money conversion and display formatting.

Payment providers send amounts in each currency's *minor* unit: cents
for USD/EUR, whole yen for JPY (a zero-decimal currency), thousandths
of a dinar for BHD (a three-decimal currency). This module centralizes
the minor-unit exponents and display formatting so parsers never assume
"divide by 100" and formatters never assume a dollar sign.

The currency sets mirror Stripe's documented zero-decimal and
three-decimal currencies (https://stripe.com/docs/currencies).

This module is intentionally stdlib-only (no babel, no Django) so both
the plugin layer (``plugins.sources``) and the webhooks services can
import it without adding dependencies or layering concerns.
"""

from decimal import Decimal

# Currencies whose minor unit equals the major unit (no decimal places).
ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)

# Currencies with three decimal places (1000 minor units per major unit).
THREE_DECIMAL_CURRENCIES: frozenset[str] = frozenset(
    {
        "BHD",
        "JOD",
        "KWD",
        "OMR",
        "TND",
    }
)

# Display symbols for the currencies we realistically see. Codes not
# listed here fall back to "CODE amount" (e.g. "CHF 42.00"), which is
# unambiguous without needing a full locale database.
CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "KRW": "₩",
    "VND": "₫",
    "INR": "₹",
}


def currency_decimals(currency: str) -> int:
    """Return the number of decimal places for a currency's minor unit.

    Args:
        currency: ISO 4217 currency code (case-insensitive).

    Returns:
        0 for zero-decimal currencies, 3 for three-decimal currencies,
        2 otherwise.
    """
    code = (currency or "").upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def from_minor_units(amount_minor: int | str | Decimal, currency: str) -> Decimal:
    """Convert a provider minor-unit amount to a major-unit Decimal.

    A Stripe JPY invoice of ¥1000 arrives as ``amount_paid=1000`` and
    must stay ``Decimal("1000")``, while a USD invoice of $10.00 arrives
    as ``1000`` and becomes ``Decimal("10.00")``.

    Args:
        amount_minor: Amount in the currency's minor unit (Stripe/Chargify
            "cents"). Strings are accepted for form-encoded payloads.
        currency: ISO 4217 currency code (case-insensitive).

    Returns:
        Amount in major units as an exact Decimal.

    Raises:
        decimal.InvalidOperation: If ``amount_minor`` is not numeric.
    """
    return Decimal(str(amount_minor)).scaleb(-currency_decimals(currency))


def format_money(
    amount: Decimal | float | int,
    currency: str,
    decimals: int | None = None,
) -> str:
    """Format a major-unit amount for display in its currency.

    Uses the currency's symbol when known (``$29.99``, ``¥1,000``) and
    falls back to ``"CODE amount"`` (``CHF 42.00``) otherwise. The number
    of decimals defaults to the currency's minor-unit exponent, so
    zero-decimal currencies render without a fractional part.

    Args:
        amount: Amount in major units.
        currency: ISO 4217 currency code (case-insensitive).
        decimals: Optional override for the number of decimal places
            (e.g. 0 for whole-number ARR displays).

    Returns:
        Human-readable money string.
    """
    code = (currency or "USD").upper()
    if decimals is None:
        decimals = currency_decimals(code)

    sign = "-" if amount < 0 else ""
    magnitude = f"{abs(amount):,.{decimals}f}"

    symbol = CURRENCY_SYMBOLS.get(code)
    if symbol:
        return f"{sign}{symbol}{magnitude}"
    return f"{sign}{code} {magnitude}"


# Rough, deliberately static "local units per 1 USD" factors, used only to
# scale insight thresholds (large payment, VIP, at-risk, LTV milestones)
# that are defined in USD. They are not exchange rates and must never be
# used to convert money for display. Currencies within ~1.6x of the dollar
# keep the nominal threshold so milestones stay round ("€5,000"); only
# high-denomination currencies are scaled, to round factors so scaled
# milestones are round too ("¥750,000"). Currencies not listed here have
# no threshold at all: callers must skip the insight rather than compare
# a ¥ or ₩ amount against a dollar threshold.
APPROX_UNITS_PER_USD: dict[str, int] = {
    "USD": 1,
    "EUR": 1,
    "GBP": 1,
    "CAD": 1,
    "AUD": 1,
    "CHF": 1,
    "JPY": 150,
    "KRW": 1400,
    "INR": 85,
    "SEK": 10,
    "NOK": 10,
    "DKK": 7,
    "BRL": 5,
    "MXN": 20,
}


def usd_threshold_in(amount_usd: float, currency: str) -> float | None:
    """Scale a USD-denominated threshold into ``currency``'s major units.

    Args:
        amount_usd: Threshold in US dollars (major units).
        currency: ISO 4217 currency code (case-insensitive).

    Returns:
        The roughly equivalent threshold in ``currency``, or None when the
        currency has no approximate factor (the caller should skip the
        threshold-based insight).
    """
    factor = APPROX_UNITS_PER_USD.get((currency or "").upper())
    if factor is None:
        return None
    return amount_usd * factor

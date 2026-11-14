"""Billing period normalization shared by source parsers and formatters.

Providers describe a billing cycle as a unit times a count: Stripe sends
``interval="month", interval_count=3`` for a quarterly price, Chargify
sends ``interval_unit="month", interval=6`` for a half-yearly product.
Parsers turn that pair into a single ``billing_period`` string with
:func:`billing_period_from_interval`; formatters derive the headline
suffix and the ARR multiplier from that string with
:func:`period_suffix` and :func:`period_months`.

Period vocabulary:

* single-unit cycles keep their established names: ``"daily"``,
  ``"weekly"``, ``"monthly"``, ``"quarterly"`` (3 months) and
  ``"annual"`` (12 months or 1 year);
* any other multiple is ``"<count> <unit>s"``, e.g. ``"6 months"``,
  ``"2 weeks"``, ``"2 years"``.

This module is stdlib-only so the plugin layer and the webhooks models
can both import it.
"""

import re
from typing import Any

_UNITS: frozenset[str] = frozenset({"day", "week", "month", "year"})

# Established names for (unit, count) pairs; everything else is spelled
# "<count> <unit>s".
_NAMED_PERIODS: dict[tuple[str, int], str] = {
    ("day", 1): "daily",
    ("week", 1): "weekly",
    ("month", 1): "monthly",
    ("month", 3): "quarterly",
    ("month", 12): "annual",
    ("year", 1): "annual",
}

# Accepted spellings of named periods -> (unit, count).
_PERIOD_ALIASES: dict[str, tuple[str, int]] = {
    "daily": ("day", 1),
    "day": ("day", 1),
    "weekly": ("week", 1),
    "week": ("week", 1),
    "monthly": ("month", 1),
    "month": ("month", 1),
    "quarterly": ("month", 3),
    "quarter": ("month", 3),
    "annual": ("year", 1),
    "annually": ("year", 1),
    "yearly": ("year", 1),
    "year": ("year", 1),
}

_COUNTED_PERIOD_RE = re.compile(r"^(\d+)\s*(day|week|month|year)s?$")

_UNIT_ABBREVIATIONS: dict[str, str] = {
    "day": "day",
    "week": "wk",
    "month": "mo",
    "year": "yr",
}


def _normalize_unit(unit: Any) -> str | None:
    """Return a singular lowercase unit name, or None when unsupported."""
    if not isinstance(unit, str):
        return None
    normalized = unit.strip().lower()
    if normalized.endswith("s"):
        normalized = normalized[:-1]
    return normalized if normalized in _UNITS else None


def _positive_int(value: Any) -> int | None:
    """Coerce a payload count to a positive int, or None when invalid."""
    if isinstance(value, bool):
        return None
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return count if count >= 1 else None


def billing_period_from_interval(unit: Any, count: Any = 1) -> str | None:
    """Build a billing period string from a provider's unit and count.

    Args:
        unit: Interval unit ("day", "week", "month", "year"; plural and
            case variants accepted).
        count: Number of units per cycle. None means 1.

    Returns:
        A billing period such as "monthly", "quarterly", "6 months", or
        None when the unit or count is not usable.
    """
    normalized_unit = _normalize_unit(unit)
    if normalized_unit is None:
        return None
    normalized_count = 1 if count is None else _positive_int(count)
    if normalized_count is None:
        return None
    named = _NAMED_PERIODS.get((normalized_unit, normalized_count))
    if named:
        return named
    return f"{normalized_count} {normalized_unit}s"


def parse_billing_period(period: Any) -> tuple[str, int] | None:
    """Parse a billing period string back into (unit, count).

    Args:
        period: Billing period value from event metadata.

    Returns:
        (unit, count) such as ("month", 3), or None when unrecognized.
    """
    if not isinstance(period, str):
        return None
    text = period.strip().lower()
    alias = _PERIOD_ALIASES.get(text)
    if alias:
        return alias
    match = _COUNTED_PERIOD_RE.fullmatch(text)
    if not match:
        return None
    count = int(match.group(1))
    if count < 1:
        return None
    return match.group(2), count


def period_months(period: Any) -> int | None:
    """Return a billing period's length in whole months.

    Only month- and year-based periods have a whole-month length; day-
    and week-based periods return None (no ARR is derived from them).

    Args:
        period: Billing period value from event metadata.

    Returns:
        Number of months per billing cycle, or None.
    """
    parsed = parse_billing_period(period)
    if parsed is None:
        return None
    unit, count = parsed
    if unit == "month":
        return count
    if unit == "year":
        return 12 * count
    return None


def period_suffix(period: Any) -> str | None:
    """Return the headline suffix for a billing period.

    Args:
        period: Billing period value from event metadata.

    Returns:
        "/mo", "/qtr", "/yr", "/wk", "/day" for named periods,
        "/<count><unit>" (e.g. "/6mo") otherwise, or None when the period
        is not recognized.
    """
    parsed = parse_billing_period(period)
    if parsed is None:
        return None
    unit, count = parsed
    if (unit, count) == ("month", 3):
        return "/qtr"
    if (unit, count) in (("month", 12), ("year", 1)):
        return "/yr"
    abbreviation = _UNIT_ABBREVIATIONS[unit]
    if count == 1:
        return f"/{abbreviation}"
    return f"/{count}{abbreviation}"

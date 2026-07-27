"""Tests for StripeObject serialization helpers in core.services.stripe.

With stripe 15.x, StripeObject is no longer a dict subclass: passing it to
dict() or calling .get() on it raises at runtime. These tests exercise the
helpers with real StripeObject instances to lock in that behavior.
"""

from typing import Any

import pytest
from core.services.stripe import _metadata_to_dict, _product_to_dict
from stripe import StripeObject


def _stripe_object(values: dict[str, Any]) -> StripeObject:
    """Build a real StripeObject from raw API-shaped values.

    Args:
        values: Raw field values as returned by the Stripe API.

    Returns:
        A StripeObject constructed from the values.
    """
    return StripeObject.construct_from(values, "sk_test_dummy")


class TestMetadataToDict:
    """Tests for _metadata_to_dict."""

    def test_stripe_object_metadata(self) -> None:
        """A real StripeObject is converted via to_dict()."""
        metadata = _stripe_object({"metadata": {"plan_name": "basic"}}).metadata

        assert isinstance(metadata, StripeObject)
        assert _metadata_to_dict(metadata) == {"plan_name": "basic"}

    def test_stripe_object_is_not_a_mapping(self) -> None:
        """Document the stripe 15.x behavior the helper exists to avoid."""
        metadata = _stripe_object({"metadata": {"plan_name": "basic"}}).metadata

        unsafe = (KeyError, AttributeError, TypeError)
        with pytest.raises(unsafe):
            dict(metadata)  # type: ignore[call-overload]
        with pytest.raises(unsafe):
            metadata.get("plan_name")

    def test_plain_dict_passthrough(self) -> None:
        """Plain mappings (e.g. test doubles) are converted with dict()."""
        assert _metadata_to_dict({"a": "b"}) == {"a": "b"}

    def test_none_and_empty(self) -> None:
        """Falsy metadata becomes an empty dict."""
        assert _metadata_to_dict(None) == {}
        assert _metadata_to_dict({}) == {}


class TestProductToDict:
    """Tests for _product_to_dict."""

    def test_full_product(self) -> None:
        """All fields are serialized, metadata as a plain dict."""
        product = _stripe_object(
            {
                "id": "prod_1",
                "name": "Basic",
                "description": "Basic plan",
                "metadata": {"plan_name": "basic"},
                "active": True,
            }
        )

        assert _product_to_dict(product) == {
            "id": "prod_1",
            "name": "Basic",
            "description": "Basic plan",
            "metadata": {"plan_name": "basic"},
            "active": True,
        }

    def test_product_without_metadata_key(self) -> None:
        """A product missing the metadata key serializes with empty metadata."""
        product = _stripe_object(
            {
                "id": "prod_2",
                "name": "Bare",
                "description": None,
                "active": False,
            }
        )

        assert _product_to_dict(product)["metadata"] == {}

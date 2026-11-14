"""Tests for tenant scoping of the Stripe customer-email cache.

Stripe customer ids come from whichever Stripe account signed the event,
so tenants can pick colliding ids. The email cache must be keyed per
workspace, or one tenant could plant the email that another tenant's
notification shows (or read another tenant's customer email).
"""

from typing import Generator

import pytest
from django.core.cache import cache
from django.test import override_settings
from plugins.sources.stripe import StripeSourcePlugin


@pytest.fixture
def locmem_cache() -> Generator[None, None, None]:
    """Back the default cache with a real in-memory cache.

    Yields:
        Nothing; the cache is cleared before and after the test.
    """
    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        cache.clear()
        yield
        cache.clear()


def test_email_cached_by_one_tenant_is_invisible_to_another(
    locmem_cache: None,
) -> None:
    """Tenant A caching an email for cus_X must not affect tenant B."""
    tenant_a = StripeSourcePlugin("whsec_a", cache_scope="ws-a")
    tenant_b = StripeSourcePlugin("whsec_b", cache_scope="ws-b")

    tenant_a._cache_customer_email("cus_SHARED", "planted@example.com")

    tenant_b._current_webhook_data = {"customer": "cus_SHARED"}
    assert tenant_b.get_customer_data("cus_SHARED")["email"] == ""

    tenant_a._current_webhook_data = {"customer": "cus_SHARED"}
    assert tenant_a.get_customer_data("cus_SHARED")["email"] == "planted@example.com"


def test_unscoped_plugin_does_not_use_the_cache(locmem_cache: None) -> None:
    """Without a tenant scope the cache is neither written nor read."""
    unscoped = StripeSourcePlugin("whsec_a")
    unscoped._cache_customer_email("cus_1", "a@example.com")
    assert unscoped._get_cached_customer_email("cus_1") == ""

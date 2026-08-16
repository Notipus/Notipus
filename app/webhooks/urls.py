"""Webhook routes.

Every webhook path accepts an optional trailing slash. Shopify strips
the trailing slash from the URI declared in an app's configuration and
delivers to the bare path, which Django's APPEND_SLASH cannot rescue for
a POST - it raises rather than redirect, since a redirect would lose the
body. That returns 500, and a provider seeing 500s retries and then
disables the subscription. Matching both forms is the fix; providers
normalise URLs however they like and none of them will change for us.
"""

from django.urls import path, re_path

from . import compliance_router, webhook_router

app_name = "webhooks"

urlpatterns = [
    # Health check
    path("health/", webhook_router.health_check, name="health_check"),
    # Shopify's mandatory privacy webhooks. App-level, not per workspace:
    # Shopify registers one URL per app, so the shop is resolved from the
    # payload. All three topics share a handler; the paths are separate
    # because the app configuration names them individually.
    re_path(
        r"^shopify/compliance/customers/data-request/?$",
        compliance_router.shopify_compliance_webhook,
        name="shopify_customer_data_request",
    ),
    re_path(
        r"^shopify/compliance/customers/redact/?$",
        compliance_router.shopify_compliance_webhook,
        name="shopify_customer_redact",
    ),
    re_path(
        r"^shopify/compliance/shop/redact/?$",
        compliance_router.shopify_compliance_webhook,
        name="shopify_shop_redact",
    ),
    # Not mandatory, but without it an uninstall leaves an active
    # integration for a store that no longer sends anything.
    re_path(
        r"^shopify/app/uninstalled/?$",
        compliance_router.shopify_compliance_webhook,
        name="shopify_app_uninstalled",
    ),
    # Shopify events for every installed shop. Subscriptions are declared
    # in the app configuration, so Shopify delivers here without Notipus
    # holding an Admin API token; the shop is identified by header.
    re_path(
        r"^shopify/events/?$",
        webhook_router.shopify_events_webhook,
        name="shopify_events_webhook",
    ),
    # Customer payment webhooks (organization-specific with UUID obfuscation)
    re_path(
        r"^customer/(?P<organization_uuid>[0-9a-f-]+)/shopify/?$",
        webhook_router.customer_shopify_webhook,
        name="customer_shopify_webhook",
    ),
    re_path(
        r"^customer/(?P<organization_uuid>[0-9a-f-]+)/chargify/?$",
        webhook_router.customer_chargify_webhook,
        name="customer_chargify_webhook",
    ),
    re_path(
        r"^customer/(?P<organization_uuid>[0-9a-f-]+)/stripe/?$",
        webhook_router.customer_stripe_webhook,
        name="customer_stripe_webhook",
    ),
    # Global billing webhooks (Notipus revenue)
    re_path(
        r"^billing/stripe/?$",
        webhook_router.billing_stripe_webhook,
        name="billing_stripe_webhook",
    ),
    # Legacy endpoints removed to enforce multi-tenancy
    # All external service webhooks must use organization-specific endpoints
]

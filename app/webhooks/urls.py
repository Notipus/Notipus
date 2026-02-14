from django.urls import path, re_path

from . import webhook_router

app_name = "webhooks"

urlpatterns = [
    # Health check
    path("health/", webhook_router.health_check, name="health_check"),
    # Customer payment webhooks (organization-specific with UUID obfuscation)
    re_path(
        r"^customer/(?P<organization_uuid>[0-9a-f-]+)/shopify/$",
        webhook_router.customer_shopify_webhook,
        name="customer_shopify_webhook",
    ),
    re_path(
        r"^customer/(?P<organization_uuid>[0-9a-f-]+)/chargify/$",
        webhook_router.customer_chargify_webhook,
        name="customer_chargify_webhook",
    ),
    re_path(
        r"^customer/(?P<organization_uuid>[0-9a-f-]+)/stripe/$",
        webhook_router.customer_stripe_webhook,
        name="customer_stripe_webhook",
    ),
    re_path(
        r"^customer/(?P<organization_uuid>[0-9a-f-]+)/zendesk/$",
        webhook_router.customer_zendesk_webhook,
        name="customer_zendesk_webhook",
    ),
    # Global billing webhooks (Notipus revenue)
    path(
        "billing/stripe/",
        webhook_router.billing_stripe_webhook,
        name="billing_stripe_webhook",
    ),
    # Legacy endpoints removed to enforce multi-tenancy
    # All external service webhooks must use organization-specific endpoints
]

"""Every webhook path must accept a missing trailing slash.

Shopify strips the trailing slash from the URI declared in an app's
configuration: a subscription registered for ``/webhook/shopify/events/``
is delivered to ``/webhook/shopify/events``. Django's APPEND_SLASH
cannot redirect a POST without discarding the body, so it raises instead
- a 500. Providers treat 500s as our fault, retry, and eventually
disable the subscription, which silently ends the integration.

This was found only by real store traffic, so it is pinned here.
"""

import json

import pytest
from django.test import Client
from django.urls import reverse

# Reversible route names paired with a body their view will accept.
WEBHOOK_ROUTES = [
    "webhooks:shopify_events_webhook",
    "webhooks:shopify_customer_data_request",
    "webhooks:shopify_customer_redact",
    "webhooks:shopify_shop_redact",
    "webhooks:shopify_app_uninstalled",
]


@pytest.mark.django_db
@pytest.mark.parametrize("route", WEBHOOK_ROUTES)
def test_post_without_trailing_slash_is_routed(client: Client, route: str) -> None:
    """A slash-less POST must reach the view, not raise.

    The status is deliberately not asserted: an unsigned request is
    rejected on its merits. What matters is that it is handled at all
    rather than dying in APPEND_SLASH with a 500.

    Args:
        client: Django test client.
        route: Reversible route name.
    """
    url = reverse(route).rstrip("/")

    response = client.post(
        url,
        data=json.dumps({"shop_domain": "example.myshopify.com"}),
        content_type="application/json",
        headers={"x-shopify-topic": "orders/create"},
    )

    assert response.status_code != 500
    assert response.status_code != 301


@pytest.mark.django_db
@pytest.mark.parametrize("route", WEBHOOK_ROUTES)
def test_post_with_trailing_slash_still_works(client: Client, route: str) -> None:
    """The declared form must keep working too.

    Args:
        client: Django test client.
        route: Reversible route name.
    """
    response = client.post(
        reverse(route),
        data=json.dumps({"shop_domain": "example.myshopify.com"}),
        content_type="application/json",
        headers={"x-shopify-topic": "orders/create"},
    )

    assert response.status_code != 500
    assert response.status_code != 301


@pytest.mark.django_db
def test_per_workspace_path_accepts_both_forms(client: Client) -> None:
    """The per-tenant address has the same exposure.

    A merchant registering that URL by hand in the Shopify admin gets it
    normalised the same way.
    """
    url = reverse(
        "webhooks:customer_shopify_webhook",
        kwargs={"organization_uuid": "0" * 8 + "-0000-0000-0000-" + "0" * 12},
    )

    for candidate in (url, url.rstrip("/")):
        response = client.post(
            candidate,
            data=json.dumps({}),
            content_type="application/json",
            headers={"x-shopify-topic": "orders/create"},
        )
        assert response.status_code != 500
        assert response.status_code != 301

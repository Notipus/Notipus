"""Record the Shopify setup screencast for the App Store listing.

Shopify asks for a screencast demonstrating onboarding, with clear
step-by-step instructions for setting up the app's core features, so
this follows the path a merchant takes: open the
integrations hub, connect a store, choose which events to receive, and
watch those events arrive.

The recording stops at our side of the OAuth handoff. Submitting the
form sends the browser to the merchant's own myshopify.com domain, and
Playwright cannot catch it: route handlers do not fire on redirect hops,
so the browser really does leave the machine and lands on the live
storefront - which belongs in neither a recording nor CI. The consent
screen is Shopify's own and needs no demonstrating here, so the
authorised state is established directly and the story picks up where it
matters: the store connected, and its events arriving.

Alongside the video it harvests stills at each beat via ``snap()``
(``output/shopify-setup-*.png``).
"""

import pytest
from conftest import (
    SHOPIFY_EVENTS,
    _seed_activity,
    hover_and_click,
    pace,
    play_shopify_finale,
    snap,
    type_text,
)
from core.models import Integration, WorkspaceMember
from playwright.sync_api import Page

SHOP_DOMAIN = "initech.myshopify.com"

# Each beat holds for this long so the finished video can carry YouTube
# chapters: chapters need at least three markers, the first at 0:00, and
# no two closer than ten seconds. Paced any faster - the first cut ran
# twenty-three seconds - and the video admits two chapters, one short of
# the minimum. It also gives a reviewer following the setup time to read
# each screen.
BEAT_MS = 12_000


@pytest.mark.django_db(transaction=True)
def shopify_screencast(recording_page_authed: Page) -> None:
    page = recording_page_authed

    # --- Where a merchant starts -------------------------------------------
    page.goto("/integrations/", wait_until="networkidle")
    pace(page, BEAT_MS)
    snap(page, "shopify-setup-integrations")

    # --- Connect the store -------------------------------------------------
    hover_and_click(page, page.locator("a[href*='integrate/shopify']"))
    page.wait_for_url("**/integrate/shopify/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, BEAT_MS)
    snap(page, "shopify-setup-connect")

    shop_input = page.locator("input[name='shop_url']")
    shop_input.scroll_into_view_if_needed()
    pace(page, 600)
    type_text(shop_input, SHOP_DOMAIN, delay=45)
    pace(page, BEAT_MS)

    # --- Choose what to hear about -----------------------------------------
    # The categories are the app's own configuration, and the part a
    # merchant most needs to see before installing.
    snap(page, "shopify-setup-events")
    pace(page, BEAT_MS)

    # --- The store, connected ----------------------------------------------
    # Standing in for the round trip through Shopify's consent screen.
    workspace = WorkspaceMember.objects.select_related("workspace").first().workspace
    Integration.objects.update_or_create(
        workspace=workspace,
        integration_type="shopify",
        defaults={
            # Exactly what a real connect leaves behind: the shop and the
            # chosen categories, and no credential of any kind.
            "oauth_credentials": {},
            "webhook_secret": "",
            "integration_settings": {
                "shop_domain": SHOP_DOMAIN,
                "enabled_categories": ["orders", "refunds", "fulfillment"],
            },
            "is_active": True,
        },
    )
    page.goto("/integrations/", wait_until="networkidle")
    pace(page, BEAT_MS)
    snap(page, "shopify-setup-connected")

    # --- The payoff: the store's events arriving ---------------------------
    _seed_activity(workspace, SHOPIFY_EVENTS)
    page.goto("/dashboard/", wait_until="networkidle")
    pace(page, BEAT_MS)
    snap(page, "shopify-setup-dashboard")

    # --- And the notification landing in the chat tool ---------------------
    play_shopify_finale(page, hold_ms=BEAT_MS)
    snap(page, "shopify-setup-alert")

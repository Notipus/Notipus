"""Capture Shopify App Store listing screenshots.

Shopify's listing requirements shape every choice here:

* Images must show the app's real interface, so each capture is the
  running app against a seeded demo store rather than a mockup.
* Browser windows and desktop backgrounds are forbidden, so captures are
  viewport-only from the ``shopify_listing_page`` fixture.
* Each image must be unique and show a different feature, view or state,
  so the set walks connect -> configure -> activity -> delivered message
  rather than showing one screen twice.
* Pricing is not allowed in listing images. That rules out the billing
  page the Slack set includes, and it also rules out the top of the
  dashboard, which carries a plan badge, an upgrade button and a monthly
  event allowance. The activity capture scrolls past all of it.
"""

import pytest
from conftest import OUTPUT_DIR, shoot, show_shopify_finale
from playwright.sync_api import Page


def _capture_activity(page: Page, name: str) -> None:
    """Capture the activity feed with no plan or pricing furniture in frame.

    The feed sits below a setup checklist and a usage card naming the
    plan and its event allowance, so a plain viewport shot of /dashboard/
    shows billing detail and none of the events. Scrolling the heading to
    the top of the frame puts the events in view and the pricing out of
    it.

    Args:
        page: The listing-sized page.
        name: Output file stem.
    """
    page.goto("/dashboard/", wait_until="networkidle")

    # scroll_into_view_if_needed stops the moment the element is on
    # screen: the heading sits just past the fold, so it moves barely
    # thirty pixels and lands at the bottom edge with every event still
    # below it. Scroll by the heading's own offset instead, leaving a
    # margin so the section title stays in frame above its events.
    box = page.get_by_text("Recent Activity", exact=True).bounding_box()
    assert box is not None, "Recent Activity heading not found"
    page.evaluate(f"window.scrollBy(0, {box['y'] - 60})")
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUTPUT_DIR / f"{name}.png"), full_page=False)
    print(f"captured {name}.png")


@pytest.mark.django_db(transaction=True)
def shopify_listing(shopify_listing_page: Page) -> None:
    page = shopify_listing_page

    # 1. Connecting a store and choosing which events to receive - the
    #    setup step the screencast requirement also asks to see.
    shoot(page, "shopify-listing-connect", "/integrate/shopify/", full_page=False)

    # 2. The integrations overview: what is wired up, and where the
    #    alerts will be delivered.
    shoot(page, "shopify-listing-integrations", "/integrations/", full_page=False)

    # 3. The store's own events - an order, a payment, a fulfillment, a
    #    delivery and a refund, which is what the listing describes.
    _capture_activity(page, "shopify-listing-activity")

    # 4. The result: notifications as they arrive in the chat tool. The
    #    store variant, not the SaaS billing one - a merchant should see
    #    orders and shipping, and Shopify does not allow plan pricing in
    #    listing images.
    show_shopify_finale(page)
    page.wait_for_timeout(500)
    page.screenshot(
        path=str(OUTPUT_DIR / "shopify-listing-notification.png"), full_page=False
    )
    print("captured shopify-listing-notification.png")

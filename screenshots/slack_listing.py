"""Capture Slack App Directory listing screenshots.

The directory requires screenshots that are exactly 1600x1000 pixels,
so every capture here is a viewport crop (``full_page=False``) from the
``slack_listing_page`` fixture. The set covers the dashboard hero, the
integrations page, billing, and the notification landing in a
Slack-style window.
"""

import pytest
from conftest import OUTPUT_DIR, shoot, show_slack_finale
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def slack_listing(slack_listing_page: Page) -> None:
    page = slack_listing_page

    shoot(page, "slack-listing-dashboard", "/dashboard/", full_page=False)
    shoot(page, "slack-listing-integrations", "/integrations/", full_page=False)
    shoot(page, "slack-listing-billing", "/billing/", full_page=False)

    # The notification arriving in a Slack-style window
    show_slack_finale(page)
    page.wait_for_timeout(500)
    page.screenshot(
        path=str(OUTPUT_DIR / "slack-listing-notification.png"), full_page=False
    )
    print("captured slack-listing-notification.png")

"""Capture the integrations page (desktop and mobile).

Shows connected Slack and Stripe integrations alongside the
available Shopify, Chargify, and Hunter.io connectors.
"""

import pytest
from conftest import shoot
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def integrations(page: Page, mobile_page: Page) -> None:
    shoot(page, "integrations", "/integrations/")
    shoot(mobile_page, "integrations-mobile", "/integrations/")

"""Capture the dashboard hero shot (desktop and mobile).

Shows the Initech workspace with usage stats and the enriched
Recent Activity feed: Initrode payments, Chotchkie's churn risk,
a Flingers order, Penetrode's cancellation, and Milton's Swingline
trial.
"""

import pytest
from conftest import shoot
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def dashboard(page: Page, mobile_page: Page) -> None:
    shoot(page, "dashboard", "/dashboard/")
    shoot(mobile_page, "dashboard-mobile", "/dashboard/")

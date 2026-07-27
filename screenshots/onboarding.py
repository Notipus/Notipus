"""Record the zero-to-configured onboarding screencast.

Drives the full journey a new customer takes: passkey signup as Peter
Gibbons → pick the Pro trial → create the Initech workspace → connect
Slack (OAuth mocked server-side, passkey ceremony real via a virtual
authenticator) → pick the #billing-alerts channel → connect Stripe with
a webhook signing secret → land on a dashboard full of activity.
"""

import pytest
from conftest import (
    _seed_activity,
    hover_and_click,
    intercept_slack_oauth,
    mock_slack_api,  # noqa: F401  (fixture)
    pace,
    play_slack_finale,
    type_text,
)
from core.models import Workspace
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def onboarding(recording_page: Page, mock_slack_api: None) -> None:  # noqa: F811
    page = recording_page

    # --- Sign up with a passkey -------------------------------------------
    page.goto("/accounts/signup/", wait_until="networkidle")
    pace(page, 1500)

    hover_and_click(page, page.locator("#passkey-signup"))
    pace(page, 800)

    type_text(page.locator("#modal-username"), "peter")
    pace(page, 300)
    type_text(page.locator("#modal-email"), "peter.gibbons@initech.com")
    pace(page, 500)

    # The virtual authenticator approves the passkey ceremony instantly
    hover_and_click(page, page.locator("#create-with-passkey"))
    page.wait_for_url("**/select-plan/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, 1500)

    # --- Choose the Pro trial ---------------------------------------------
    pro_button = page.locator(
        "form:has(input[name='plan'][value='pro']) button[type='submit']"
    )
    hover_and_click(page, pro_button)
    page.wait_for_load_state("networkidle")
    pace(page, 1200)

    # Plan confirmation → continue to workspace creation
    hover_and_click(page, page.get_by_role("link", name="Go to Dashboard"))
    page.wait_for_url("**/workspace/create/", timeout=15_000)
    pace(page, 1000)

    # --- Create the workspace ---------------------------------------------
    type_text(page.locator("input[name='name']"), "Initech")
    pace(page, 300)
    type_text(page.locator("input[name='shop_domain']"), "initech.com")
    pace(page, 500)
    hover_and_click(page, page.get_by_role("button", name="Create Workspace"))
    page.wait_for_url("**/dashboard/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, 2000)

    # --- Connect Slack (OAuth round-trip, mocked at the edges) ------------
    intercept_slack_oauth(page)
    hover_and_click(page, page.get_by_role("link", name="Integrations").first)
    page.wait_for_load_state("networkidle")
    pace(page, 1500)

    hover_and_click(page, page.locator("a[href*='integrate/slack']"))
    page.wait_for_load_state("networkidle")
    pace(page, 1500)

    # Pick a notification channel
    hover_and_click(page, page.get_by_role("button", name="Configure"))
    channel_select = page.locator("#slack-channel-select")
    channel_select.wait_for(state="visible", timeout=10_000)
    pace(page, 600)
    channel_select.select_option("#billing-alerts")
    pace(page, 600)
    hover_and_click(page, page.locator("#slack-config-save"))
    pace(page, 1500)

    # --- Connect Stripe ----------------------------------------------------
    hover_and_click(page, page.locator("a[href*='integrate/stripe']"))
    page.wait_for_url("**/integrate/stripe/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, 1500)

    secret_input = page.locator("input[name='webhook_secret']")
    secret_input.scroll_into_view_if_needed()
    pace(page, 800)
    type_text(secret_input, "whsec_9wK2mDemoSigningSecret", delay=30)
    pace(page, 500)
    submit = page.get_by_role("button", name="Connect Stripe")
    submit.scroll_into_view_if_needed()
    hover_and_click(page, submit)
    page.wait_for_load_state("networkidle")
    pace(page, 1800)

    # --- The payoff: a dashboard full of activity --------------------------
    _seed_activity(Workspace.objects.get(name="Initech"))
    page.goto("/dashboard/", wait_until="networkidle")
    pace(page, 3000)

    # --- And the notification landing in Slack -----------------------------
    play_slack_finale(page)

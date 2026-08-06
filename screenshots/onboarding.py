"""Record the zero-to-configured onboarding screencast.

Drives the journey a new customer takes under the "build before you
commit" onboarding: passkey signup as Peter Gibbons → land straight in
the product on an auto-provisioned free workspace → connect Slack (OAuth
mocked server-side, passkey ceremony real via a virtual authenticator) →
pick the #billing-alerts channel → connect Stripe with a webhook signing
secret → land on a dashboard full of activity.

Alongside the video, it harvests reusable stills at each beat via
``snap()`` (``output/onboarding-*.png``) for the marketing site and
listing pages.
"""

import pytest
from conftest import (
    _seed_activity,
    hover_and_click,
    intercept_slack_oauth,
    mock_slack_api,  # noqa: F401  (fixture)
    pace,
    play_slack_finale,
    snap,
    type_text,
)
from core.models import WorkspaceMember
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
    snap(page, "onboarding-signup")

    # The virtual authenticator approves the passkey ceremony instantly.
    # "Build before you commit": signup drops the user straight into the
    # product on an auto-provisioned free workspace — no plan gate and no
    # manual workspace form — landing on the integrations hub.
    hover_and_click(page, page.locator("#create-with-passkey"))
    page.wait_for_url("**/integrations/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, 1800)

    # The workspace is auto-named "<username>'s Workspace"; rename it to
    # Initech to match the Office Space theming used across the rest of the
    # screenshot suite, then reload so the UI reflects it.
    workspace = (
        WorkspaceMember.objects.select_related("workspace")
        .get(user__username="peter")
        .workspace
    )
    workspace.name = "Initech"
    workspace.save(update_fields=["name"])
    page.reload(wait_until="networkidle")
    pace(page, 1500)
    snap(page, "onboarding-integrations")

    # --- Connect Slack (OAuth round-trip, mocked at the edges) ------------
    intercept_slack_oauth(page)
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
    snap(page, "onboarding-slack-config")
    hover_and_click(page, page.locator("#slack-config-save"))
    pace(page, 1500)

    # --- Connect Stripe ----------------------------------------------------
    hover_and_click(page, page.locator("a[href*='integrate/stripe']"))
    page.wait_for_url("**/integrate/stripe/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, 1500)
    # Snapped at the top of the page, before the scroll down to the secret
    # field: a mid-flow scroll position crops off the nav and page heading,
    # which makes the still unusable outside the video.
    snap(page, "onboarding-stripe-connect")

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
    _seed_activity(workspace)
    page.goto("/dashboard/", wait_until="networkidle")
    pace(page, 3000)
    snap(page, "onboarding-dashboard")

    # --- And the notification landing in Slack -----------------------------
    play_slack_finale(page)
    snap(page, "onboarding-slack-alert")

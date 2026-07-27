"""Record the Telegram connection screencast.

Drives connecting a Telegram destination from inside the app: an
authenticated Initech owner opens Integrations → Connect Telegram, pastes
a bot token and chat ID (the Bot API is mocked server-side, so getMe/
getChat validate without leaving the machine), saves, and lands back on
Integrations with Telegram connected — then the payoff: the enriched
payment alert arriving in a Telegram chat.
"""

import pytest
from conftest import (
    hover_and_click,
    mock_telegram_api,  # noqa: F401  (fixture)
    pace,
    play_telegram_finale,
    type_text,
)
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def telegram(recording_page_authed: Page, mock_telegram_api: None) -> None:  # noqa: F811
    page = recording_page_authed

    # --- Start on the integrations hub ------------------------------------
    page.goto("/integrations/", wait_until="networkidle")
    pace(page, 1800)

    # --- Open the Telegram connect flow -----------------------------------
    hover_and_click(page, page.locator("a[href*='integrate/telegram']").first)
    page.wait_for_url("**/integrate/telegram/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, 1200)

    # --- Enter the bot token and chat ID ----------------------------------
    type_text(
        page.locator("#bot_token"),
        "8123456789:AA-DemoBotTokenForScreencast",
        delay=25,
    )
    pace(page, 300)
    type_text(page.locator("#chat_id"), "-1001234567890")
    pace(page, 500)

    # Submit — getMe/getChat are mocked, so validation succeeds
    submit = page.get_by_role("button", name="Connect Telegram")
    submit.scroll_into_view_if_needed()
    hover_and_click(page, submit)
    page.wait_for_url("**/integrations/", timeout=15_000)
    page.wait_for_load_state("networkidle")
    pace(page, 2000)

    # --- The payoff: the alert landing in Telegram ------------------------
    play_telegram_finale(page)

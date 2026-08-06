"""Fixtures for capturing marketing screenshots and screencasts.

Boots the app against the test database via pytest-django's
``live_server``, seeds a fully Office Space-themed demo workspace
(Initech), logs in as Peter Gibbons through a forged session cookie
(the app itself only offers passkey/Slack OAuth), and hands scenarios
authenticated Playwright pages at marketing-friendly viewports.

Screencast scenarios instead use ``recording_page`` — an
unauthenticated Full HD page whose session is recorded to webm, with a
CDP virtual authenticator so the real passkey signup ceremony can run
on camera, and a mocked Slack API so the OAuth dance completes without
leaving the machine.

Run via ``bin/record_screenshots.sh`` — the suite is intentionally
outside pytest's ``testpaths`` so normal test runs never collect it.
"""

import os
import time
from pathlib import Path
from typing import Any, Generator
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlparse

import pytest
from core.models import (
    Integration,
    UserProfile,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Route,
    sync_playwright,
)

# Playwright's sync API keeps an asyncio loop alive in the main thread,
# which trips Django's async-context guard on ORM calls. This is capture
# tooling, not the app, so the guard adds nothing here.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

# Every capture lands on a standard broadcast resolution, so assets drop
# into marketing pages, app-store listings, and video timelines without
# rescaling. Captures are viewport-only for exactly this reason: a
# full-page capture grows to whatever height the page happens to be,
# which yields off-spec sizes like 3840x3194.
#
# Full HD desktop frame at 2x -> 3840x2160 (4K UHD).
DESKTOP_VIEWPORT = {"width": 1920, "height": 1080}
# 360x640 at 3x -> 1080x1920 (Full HD in portrait). Both the CSS width
# and the 3x density match a real flagship phone.
MOBILE_VIEWPORT = {"width": 360, "height": 640}
# Slack App Directory listing requirement: screenshots must be exactly
# 1600x1000 pixels — captured at 1:1 scale, viewport only (no
# full-page scrolling).
SLACK_LISTING_VIEWPORT = {"width": 1600, "height": 1000}
OUTPUT_DIR = Path(__file__).parent / "output"

# Initech's customers, straight from the movie
OFFICE_SPACE_EVENTS: list[dict[str, Any]] = [
    {
        "type": "payment",
        "event_type": "payment_success",
        "provider": "stripe",
        "status": "success",
        "amount": 4999.00,
        "currency": "USD",
        "headline": "Payment received from Initrode",
        "company_name": "Initrode",
        "company_domain": "initrode.com",
        "customer_email": "billing@initrode.com",
        "customer_name": "Dom Portwood",
        "plan_name": "TPS Premium",
        "card_last4": "4242",
        "payment_method": "card",
        "insight_text": "3rd successful payment in a row",
        "insight_icon": "chart",
    },
    {
        "type": "payment",
        "event_type": "payment_failure",
        "provider": "stripe",
        "status": "failed",
        "amount": 150.00,
        "currency": "USD",
        "headline": "Payment failed for Chotchkie's",
        "company_name": "Chotchkie's",
        "company_domain": "chotchkies.com",
        "customer_email": "joanna@chotchkies.com",
        "customer_name": "Joanna",
        "plan_name": "Flair Basic",
        "card_last4": "0341",
        "payment_method": "card",
        "severity": "warning",
        "insight_text": "2nd failure this week — sounds like a case of the Mondays",
        "insight_icon": "warning",
    },
    {
        "type": "order",
        "event_type": "order_created",
        "provider": "shopify",
        "status": "success",
        "amount": 1280.50,
        "currency": "USD",
        "headline": "New order from Flingers",
        "company_name": "Flingers",
        "company_domain": "flingers.com",
        "customer_email": "orders@flingers.com",
        "customer_name": "Brian",
        "order_number": "#1042",
    },
    {
        "type": "payment",
        "event_type": "subscription_cancelled",
        "provider": "chargify",
        "status": "cancelled",
        "amount": 89.00,
        "currency": "USD",
        "headline": "Subscription cancelled by Penetrode",
        "company_name": "Penetrode",
        "company_domain": "penetrode.com",
        "customer_email": "accounts@penetrode.com",
        "customer_name": "Bob Slydell",
        "plan_name": "Two Bobs",
        "severity": "critical",
        "insight_text": (
            "Customer for 14 months, LTV $686 — what would you say they did here?"
        ),
        "insight_icon": "warning",
    },
    {
        "type": "payment",
        "event_type": "trial_started",
        "provider": "stripe",
        "status": "pending",
        "amount": 0,
        "currency": "USD",
        "headline": "Trial started for Swingline",
        "company_name": "Swingline",
        "company_domain": "swingline.com",
        "customer_email": "milton@swingline.com",
        "customer_name": "Milton Waddams",
        "plan_name": "Stapler Tier",
    },
]


@pytest.fixture(autouse=True)
def screenshot_settings(settings) -> None:
    """Marketing-friendly settings for every capture.

    ``test_settings`` uses DummyCache, which would leave the dashboard's
    Recent Activity empty. LocMemCache is shared across threads, so the
    live-server thread sees what the fixture seeds.

    BASE_URL feeds the webhook URLs shown on the integration setup pages
    (Stripe/Shopify/Chargify); the default would render
    ``http://localhost:8000/...`` on camera, so use the production
    domain instead.
    """
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    settings.BASE_URL = "https://app.notipus.com"


def _seed_activity(workspace: Workspace) -> None:
    """Store three days of themed webhook activity in the cache."""
    now = time.time()
    ws_id = str(workspace.uuid)
    for day_offset in range(3):
        date = timezone.now() - timezone.timedelta(days=day_offset)
        activity_key = f"webhook_activity:{ws_id}:{date.strftime('%Y-%m-%d')}"
        keys = []
        for i, event in enumerate(OFFICE_SPACE_EVENTS):
            # Thin out older days so the feed looks organic
            if (i + day_offset) % 2 == 0 and day_offset > 0:
                continue
            record = dict(event)
            record["timestamp"] = now - day_offset * 86400 - i * 3600
            record["external_id"] = f"evt_os_{day_offset}_{i}"
            record["customer_id"] = f"cus_os_{i:04d}"
            key = f"webhook_record:os:{ws_id}:{day_offset}:{i}"
            cache.set(key, record, 7 * 86400)
            keys.append(key)
        cache.set(activity_key, keys, 7 * 86400)


@pytest.fixture
def office_space_data(db) -> dict[str, Any]:
    """Seed the Initech workspace with members, integrations, and activity."""
    peter = User.objects.create_user(
        username="peter",
        email="peter.gibbons@initech.com",
        first_name="Peter",
        last_name="Gibbons",
    )
    samir = User.objects.create_user(
        username="samir",
        email="samir@initech.com",
        first_name="Samir",
        last_name="Nagheenanajar",
    )

    workspace = Workspace.objects.create(
        name="Initech",
        subscription_plan="pro",
        subscription_status="trial",
    )
    WorkspaceMember.objects.create(user=peter, workspace=workspace, role="owner")
    WorkspaceMember.objects.create(user=samir, workspace=workspace, role="user")
    UserProfile.objects.create(user=peter, workspace=workspace)

    for integration_type in ("slack_notifications", "stripe_customer"):
        Integration.objects.create(
            workspace=workspace,
            integration_type=integration_type,
            is_active=True,
            oauth_credentials={"team_name": "initech"},
        )

    WorkspaceInvitation.objects.create(
        workspace=workspace,
        email="michael.bolton@initech.com",
        role="admin",
        invited_by=peter,
    )

    _seed_activity(workspace)

    return {"owner": peter, "member": samir, "workspace": workspace}


@pytest.fixture(scope="session")
def playwright() -> Generator[Playwright, Any, None]:
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser(playwright: Playwright) -> Generator[Browser, Any, None]:
    browser_instance = playwright.chromium.launch(args=["--no-sandbox"])
    yield browser_instance
    browser_instance.close()


@pytest.fixture
def session_cookie(office_space_data: dict[str, Any], live_server) -> dict[str, str]:
    """Authenticated session cookie for the workspace owner."""
    client = Client()
    client.force_login(office_space_data["owner"])
    return {
        "name": "sessionid",
        "value": client.cookies["sessionid"].value,
        "domain": urlparse(live_server.url).hostname,
        "path": "/",
    }


def _new_context(
    browser: Browser, live_server, session_cookie: dict[str, str], **kwargs: Any
) -> BrowserContext:
    context = browser.new_context(base_url=live_server.url, **kwargs)
    context.add_cookies([session_cookie])
    return context


@pytest.fixture
def page(
    browser: Browser, live_server, session_cookie: dict[str, str]
) -> Generator[Page, Any, None]:
    """Authenticated desktop page (Full HD frame at 2x pixels)."""
    context = _new_context(
        browser,
        live_server,
        session_cookie,
        viewport=DESKTOP_VIEWPORT,
        device_scale_factor=2,
    )
    yield context.new_page()
    context.close()


@pytest.fixture
def mobile_page(
    browser: Browser, live_server, session_cookie: dict[str, str]
) -> Generator[Page, Any, None]:
    """Authenticated mobile page."""
    context = _new_context(
        browser,
        live_server,
        session_cookie,
        viewport=MOBILE_VIEWPORT,
        device_scale_factor=3,  # 360x640 at 3x -> 1080x1920, as on a real phone
        is_mobile=True,
        has_touch=True,
    )
    yield context.new_page()
    context.close()


@pytest.fixture
def slack_listing_page(
    browser: Browser, live_server, session_cookie: dict[str, str]
) -> Generator[Page, Any, None]:
    """Authenticated page sized for Slack App Directory listings.

    The directory rejects anything that isn't exactly 1600x1000, so
    captures from this page must use ``full_page=False`` (the default
    ``shoot`` full-page mode would grow taller than 1000px).
    """
    context = _new_context(
        browser,
        live_server,
        session_cookie,
        viewport=SLACK_LISTING_VIEWPORT,
        device_scale_factor=1,
    )
    yield context.new_page()
    context.close()


# ---------------------------------------------------------------------------
# Screencast recording
# ---------------------------------------------------------------------------


def pace(page: Page, ms: int = 700) -> None:
    """Pause for a natural beat between actions so viewers can follow."""
    page.wait_for_timeout(ms)


def hover_and_click(page: Page, locator: Locator, pause_ms: int = 300) -> None:
    """Move the cursor to the element, pause, then click.

    Without the pause, Playwright clicks land in a single video frame
    and the action is impossible to follow.
    """
    box = locator.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(pause_ms)
    locator.click()


def type_text(locator: Locator, text: str, delay: int = 60) -> None:
    """Type character-by-character for a human-like feel."""
    locator.press_sequentially(text, delay=delay)


def intercept_slack_oauth(page: Page) -> None:
    """Short-circuit the Slack OAuth hop back into the app.

    Route handlers don't fire on redirect hops, so intercepting
    slack.com directly never triggers. Instead the first hop
    (``/api/connect/slack/``) is replayed through the context's request
    API — sharing the cookie jar, so the CSRF state the server mints
    still lands in the session — and the browser is sent straight to the
    app's callback with the ``state`` and ``redirect_uri`` the server
    put in its authorize URL. The browser never leaves the machine.
    """

    def handle(route: Route) -> None:
        url = route.request.url
        for _ in range(5):
            response = page.request.get(url, max_redirects=0)
            location = response.headers.get("location", "")
            if not location:
                break
            if location.startswith("https://slack.com/"):
                query = parse_qs(urlparse(location).query)
                redirect_uri = query["redirect_uri"][0]
                state = query["state"][0]
                route.fulfill(
                    status=302,
                    headers={
                        "Location": (
                            f"{redirect_uri}?code=demo-oauth-code&state={state}"
                        )
                    },
                )
                return
            url = urljoin(url, location)
        # Never fall through to the real network: if the authorize hop
        # wasn't found, abort so the scenario fails fast and loudly
        # instead of the browser leaving the machine (or hanging CI).
        route.abort("failed")

    page.route("**/integrate/slack/", handle)


class _FakeSlackResponse:
    """Minimal requests.Response stand-in for the mocked Slack API."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        """Always a 200 — nothing to raise."""


def _fake_slack_post(url: str, **kwargs: Any) -> _FakeSlackResponse:
    # oauth.v2.access token exchange
    return _FakeSlackResponse(
        {
            "ok": True,
            "access_token": "xoxb-demo-token",
            "team": {"id": "T0INITECH", "name": "Initech"},
            "incoming_webhook": {
                "channel": "#general",
                "url": "https://hooks.slack.com/services/demo",
            },
        }
    )


def _fake_slack_get(url: str, **kwargs: Any) -> _FakeSlackResponse:
    # conversations.list channel listing
    return _FakeSlackResponse(
        {
            "ok": True,
            "channels": [
                {"id": "C01", "name": "general"},
                {"id": "C02", "name": "billing-alerts"},
                {"id": "C03", "name": "tps-reports"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
    )


@pytest.fixture
def mock_slack_api() -> Generator[None, Any, None]:
    """Fake the server-side Slack API calls.

    ``live_server`` runs in this process, so patching the ``requests``
    module used by the Slack views affects the serving thread too.
    """
    with (
        patch("core.views.integrations.slack.requests.post", _fake_slack_post),
        patch("core.views.integrations.slack.requests.get", _fake_slack_get),
    ):
        yield


class _FakeTelegramResponse:
    """Minimal requests.Response stand-in for the mocked Telegram Bot API."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        """Always a 200 — nothing to raise."""


def _fake_telegram_get(url: str, **kwargs: Any) -> _FakeTelegramResponse:
    # getMe validates the bot token; getChat validates the chat id.
    if "/getMe" in url:
        return _FakeTelegramResponse(
            {
                "ok": True,
                "result": {
                    "id": 424242,
                    "is_bot": True,
                    "first_name": "Initech Alerts",
                    "username": "InitechAlertsBot",
                },
            }
        )
    return _FakeTelegramResponse(
        {
            "ok": True,
            "result": {
                "id": -1001234567890,
                "title": "Initech · billing",
                "type": "supergroup",
            },
        }
    )


def _fake_telegram_post(url: str, **kwargs: Any) -> _FakeTelegramResponse:
    # sendMessage (the "send test message" button)
    return _FakeTelegramResponse({"ok": True, "result": {"message_id": 1}})


@pytest.fixture
def mock_telegram_api() -> Generator[None, Any, None]:
    """Fake the server-side Telegram Bot API (getMe/getChat/sendMessage).

    ``live_server`` runs in this process, so patching the ``requests``
    module used by the Telegram views affects the serving thread too.
    """
    with (
        patch("core.views.integrations.telegram.requests.get", _fake_telegram_get),
        patch("core.views.integrations.telegram.requests.post", _fake_telegram_post),
    ):
        yield


@pytest.fixture
def recording_page(
    request: pytest.FixtureRequest, browser: Browser, live_server, settings
) -> Generator[Page, Any, None]:
    """Unauthenticated Full HD page recorded to ``output/<test>.webm``.

    Configures WebAuthn and the Slack redirect URI for the live server's
    ephemeral port, and attaches a CDP virtual authenticator so
    ``navigator.credentials.create()`` succeeds without hardware.
    """
    # RP ID must be the effective domain of the origin, whatever host
    # the live server actually bound (localhost vs 127.0.0.1).
    settings.WEBAUTHN_RP_ID = urlparse(live_server.url).hostname
    settings.WEBAUTHN_ORIGIN = live_server.url
    settings.SLACK_CONNECT_REDIRECT_URI = (
        f"{live_server.url}/api/connect/slack/callback/"
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    context = browser.new_context(
        base_url=live_server.url,
        viewport=DESKTOP_VIEWPORT,
        # 2x so mid-flow stills harvested via snap() match the retina
        # resolution of the standalone screenshot scenarios; the video is
        # still written at record_video_size (1920x1080), just rendered
        # from a crisper backing store.
        device_scale_factor=2,
        record_video_dir=str(OUTPUT_DIR),
        record_video_size=DESKTOP_VIEWPORT,
    )
    page = context.new_page()

    cdp = context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )

    yield page

    video = page.video
    context.close()  # finalizes the recording
    if video:
        Path(video.path()).rename(OUTPUT_DIR / f"{request.node.name}.webm")


@pytest.fixture
def recording_page_authed(
    request: pytest.FixtureRequest,
    browser: Browser,
    live_server,
    session_cookie: dict[str, str],
) -> Generator[Page, Any, None]:
    """Authenticated Full HD page recorded to ``output/<test>.webm``.

    Starts logged in as the Initech owner, with Slack and Stripe already
    connected (via ``office_space_data``), for screencasts that begin
    inside the app rather than at signup.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    context = browser.new_context(
        base_url=live_server.url,
        viewport=DESKTOP_VIEWPORT,
        record_video_dir=str(OUTPUT_DIR),
        record_video_size=DESKTOP_VIEWPORT,
    )
    context.add_cookies([session_cookie])
    page = context.new_page()

    yield page

    video = page.video
    context.close()  # finalizes the recording
    if video:
        Path(video.path()).rename(OUTPUT_DIR / f"{request.node.name}.webm")


# Slack-style finale for the onboarding screencast: a #billing-alerts
# channel where the Notipus notification slides in after a beat. Uses
# the app's own logo (relative URL, served by the live server since the
# page keeps the app origin after set_content).
SLACK_FINALE_HTML = """\
<!doctype html><html><head><meta charset="utf-8"><style>
  * { margin: 0; box-sizing: border-box; font-family: -apple-system,
      BlinkMacSystemFont, "Segoe UI", Lato, sans-serif; }
  body { display: flex; height: 100vh; overflow: hidden; }
  .sidebar { width: 260px; background: #3F0E40; color: #cfc3cf;
             padding: 16px 0; flex-shrink: 0; }
  .team { color: #fff; font-weight: 900; font-size: 18px;
          padding: 0 16px 14px; border-bottom: 1px solid #522653;
          margin-bottom: 12px; }
  .section { padding: 4px 16px; font-size: 15px; }
  .channel { padding: 4px 16px; font-size: 15px; }
  .channel.active { background: #1164A3; color: #fff; }
  .main { flex: 1; display: flex; flex-direction: column; }
  .header { padding: 12px 20px; border-bottom: 1px solid #e2e2e2;
            font-weight: 900; font-size: 18px; color: #1d1c1d; }
  .header small { display: block; font-weight: 400; font-size: 13px;
                  color: #616061; margin-top: 2px; }
  .messages { flex: 1; padding: 20px; display: flex;
              flex-direction: column; justify-content: flex-end; }
  .msg { display: flex; gap: 10px; margin-top: 18px; }
  .avatar { width: 38px; height: 38px; border-radius: 5px;
            background: #fff; border: 1px solid #e2e2e2;
            display: flex; align-items: center; justify-content: center; }
  .avatar img { width: 30px; height: 30px; }
  .author { font-weight: 900; color: #1d1c1d; font-size: 15px; }
  .app-badge { background: #e8e8e8; color: #616061; font-size: 10px;
               font-weight: 700; padding: 1px 4px; border-radius: 3px;
               vertical-align: middle; }
  .ts { color: #616061; font-size: 12px; margin-left: 6px; }
  .attachment { border-left: 4px solid #2eb67d; background: #f8f8f8;
                border-radius: 0 6px 6px 0; padding: 10px 14px;
                margin-top: 6px; max-width: 640px; }
  .attachment.gray { border-left-color: #868686; }
  .attachment.red { border-left-color: #e01e5a; }
  .attachment.blue { border-left-color: #1264a3; }
  .headline { font-weight: 700; color: #1d1c1d; font-size: 15px; }
  .meta { color: #616061; font-size: 13px; margin-top: 4px; }
  .insight { color: #2eb67d; font-size: 13px; font-weight: 600;
             margin-top: 6px; }
  .insight.warn { color: #e01e5a; }
  .insight.info { color: #1264a3; }
  #incoming { opacity: 0; transform: translateY(14px); }
  #incoming.shown { opacity: 1; transform: none;
                    transition: all 420ms ease-out; }
</style></head><body>
  <div class="sidebar">
    <div class="team">Initech</div>
    <div class="section">Channels</div>
    <div class="channel"># general</div>
    <div class="channel"># tps-reports</div>
    <div class="channel active"># billing-alerts</div>
  </div>
  <div class="main">
    <div class="header"># billing-alerts
      <small>Payment events from Notipus</small></div>
    <div class="messages">
      <div class="msg">
        <div class="avatar"><img src="/static/img/notipus-logo.png"></div>
        <div>
          <span class="author">Notipus</span>
          <span class="app-badge">APP</span><span class="ts">8:41 AM</span>
          <div class="attachment red">
            <div class="headline">&#9888;&#65039; Payment failed for
              Chotchkie's</div>
            <div class="meta">$149.00 &middot; Flair Plan &middot;
              manager@chotchkies.com</div>
            <div class="insight warn">Card declined &middot; retry scheduled
              for tomorrow</div>
          </div>
        </div>
      </div>
      <div class="msg">
        <div class="avatar"><img src="/static/img/notipus-logo.png"></div>
        <div>
          <span class="author">Notipus</span>
          <span class="app-badge">APP</span><span class="ts">8:58 AM</span>
          <div class="attachment blue">
            <div class="headline">&#11014;&#65039; Penetrode upgraded to TPS
              Premium</div>
            <div class="meta">$4,999.00/mo &middot; was TPS Standard &middot;
              ap@penetrode.com</div>
            <div class="insight info">Expansion revenue +$3,000/mo</div>
          </div>
        </div>
      </div>
      <div class="msg">
        <div class="avatar"><img src="/static/img/notipus-logo.png"></div>
        <div>
          <span class="author">Notipus</span>
          <span class="app-badge">APP</span><span class="ts">9:12 AM</span>
          <div class="attachment gray">
            <div class="headline">Trial started for Swingline</div>
            <div class="meta">Stapler Tier &middot; milton@swingline.com</div>
          </div>
        </div>
      </div>
      <div class="msg" id="incoming">
        <div class="avatar"><img src="/static/img/notipus-logo.png"></div>
        <div>
          <span class="author">Notipus</span>
          <span class="app-badge">APP</span><span class="ts">9:57 AM</span>
          <div class="attachment">
            <div class="headline">&#128176; Payment received from Initrode</div>
            <div class="meta">$4,999.00 &middot; TPS Premium &middot;
              billing@initrode.com</div>
            <div class="insight">&#128200; 3rd successful payment in a row</div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <script>
    setTimeout(() => {
      document.getElementById('incoming').classList.add('shown');
    }, 1600);
  </script>
</body></html>
"""


def show_slack_finale(page: Page) -> None:
    """Swap the page for the Slack-style view and wait for the alert."""
    page.set_content(SLACK_FINALE_HTML, wait_until="load")
    page.wait_for_selector("#incoming.shown", timeout=10_000)


def play_slack_finale(page: Page, hold_ms: int = 4500) -> None:
    """Show the Slack-style view and hold it on screen for recordings."""
    show_slack_finale(page)
    pace(page, hold_ms)


# Telegram-style finale for the telegram screencast: a chat where the
# Notipus notification bubble slides in after a beat. Uses the app's own
# logo (relative URL, served by the live server since the page keeps the
# app origin after set_content).
TELEGRAM_FINALE_HTML = """\
<!doctype html><html><head><meta charset="utf-8"><style>
  * { margin: 0; box-sizing: border-box; font-family: -apple-system,
      BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { height: 100vh; overflow: hidden; background: #dfe6ea; }
  .chat { display: flex; flex-direction: column; height: 100vh; }
  .header { background: #527da3; color: #fff; padding: 12px 20px;
            display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  .chat-avatar { width: 42px; height: 42px; border-radius: 50%;
                 background: #fff; display: flex; align-items: center;
                 justify-content: center; }
  .chat-avatar img { width: 30px; height: 30px; }
  .chat-title { font-weight: 600; font-size: 17px; }
  .chat-sub { font-size: 13px; color: #cfe0ee; margin-top: 1px; }
  .messages { flex: 1; padding: 24px 18%; display: flex;
              flex-direction: column; justify-content: flex-end; gap: 14px;
              background:
                radial-gradient(circle at 20% 30%, #c9d6de 0 2px, transparent 2px),
                radial-gradient(circle at 70% 60%, #c9d6de 0 2px, transparent 2px),
                #dfe6ea;
              background-size: 60px 60px; }
  .bubble { align-self: flex-start; background: #fff; border-radius: 12px;
            padding: 10px 14px; max-width: 560px;
            box-shadow: 0 1px 1px rgba(0,0,0,.12); }
  .headline { font-weight: 700; color: #1d1d1f; font-size: 15px; }
  .meta { color: #556; font-size: 13px; margin-top: 4px; }
  .insight { color: #1a8f4c; font-size: 13px; font-weight: 600;
             margin-top: 6px; }
  .stamp { display: block; text-align: right; color: #9aa7b0;
           font-size: 11px; margin-top: 6px; }
  #incoming { opacity: 0; transform: translateY(14px); }
  #incoming.shown { opacity: 1; transform: none;
                    transition: all 420ms ease-out; }
</style></head><body>
  <div class="chat">
    <div class="header">
      <div class="chat-avatar"><img src="/static/img/notipus-logo.png"></div>
      <div>
        <div class="chat-title">Initech · billing</div>
        <div class="chat-sub">Notipus bot &middot; payment alerts</div>
      </div>
    </div>
    <div class="messages">
      <div class="bubble">
        <div class="headline">Trial started for Swingline</div>
        <div class="meta">Stapler Tier &middot; milton@swingline.com</div>
        <span class="stamp">9:12 AM</span>
      </div>
      <div class="bubble" id="incoming">
        <div class="headline">&#128176; Payment received from Initrode</div>
        <div class="meta">$4,999.00 &middot; TPS Premium &middot;
          billing@initrode.com</div>
        <div class="insight">&#128200; 3rd successful payment in a row</div>
        <span class="stamp">9:57 AM</span>
      </div>
    </div>
  </div>
  <script>
    setTimeout(() => {
      document.getElementById('incoming').classList.add('shown');
    }, 1600);
  </script>
</body></html>
"""


def show_telegram_finale(page: Page) -> None:
    """Swap the page for the Telegram-style view and wait for the alert."""
    page.set_content(TELEGRAM_FINALE_HTML, wait_until="load")
    page.wait_for_selector("#incoming.shown", timeout=10_000)


def play_telegram_finale(page: Page, hold_ms: int = 4500) -> None:
    """Show the Telegram-style view and hold it on screen for recordings."""
    show_telegram_finale(page)
    pace(page, hold_ms)


def shoot(target: Page, name: str, path: str, full_page: bool = False) -> None:
    """Navigate to ``path`` and save ``output/<name>.png``.

    Viewport-only by default, which pins the output to the fixture's
    frame — 3840x2160 from ``page``, 1080x1920 from ``mobile_page``.
    Passing ``full_page=True`` trades that guarantee for the whole
    scroll height, so only reach for it when an asset genuinely needs
    below-the-fold content and its odd dimensions are acceptable.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    target.goto(path, wait_until="networkidle")
    # Let entrance animations (fade/slide) settle before the capture
    target.wait_for_timeout(400)
    target.screenshot(path=str(OUTPUT_DIR / f"{name}.png"), full_page=full_page)
    print(f"captured {name}.png")


def snap(page: Page, name: str, full_page: bool = False) -> None:
    """Capture ``output/<name>.png`` at the current point in a flow.

    Viewport-only by default so a full-page scroll doesn't disturb an
    in-progress screencast recording. Use inside a recorded scenario to
    harvest reusable stills alongside the video, without re-driving the UI.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(OUTPUT_DIR / f"{name}.png"), full_page=full_page)
    print(f"captured {name}.png")

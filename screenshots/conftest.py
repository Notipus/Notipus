"""Fixtures for capturing marketing screenshots.

Boots the app against the test database via pytest-django's
``live_server``, seeds a fully Office Space-themed demo workspace
(Initech), logs in as Peter Gibbons through a forged session cookie
(the app itself only offers passkey/Slack OAuth), and hands scenarios
authenticated Playwright pages at marketing-friendly viewports.

Run via ``bin/record_screenshots.sh`` — the suite is intentionally
outside pytest's ``testpaths`` so normal test runs never collect it.
"""

import os
import time
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlparse

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
    Page,
    Playwright,
    sync_playwright,
)

# Playwright's sync API keeps an asyncio loop alive in the main thread,
# which trips Django's async-context guard on ORM calls. This is capture
# tooling, not the app, so the guard adds nothing here.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
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
    """Cache the seeded activity feed where the live server can read it.

    ``test_settings`` uses DummyCache, which would leave the dashboard's
    Recent Activity empty. LocMemCache is shared across threads, so the
    live-server thread sees what the fixture seeds.
    """
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }


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
    context = browser.new_context(
        base_url=live_server.url,
        device_scale_factor=2,  # crisp images for marketing use
        **kwargs,
    )
    context.add_cookies([session_cookie])
    return context


@pytest.fixture
def page(
    browser: Browser, live_server, session_cookie: dict[str, str]
) -> Generator[Page, Any, None]:
    """Authenticated desktop page."""
    context = _new_context(
        browser, live_server, session_cookie, viewport=DESKTOP_VIEWPORT
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
        is_mobile=True,
        has_touch=True,
    )
    yield context.new_page()
    context.close()


def shoot(target: Page, name: str, path: str, full_page: bool = True) -> None:
    """Navigate to ``path`` and save ``output/<name>.png``."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    target.goto(path, wait_until="networkidle")
    # Let entrance animations (fade/slide) settle before the capture
    target.wait_for_timeout(400)
    target.screenshot(path=str(OUTPUT_DIR / f"{name}.png"), full_page=full_page)
    print(f"captured {name}.png")

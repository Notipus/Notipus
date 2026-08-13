# Marketing Screenshots & Screencasts

Programmatic, reproducible screenshots and screencasts of the app for
the marketing site, docs, and app-store listings. Every capture boots
the real app against a seeded demo workspace, so assets stay current
with the UI instead of rotting in a design folder.

`onboarding.py` records a Full HD webm of the entire zero-to-configured
journey: passkey signup (a real WebAuthn ceremony against a CDP virtual
authenticator), plan selection, workspace creation, connecting Slack
(OAuth short-circuited at the network edge, server API mocked
in-process), choosing a channel, connecting Stripe, and a finale where
the notification lands in a Slack-style window.

The demo data is Office Space themed: the **Initech** workspace is
owned by Peter Gibbons, with Samir Nagheenanajar as a member and a
pending invite for Michael Bolton. The activity feed shows Initech's
customers — Initrode, Chotchkie's, Flingers, Penetrode, and Milton's
Swingline trial.

## Running locally

```bash
uv sync --all-groups                 # one-time: installs the playwright dependency
bin/record_screenshots.sh            # capture everything
bin/record_screenshots.sh dashboard.py   # capture one scenario
```

Output lands in `screenshots/output/` (gitignored). The script builds
the frontend, installs the Playwright Chromium if needed, and runs
each scenario through pytest with a live server and SQLite — no
external services required.

## Running in CI

The **Marketing Screenshots** workflow is manual-only: trigger it from
the Actions tab (workflow_dispatch), optionally naming a single
scenario file. Captures are attached to the run as a `screenshots`
build artifact.

## Adding a scenario

Create `screenshots/<name>.py` with a single function named `<name>`
(the runner maps filename → pytest function). Use the `page` /
`mobile_page` fixtures for an authenticated session and `shoot()` to
navigate and capture:

```python
import pytest
from playwright.sync_api import Page

from conftest import shoot


@pytest.mark.django_db(transaction=True)
def my_page(page: Page) -> None:
    shoot(page, "my-page", "/my-page/")
```

## Resolutions

Every capture lands on a standard broadcast resolution, so assets drop
into marketing pages, listings, and video timelines without rescaling:

| Fixture | Frame | Output |
| --- | --- | --- |
| `page`, `recording_page` | 1920x1080 at 2x | **3840x2160** (4K UHD) |
| `mobile_page` | 360x640 at 3x | **1080x1920** (Full HD portrait) |
| `slack_listing_page` | 1600x1000 at 1x | **1600x1000** |

`shoot()` and `snap()` are therefore viewport-only. A full-page capture
grows to whatever height the page happens to be, which yields off-spec
sizes like 3840x3194 — pass `full_page=True` only when an asset really
needs below-the-fold content, and expect a non-standard size.

`slack_listing.py` sits outside the 1080p/4K set on purpose: the Slack
App Directory requires screenshots that are exactly 1600x1000.

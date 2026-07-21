# Marketing Screenshots

Programmatic, reproducible screenshots of the app for the marketing
site, docs, and app-store listings. Every capture boots the real app
against a seeded demo workspace, so screenshots stay current with the
UI instead of rotting in a design folder.

The demo data is Office Space themed: the **Initech** workspace is
owned by Peter Gibbons, with Samir Nagheenanajar as a member and a
pending invite for Michael Bolton. The activity feed shows Initech's
customers — Initrode, Chotchkie's, Flingers, Penetrode, and Milton's
Swingline trial.

## Running locally

```bash
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
scenario file. Captures are uploaded as a build artifact, and synced to
Cloudflare R2 when the `R2_*` secrets are configured.

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

Keep captures at the fixture defaults (1920x1080 desktop frame and
390x844 mobile frame, both at 2x — output is 3840px / 780px wide, so
Full HD is the floor) so the set stays visually consistent.

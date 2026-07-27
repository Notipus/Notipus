"""Capture the team members page.

Shows Peter Gibbons (owner), Samir Nagheenanajar, and a pending
admin invitation for Michael Bolton.
"""

import pytest
from conftest import shoot
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def members(page: Page) -> None:
    shoot(page, "members", "/workspace/members/")

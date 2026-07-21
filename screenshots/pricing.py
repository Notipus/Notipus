"""Capture the plan selection (pricing) page."""

import pytest
from conftest import shoot
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def pricing(page: Page) -> None:
    shoot(page, "pricing", "/select-plan/")

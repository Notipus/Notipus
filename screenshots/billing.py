"""Capture the billing dashboard during an active Pro trial."""

import pytest
from conftest import shoot
from playwright.sync_api import Page


@pytest.mark.django_db(transaction=True)
def billing(page: Page) -> None:
    shoot(page, "billing", "/billing/")

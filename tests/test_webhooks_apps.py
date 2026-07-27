"""Tests for the webhooks app startup guard.

The guard decides which processes run orphan recovery and the periodic
delivery-retry sweep. Getting it wrong in either direction is costly:
skipping servers silently disables retries in production, while running
in tests/management commands starts pointless background threads.
"""

from unittest.mock import patch

import pytest
from webhooks.apps import _is_serving_process


class TestIsServingProcess:
    """Test serving-process detection across launch contexts."""

    @pytest.mark.parametrize(
        "argv",
        [
            # Production ASGI server (Dockerfile CMD): argv[1] is a flag,
            # which the old argument-based guard wrongly treated as a
            # management command
            [
                "/venv/bin/uvicorn",
                "--host",
                "0.0.0.0",
                "django_notipus.asgi:application",
            ],
            ["/usr/local/bin/gunicorn", "django_notipus.wsgi:application"],
            ["/venv/bin/daphne", "django_notipus.asgi:application"],
        ],
    )
    def test_asgi_wsgi_servers_are_serving(self, argv: list[str]) -> None:
        """Test that real app servers run recovery regardless of argv[1]."""
        with patch.object(__import__("sys"), "argv", argv):
            assert _is_serving_process() is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["/venv/bin/pytest", "tests/test_webhooks.py", "-q"],
            ["/venv/bin/pytest"],
            ["manage.py", "migrate"],
            ["manage.py", "makemigrations", "core"],
            ["app/manage.py", "shell"],
            ["/venv/bin/django-admin", "check"],
        ],
    )
    def test_tests_and_management_commands_are_not_serving(
        self, argv: list[str]
    ) -> None:
        """Test that non-serving contexts skip recovery and the sweeper."""
        with patch.object(__import__("sys"), "argv", argv):
            assert _is_serving_process() is False

    def test_runserver_inner_autoreload_process_is_serving(self) -> None:
        """Test that runserver's serving child (RUN_MAIN=true) qualifies."""
        with patch.object(__import__("sys"), "argv", ["manage.py", "runserver"]):
            with patch.dict("os.environ", {"RUN_MAIN": "true"}):
                assert _is_serving_process() is True

    def test_runserver_outer_watcher_process_is_not_serving(self) -> None:
        """Test that runserver's autoreload watcher does not double-run."""
        with patch.object(__import__("sys"), "argv", ["manage.py", "runserver"]):
            with patch.dict("os.environ", {}, clear=True):
                assert _is_serving_process() is False

    def test_runserver_noreload_single_process_is_serving(self) -> None:
        """Test that runserver --noreload (no RUN_MAIN) still qualifies."""
        argv = ["manage.py", "runserver", "--noreload"]
        with patch.object(__import__("sys"), "argv", argv):
            with patch.dict("os.environ", {}, clear=True):
                assert _is_serving_process() is True

"""Tests for the production logging configuration.

The root logger has the console handler attached, so any named logger
that both attaches the console handler and propagates to root emits
every record twice (seen in prod as duplicated, interleaved tracebacks
in the Fly logs).
"""

import logging
import logging.config

import pytest
from django_notipus.settings import LOGGING


def test_root_logger_has_console_handler() -> None:
    """Root keeps a console handler so unnamed loggers still emit."""
    assert "console" in LOGGING["root"]["handlers"]


def test_named_loggers_with_handlers_do_not_propagate() -> None:
    """Loggers with their own handler must not also propagate to root.

    Propagation plus a root handler means double emission: the record is
    handled once by the named logger's handler and again by root's.
    """
    for name, config in LOGGING["loggers"].items():
        if config.get("handlers"):
            assert config.get("propagate") is False, (
                f"Logger {name!r} attaches handlers and propagates to the "
                "root logger, so every record it emits is printed twice"
            )


def test_disallowed_host_logger_is_silenced() -> None:
    """DisallowedHost records must be dropped entirely.

    Scanners probing unallowed hostnames (e.g. *.fly.dev) would otherwise
    log a full traceback per hit; the 400 response is outcome enough.
    The null handler is load-bearing: an empty handler list does NOT
    silence the logger, it routes records to logging.lastResort (stderr).
    """
    config = LOGGING["loggers"]["django.security.DisallowedHost"]
    assert config["handlers"] == ["null"]
    assert config["propagate"] is False


def test_disallowed_host_records_do_not_reach_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A DisallowedHost record must produce no output at all.

    Regression test: with handlers=[] the record found no handler
    anywhere and fell through to logging.lastResort, which wrote the
    message and full traceback to stderr on every scanner hit (seen in
    prod July 23, 2026 despite the logger being 'silenced').

    Applies only the production entries for this logger (not the full
    LOGGING dict) so the test cannot clobber the test-run logging config.
    Root handlers are irrelevant to the lastResort fallback because the
    logger does not propagate, so this scoped config exercises the same
    code path as production.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {"null": dict(LOGGING["handlers"]["null"])},
            "loggers": {
                "django.security.DisallowedHost": dict(
                    LOGGING["loggers"]["django.security.DisallowedHost"]
                ),
            },
        }
    )
    logger = logging.getLogger("django.security.DisallowedHost")

    try:
        raise ValueError("scanner probe")
    except ValueError:
        logger.exception("Invalid HTTP_HOST header: 'notipus.fly.dev'.")

    out, err = capfd.readouterr()
    assert out == ""
    assert err == ""

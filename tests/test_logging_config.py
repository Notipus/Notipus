"""Tests for the production logging configuration.

The root logger has the console handler attached, so any named logger
that both attaches the console handler and propagates to root emits
every record twice (seen in prod as duplicated, interleaved tracebacks
in the Fly logs).
"""

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

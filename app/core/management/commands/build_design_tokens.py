"""Render the design tokens into the Tailwind theme stylesheet."""

from pathlib import Path
from typing import Any

from core.design_tokens import failing_rules, render_theme_css
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# src/ sits beside app/, next to package.json where the Tailwind build runs.
TOKENS_CSS_PATH = Path(settings.BASE_DIR).parent / "src" / "css" / "tokens.css"


class Command(BaseCommand):
    """Write ``src/css/tokens.css`` from ``core.design_tokens``."""

    help = "Generate src/css/tokens.css from the design token definitions."

    def add_arguments(self, parser: Any) -> None:
        """Register the command's flags.

        Args:
            parser: The argparse parser Django hands to the command.
        """
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit non-zero if the file on disk is stale instead of writing it.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Generate the stylesheet, or verify the checked-in copy is current.

        Args:
            *args: Unused positional arguments.
            **options: Parsed command-line options.

        Raises:
            CommandError: If a token pairing fails WCAG, or --check finds drift.
        """
        failures = failing_rules()
        if failures:
            detail = "\n".join(
                f"  {rule.foreground} on {rule.background} is "
                f"{rule.ratio:.2f}:1, needs {rule.minimum}:1 ({rule.where})"
                for rule in failures
            )
            raise CommandError(f"Design tokens fail WCAG contrast:\n{detail}")

        css = render_theme_css()
        existing = (
            TOKENS_CSS_PATH.read_text(encoding="utf-8")
            if TOKENS_CSS_PATH.exists()
            else None
        )

        if options["check"]:
            if existing != css:
                raise CommandError(
                    f"{TOKENS_CSS_PATH} is stale. Run: "
                    "uv run python app/manage.py build_design_tokens"
                )
            self.stdout.write(self.style.SUCCESS("Design tokens are up to date."))
            return

        if existing == css:
            self.stdout.write("Design tokens unchanged.")
            return

        TOKENS_CSS_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKENS_CSS_PATH.write_text(css, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {TOKENS_CSS_PATH}"))

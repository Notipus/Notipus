"""Tests for the design token system.

These are the guard rails that keep the UI consistent between reviews:

* every declared colour pairing clears the WCAG threshold Lighthouse audits;
* the checked-in ``src/css/tokens.css`` matches the Python definitions;
* templates and JS use semantic tokens, not raw Tailwind palette colours;
* every Tabler icon name referenced anywhere actually exists in the webfont.
"""

import re
from pathlib import Path

import pytest
from core.design_tokens import (
    CONTRAST_RULES,
    TOKEN_GROUPS,
    ContrastKind,
    color_value,
    contrast_ratio,
    parse_hex,
    relative_luminance,
    render_theme_css,
)
from django.conf import settings

REPO_ROOT = Path(settings.BASE_DIR).parent
TOKENS_CSS = REPO_ROOT / "src" / "css" / "tokens.css"
TEMPLATE_ROOT = Path(settings.BASE_DIR) / "core" / "templates"
UI_JS = Path(settings.BASE_DIR) / "static" / "js" / "ui.js"

# Tailwind's stock palette. Using one of these in a template means a colour has
# escaped the token system, which is how the interface drifted before.
RAW_PALETTE = re.compile(
    r"\b(?:bg|text|border|ring|from|via|to|divide|placeholder|accent|outline|"
    r"decoration|shadow|fill|stroke)-"
    r"(?:slate|gray|grey|zinc|neutral|stone|red|orange|amber|yellow|lime|green|"
    r"emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|"
    r"primary|notipus)-\d{2,3}\b"
)


def _template_files() -> list[Path]:
    """Return every template that renders product UI.

    Returns:
        Paths to the Cotton components and page templates, excluding the email
        templates, which are inline-styled for mail clients rather than Tailwind.
    """
    files = [*TEMPLATE_ROOT.rglob("*.html"), *TEMPLATE_ROOT.rglob("*.html.j2")]
    return [
        path
        for path in files
        if "emails" not in path.parts and "admin" not in path.parts
    ]


class TestContrastMaths:
    """The WCAG helpers themselves, against values with known answers."""

    def test_parse_hex_accepts_both_forms(self) -> None:
        """A colour parses with or without the leading hash."""
        assert parse_hex("#f05800") == (240, 88, 0)
        assert parse_hex("f05800") == (240, 88, 0)

    def test_parse_hex_rejects_short_form(self) -> None:
        """Three-digit hex is rejected rather than silently misread."""
        with pytest.raises(ValueError):
            parse_hex("#fff")

    def test_luminance_endpoints(self) -> None:
        """Black and white sit at the ends of the luminance range."""
        assert relative_luminance("#000000") == pytest.approx(0.0)
        assert relative_luminance("#ffffff") == pytest.approx(1.0)

    def test_black_on_white_is_twenty_one_to_one(self) -> None:
        """The maximum possible ratio comes out at 21:1."""
        assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)

    def test_ratio_is_symmetric(self) -> None:
        """Swapping foreground and background does not change the ratio."""
        assert contrast_ratio("#bb3700", "#ffffff") == pytest.approx(
            contrast_ratio("#ffffff", "#bb3700")
        )


class TestPalette:
    """The token declarations themselves."""

    def test_every_pairing_clears_its_threshold(self) -> None:
        """No declared foreground/background pairing misses WCAG AA."""
        failures = [
            f"{rule.foreground} on {rule.background} is {rule.ratio:.2f}:1, "
            f"needs {rule.minimum}:1 ({rule.where})"
            for rule in CONTRAST_RULES
            if not rule.passes
        ]
        assert not failures, "Contrast failures:\n" + "\n".join(failures)

    def test_body_text_pairings_are_actually_checked(self) -> None:
        """Guard against the rule set quietly emptying out."""
        body_rules = [r for r in CONTRAST_RULES if r.kind is ContrastKind.BODY_TEXT]
        assert len(body_rules) >= 25

    def test_token_names_are_unique(self) -> None:
        """Two tokens never share a name within the same namespace."""
        seen: set[tuple[str, str]] = set()
        for group in TOKEN_GROUPS:
            for token in group.tokens:
                key = (group.namespace, token.name)
                assert key not in seen, (
                    f"Duplicate token: {group.namespace}-{token.name}"
                )
                seen.add(key)

    def test_every_colour_token_is_a_six_digit_hex(self) -> None:
        """Colour tokens stay in one notation so the maths can read them."""
        for group in TOKEN_GROUPS:
            if not group.is_color:
                continue
            for token in group.tokens:
                assert re.fullmatch(r"#[0-9a-f]{6}", token.value), (
                    f"{group.namespace}-{token.name} is {token.value!r}"
                )

    def test_every_token_documents_its_use(self) -> None:
        """A token nobody can explain is a token nobody should reach for."""
        for group in TOKEN_GROUPS:
            for token in group.tokens:
                assert token.usage.strip(), (
                    f"{group.namespace}-{token.name} has no usage note"
                )

    def test_graphic_only_tokens_really_cannot_carry_text(self) -> None:
        """The graphic_only marker matches reality, not just intent."""
        for group in TOKEN_GROUPS:
            if not group.is_color:
                continue
            for token in group.tokens:
                if not token.graphic_only:
                    continue
                on_white = contrast_ratio(token.value, color_value("surface"))
                assert on_white < 4.5, (
                    f"{token.name} clears 4.5:1 on white, so the graphic_only "
                    "marker is misleading — drop it or pick a different value."
                )


class TestGeneratedStylesheet:
    """The checked-in CSS has to match the Python definitions."""

    def test_tokens_css_is_in_sync(self) -> None:
        """src/css/tokens.css matches what design_tokens.py renders."""
        assert TOKENS_CSS.exists(), f"{TOKENS_CSS} is missing"
        assert TOKENS_CSS.read_text(encoding="utf-8") == render_theme_css(), (
            "src/css/tokens.css is stale. Run: "
            "uv run python app/manage.py build_design_tokens"
        )

    def test_every_token_reaches_the_stylesheet(self) -> None:
        """No token is declared in Python but missing from the CSS."""
        css = render_theme_css()
        for group in TOKEN_GROUPS:
            for name, value in group.css_variables():
                assert f"{name}: {value};" in css


class TestTemplatesUseTokens:
    """Guards that stop colour escaping the token system again."""

    def test_no_raw_tailwind_palette_in_templates(self) -> None:
        """Templates use semantic tokens, never bg-gray-500 and friends."""
        offenders: list[str] = []
        for path in _template_files():
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                for match in RAW_PALETTE.finditer(line):
                    offenders.append(
                        f"{path.relative_to(TEMPLATE_ROOT)}:{lineno}: {match.group(0)}"
                    )
        assert not offenders, (
            "Raw Tailwind palette classes found. Use a semantic token from "
            "app/core/design_tokens.py instead:\n" + "\n".join(offenders)
        )

    def test_no_raw_tailwind_palette_in_ui_js(self) -> None:
        """NotipusUI builds markup in JS and must use tokens there too."""
        offenders = RAW_PALETTE.findall(UI_JS.read_text())
        assert not offenders, f"Raw palette classes in ui.js: {sorted(set(offenders))}"

    def test_components_bind_every_declared_prop(self) -> None:
        """Every <c-vars> prop carries a default.

        Cotton strips bare prop names from {{ attrs }} but never binds them, so
        with its default non-isolated context an unset bare prop resolves against
        whatever the surrounding page has in scope. Django's {% block %} puts a
        truthy `block` in context, which silently made every button inside
        {% block content %} full-width. An explicit default shadows the outer
        context and closes that off.
        """
        offenders: list[str] = []
        cotton_dir = TEMPLATE_ROOT / "cotton"
        for path in sorted(cotton_dir.rglob("*.html")):
            match = re.search(r"<c-vars\b([^/>]*)/>", path.read_text())
            if not match:
                continue
            for token in re.findall(r'(?:[:\w-]+="[^"]*"|[:\w-]+)', match.group(1)):
                if "=" not in token:
                    offenders.append(f"{path.relative_to(cotton_dir)}: {token}")
        assert not offenders, (
            "These <c-vars> props have no default and will leak from the page "
            'context. Give them one, e.g. block="":\n' + "\n".join(offenders)
        )


class TestIconNames:
    """Every Tabler icon referenced has to exist in the webfont."""

    @staticmethod
    def _available_icons() -> set[str] | None:
        """Read the icon names the installed Tabler webfont defines.

        Returns:
            The available names, or None when node_modules is not installed.
        """
        css = REPO_ROOT / "node_modules/@tabler/icons-webfont/dist/tabler-icons.css"
        if not css.exists():
            return None
        return set(re.findall(r"\.ti-([a-z0-9-]+):before", css.read_text()))

    def test_referenced_icons_exist(self) -> None:
        """No template or component asks for an icon Tabler does not ship.

        A missing name renders as blank space rather than failing, so this is
        the only thing that catches it. ti-star-filled shipped that way: the
        webfont has no -filled variants at all.
        """
        available = self._available_icons()
        if available is None:
            pytest.skip("node_modules is not installed; run `bun install` first")

        used: dict[str, set[str]] = {}

        def record(name: str, where: str) -> None:
            used.setdefault(name, set()).add(where)

        for path in _template_files():
            text = path.read_text()
            where = str(path.relative_to(TEMPLATE_ROOT))
            # The lookbehinds skip Cotton's :name="var" / :icon="var" dynamic
            # bindings, whose values are variable names rather than icon names.
            for name in re.findall(r"\bti ti-([a-z0-9-]+)", text):
                record(name, where)
            for name in re.findall(
                r'<c-icon(?:-tile)?[^>]*?(?<![:\w-])name="([a-z0-9-]+)"', text
            ):
                record(name, where)
            for name in re.findall(r'(?<![:\w-])icon="([a-z0-9-]+)"', text):
                record(name, where)

        for name in re.findall(r'"ti-([a-z0-9-]+)"', UI_JS.read_text()):
            record(name, "static/js/ui.js")

        missing = {
            name: sorted(files) for name, files in used.items() if name not in available
        }
        assert not missing, (
            "These Tabler icon names do not exist and render as blank space:\n"
            + "\n".join(f"  {name}: {files}" for name, files in sorted(missing.items()))
        )

    def test_the_check_actually_found_icons(self) -> None:
        """Guard against the extraction silently matching nothing."""
        available = self._available_icons()
        if available is None:
            pytest.skip("node_modules is not installed; run `bun install` first")
        assert len(available) > 1000

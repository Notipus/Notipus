"""Tests for the developer-only component library at /ui/.

The page renders every component, so it doubles as a smoke test: a component
with a broken tag, a missing prop or a bad template path fails here first.
"""

import pytest
from core.views.ui_library import ui_library
from django.http import Http404
from django.test import Client, RequestFactory, override_settings


@pytest.mark.django_db
class TestUiLibraryAvailability:
    """When the page exists, and when it must not."""

    def test_available_in_development(self, client: Client) -> None:
        """The library renders under the test settings, where DEBUG is on."""
        response = client.get("/ui/")

        assert response.status_code == 200
        assert b"Component library" in response.content

    def test_needs_no_login(self, client: Client) -> None:
        """It is a developer tool, not a workspace page: no auth wall to trip over."""
        response = client.get("/ui/")

        assert response.status_code == 200

    def test_view_404s_when_disabled(self) -> None:
        """Turning the setting off closes the view itself, not just the URL.

        The URL is only registered when the setting is on, so this exercises the
        second gate directly — the one that still holds if something else routes
        to the view.
        """
        request = RequestFactory().get("/ui/")

        with override_settings(UI_LIBRARY_ENABLED=False):
            with pytest.raises(Http404):
                ui_library(request)


@pytest.mark.django_db
class TestUiLibraryContent:
    """What the page has to show to be useful."""

    def test_renders_every_token_group(self, client: Client) -> None:
        """Each token group's heading and CSS variable names appear."""
        from core.design_tokens import TOKEN_GROUPS

        html = client.get("/ui/").content.decode()

        for group in TOKEN_GROUPS:
            assert group.title in html, f"Missing token group: {group.title}"
            for name, _value in group.css_variables():
                if "--" in name.removeprefix("--"):
                    continue  # Tailwind modifiers such as --text-body--line-height
                assert name in html, f"Missing token: {name}"

    def test_reports_the_contrast_audit(self, client: Client) -> None:
        """The page states the palette's WCAG status rather than implying it."""
        html = client.get("/ui/").content.decode()

        assert "pairings pass WCAG AA" in html
        assert "Measured contrast" in html

    def test_renders_every_component(self, client: Client) -> None:
        """Each component in the library has a section on the page.

        This is what makes the page a smoke test: rendering it exercises every
        component at once, so a broken one cannot reach a real page unnoticed.
        """
        html = client.get("/ui/").content.decode()

        for tag in (
            "c-button",
            "c-badge",
            "c-alert",
            "c-card",
            "c-icon-tile",
            "c-provider-logo",
            "c-progress",
            "c-stat",
            "c-checklist",
            "c-list",
            "c-table",
            "c-definition",
            "c-plan-card",
            "c-segmented",
            "c-empty-state",
        ):
            assert f"&lt;{tag}&gt;" in html, f"Missing component section: {tag}"

    def test_source_examples_are_shown_as_text(self, client: Client) -> None:
        """Example markup is escaped, not parsed away by the browser."""
        html = client.get("/ui/").content.decode()

        assert "&lt;c-badge tone=&quot;success&quot;" in html


@pytest.mark.django_db
class TestButtonWidthRegression:
    """Buttons stay content-width unless asked to stretch.

    Django's {% block %} tag puts a truthy `block` into the template context.
    With Cotton's default non-isolated context that used to satisfy <c-button>'s
    `block` prop, so every button inside {% block content %} rendered full-width.
    """

    def test_buttons_are_not_full_width_by_default(self, client: Client) -> None:
        """Only the two examples that ask for it render w-full."""
        html = client.get("/ui/").content.decode()

        buttons = [
            line
            for line in html.splitlines()
            if "rounded-control font-medium" in line and "inline-flex" in line
        ]
        stretched = [line for line in buttons if " w-full" in line]

        assert len(buttons) > 20, "Expected the library to render many buttons"
        assert len(stretched) == 2, (
            "Only the two <c-plan-card> examples pass block; a different count "
            "means the prop is being satisfied by something in the page context."
        )


def render_component(markup: str, context: dict[str, object]) -> str:
    """Render component markup through the Cotton loader.

    Cotton compiles <c-*> tags when a template is *loaded*, so markup passed
    straight to Template() is never compiled. Writing it into the template
    directory and loading it by name is what exercises the real path.

    Args:
        markup: Template source using <c-*> tags.
        context: Values made available to the template.

    Returns:
        The rendered HTML.
    """
    import tempfile
    from pathlib import Path

    from django.conf import settings
    from django.template.loader import render_to_string

    templates = Path(settings.BASE_DIR) / "core" / "templates"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", dir=templates, delete=True
    ) as handle:
        handle.write(markup)
        handle.flush()
        return render_to_string(Path(handle.name).name, context)


@pytest.mark.django_db
class TestZeroValueRegression:
    """A value of 0 survives the components that render values.

    Cotton binds :value="count" as an int, and each of these components used to
    gate its output on truthiness, so a legitimate 0 was dropped: an input
    rendered empty, a checkbox silently submitted the browser default of "on",
    and a definition row or code block rendered nothing at all. The unset default
    is "", so the test has to be presence rather than truthiness.
    """

    def test_input_keeps_a_zero_value(self) -> None:
        """An input given 0 renders value="0" rather than an empty field."""
        html = render_component(
            '<c-input name="count" :value="amount" />', {"amount": 0}
        )

        assert 'value="0"' in html

    def test_input_omits_an_unset_value(self) -> None:
        """An unset value still renders no value attribute at all."""
        html = render_component('<c-input name="count" />', {})

        assert "value=" not in html

    def test_checkbox_keeps_a_zero_value(self) -> None:
        """A checkbox valued 0 keeps it instead of submitting "on"."""
        html = render_component(
            '<c-checkbox name="tier" :value="tier" label="Free" />', {"tier": 0}
        )

        assert 'value="0"' in html

    def test_definition_row_keeps_a_zero_value(self) -> None:
        """A definition row valued 0 renders it rather than the empty slot."""
        html = render_component(
            '<c-definition.row label="Failed deliveries" :value="failures" />',
            {"failures": 0},
        )

        assert "0" in html.split("<dd")[1]

    def test_code_block_keeps_a_zero_value(self) -> None:
        """A code block valued 0 renders it rather than the empty slot."""
        html = render_component('<c-code-block :value="port" />', {"port": 0})

        assert "0" in html.split("<code")[1]

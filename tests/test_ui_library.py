"""Tests for the developer-only component library at /ui/.

The page renders every component, so it doubles as a smoke test: a component
with a broken tag, a missing prop or a bad template path fails here first.
"""

from html.parser import HTMLParser

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


VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


class CrushedColumnFinder(HTMLParser):
    """Walks the tree tracking whether the open ancestor wraps.

    The stack holds (tag, wraps) rather than a bare flag so an end tag pops to
    its own start tag. Popping blindly desynced it: HTMLParser reports a
    self-closing `<path />` — every inline SVG on /ui/ has them — as a start
    *and* an end tag, so the end popped the real parent's entry and everything
    after the first SVG was measured against the wrong ancestor.
    """

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.offenders: list[str] = []

    def measure(self, attrs: list[tuple[str, str | None]]) -> bool:
        """Record an offender, and report whether this element itself wraps."""
        classes = dict(attrs).get("class") or ""
        names = classes.split()
        if self.stack and self.stack[-1][1]:
            grows = "flex-1" in names or "grow" in names
            sized = any(name.startswith("basis-") for name in names)
            if grows and not sized:
                self.offenders.append(classes)
        return "flex" in names and "flex-wrap" in names

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Measure the element, then make it the open ancestor."""
        wraps = self.measure(attrs)
        if tag not in VOID_TAGS:
            self.stack.append((tag, wraps))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """`<tag />` is measured but never becomes anyone's ancestor."""
        self.measure(attrs)

    def handle_endtag(self, tag: str) -> None:
        """Unwind to this tag's own start, ignoring anything left unclosed."""
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def crushed_columns(html: str) -> list[str]:
    """Find growing columns that stop their wrapping parent from ever wrapping.

    `flex-1` is `flex: 1 1 0%`, so the item's hypothetical size is zero and a
    `flex-wrap` parent always believes the line fits. The actions beside it keep
    their full width and the column is squeezed to whatever is left — which on a
    narrow viewport is a word per line. An explicit `basis-*` restores the width
    the wrap algorithm needs to see.

    Args:
        html: Rendered markup to inspect.

    Returns:
        The class attribute of each offending element.
    """
    finder = CrushedColumnFinder()
    finder.feed(html)
    return finder.offenders


@pytest.mark.django_db
class TestWrappingRowRegression:
    """A wrapping row wraps instead of crushing its own text.

    <c-integration-row> shipped as `flex-wrap` around a bare `flex-1` column, so
    on a narrow viewport the Slack row kept Configure / Send test / Disconnect on
    the name's line and reflowed the description one word per line.
    """

    def test_ui_library_has_no_crushed_columns(self, client: Client) -> None:
        """No component on /ui/ grows a zero-basis column inside a wrapping row."""
        offenders = crushed_columns(client.get("/ui/").content.decode())

        assert offenders == [], (
            f"flex-1 inside flex-wrap never wraps; add a basis-* to: {offenders}"
        )

    def test_integration_row_has_no_crushed_columns(self) -> None:
        """The row that showed the bug is checked directly: /ui/ omits it."""
        html = render_component(
            '<c-integration-row :integration="integration" provider="slack">'
            '<c-slot name="actions"><c-button size="sm">Configure</c-button>'
            "</c-slot></c-integration-row>",
            {"integration": {"name": "Slack", "connected": True, "description": "x"}},
        )

        assert crushed_columns(html) == []

    def test_self_closing_tag_does_not_desync_the_walk(self) -> None:
        """`<path />` must not pop the flex-wrap parent off the stack.

        HTMLParser reports a self-closing tag as both a start and an end tag.
        Popping on every end tag lost the real parent, so everything after the
        first inline SVG was measured against the wrong ancestor and offenders
        went unreported.
        """
        html = (
            '<div class="flex flex-wrap">'
            '<svg><path d="M0 0" /></svg>'
            '<div class="flex-1">crushed</div>'
            "</div>"
        )

        assert crushed_columns(html) == ["flex-1"]


@pytest.mark.django_db
class TestRowsAgreeOnActionPlacement:
    """Rows in one list put their actions in the same place as each other.

    Placement used to follow from `flex-wrap`, which each row decided from its
    own content: on /integrations/ the Shopify row kept its single Disconnect
    button inline while the Slack row below dropped three buttons onto their own
    line. The flip is now a container query, so every row in a list agrees.

    It is a container query and not `sm:` because the container, not the
    viewport, is the constraint — that page's cards sit in a two-column grid, so
    at a 1280px viewport these rows live in a 530px card.
    """

    def test_both_row_components_share_one_rule(self) -> None:
        """Both rows flip at the same container width, not at a viewport one."""
        rows = {
            "integration": render_component(
                '<c-integration-row :integration="integration" provider="slack">'
                '<c-slot name="actions"><c-button size="sm">Configure</c-button>'
                "</c-slot></c-integration-row>",
                {"integration": {"name": "Slack", "connected": True}},
            ),
            "list": render_component(
                '<c-list.row title="ada@example.com">'
                '<c-slot name="actions"><c-button size="sm">Remove</c-button>'
                "</c-slot></c-list.row>",
                {},
            ),
        }

        for name, html in rows.items():
            assert "@container" in html, f"{name} row declares no query container"
            assert "flex flex-col" in html, f"{name} row does not stack by default"
            assert "@2xl:flex-row" in html, (
                f"{name} row does not flip at @2xl; if this moved, move it in "
                "both rows or lists of each will disagree with each other"
            )


@pytest.mark.django_db
class TestIntegrationRowEventBadge:
    """What the row actually says about events.

    The service data was already right and already tested; the template threw it
    away. `{% if x is defined %}` is Jinja, and these are Django templates, where
    `is` is an identity test and `defined` is an undefined variable resolving to
    None — so the guard meant `x is None`, the inverse of the intent. Every case
    below came out wrong, and nothing rendered the row to notice.
    """

    def row(self, integration: dict[str, object]) -> str:
        """Render an integration row for one context dict.

        Args:
            integration: The entry the integrations view would pass in.

        Returns:
            The rendered HTML.
        """
        return render_component(
            '<c-integration-row :integration="integration" provider="slack" />',
            {"integration": integration},
        )

    def test_a_receiving_source_says_so(self) -> None:
        """A source with events shows "Receiving events", which never rendered."""
        html = self.row(
            {"name": "Shopify", "connected": True, "event_state": "receiving"}
        )

        assert "Receiving events" in html
        assert "No events yet" not in html

    def test_a_waiting_source_says_so(self) -> None:
        """A connected source with no events keeps the nudge to send one."""
        html = self.row(
            {"name": "Shopify", "connected": True, "event_state": "waiting"}
        )

        assert "No events yet" in html
        assert "Send a test event from Shopify" in html

    def test_a_destination_claims_nothing_about_events(self) -> None:
        """The reported bug: Slack announced "No events yet" for ever.

        A destination's entry carries no event state at all, and a missing key
        must read as "no claim" rather than as "no events".
        """
        html = self.row({"name": "Slack", "connected": True})

        assert "No events yet" not in html
        assert "Receiving events" not in html
        assert "Send a test event" not in html
        assert "Connected" in html


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

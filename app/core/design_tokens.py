"""Design tokens for the Notipus UI — the single source of truth.

Every colour, radius, shadow and type step used by the interface is declared
here once. Three consumers read from this module:

1. ``core.management.commands.build_design_tokens`` renders ``src/css/tokens.css``,
   which ``src/css/main.css`` imports into Tailwind's ``@theme`` block. That is
   what turns a token into utilities such as ``bg-surface`` or ``text-content-muted``.
2. The dev-only UI library page (``core.views.ui_library``) renders the swatches
   and measured contrast ratios straight off ``TOKEN_GROUPS`` and ``CONTRAST_RULES``.
3. ``tests/test_design_tokens.py`` asserts the generated CSS is in sync and that
   every declared colour pairing clears its WCAG threshold.

Adding a token means editing this file and re-running::

    uv run python app/manage.py build_design_tokens

Contrast policy
---------------
Google PageSpeed Insights runs Lighthouse, whose ``color-contrast`` audit is the
axe-core implementation of WCAG 2.1 SC 1.4.3 (AA): 4.5:1 for body text, 3:1 for
large text (>=24px, or >=18.7px bold). Foreground/background pairings that the
components actually produce are declared in ``CONTRAST_RULES`` and enforced by
test. Non-text pairings (control borders, graphical accents) are held to SC
1.4.11's 3:1 even though Lighthouse does not audit them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# --------------------------------------------------------------------------
# Contrast maths (WCAG 2.1 relative luminance)
# --------------------------------------------------------------------------


def _channel_luminance(value: int) -> float:
    """Linearise one 0-255 sRGB channel.

    Args:
        value: Channel value in the 0-255 range.

    Returns:
        The linearised channel value used by the luminance formula.
    """
    channel = value / 255
    if channel <= 0.04045:
        return channel / 12.92
    # float(...) because ** on floats is typed as returning Any.
    return float(((channel + 0.055) / 1.055) ** 2.4)


def parse_hex(color: str) -> tuple[int, int, int]:
    """Split a ``#rrggbb`` string into its integer channels.

    Args:
        color: Hex colour, with or without the leading ``#``.

    Returns:
        The red, green and blue channels as 0-255 integers.

    Raises:
        ValueError: If the string is not a six-digit hex colour.
    """
    digits = color.lstrip("#")
    if len(digits) != 6:
        raise ValueError(f"Expected a six-digit hex colour, got {color!r}")
    return (
        int(digits[0:2], 16),
        int(digits[2:4], 16),
        int(digits[4:6], 16),
    )


def relative_luminance(color: str) -> float:
    """Compute the WCAG relative luminance of a hex colour.

    Args:
        color: Hex colour such as ``#f05800``.

    Returns:
        Relative luminance in the 0.0 (black) to 1.0 (white) range.
    """
    red, green, blue = parse_hex(color)
    return (
        0.2126 * _channel_luminance(red)
        + 0.7152 * _channel_luminance(green)
        + 0.0722 * _channel_luminance(blue)
    )


def contrast_ratio(foreground: str, background: str) -> float:
    """Compute the WCAG contrast ratio between two hex colours.

    Args:
        foreground: Hex colour of the text or graphic.
        background: Hex colour it sits on.

    Returns:
        The ratio, from 1.0 (identical) to 21.0 (black on white).
    """
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------------
# Token declarations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    """One design token.

    Attributes:
        name: Token name without the CSS namespace, e.g. ``content-muted``.
        value: The CSS value, e.g. ``#475569``.
        usage: One line telling a developer when to reach for it.
        graphic_only: True for colours that cannot carry text at 4.5:1. The UI
            library page flags these so nobody puts a label on one.
        modifiers: Extra Tailwind theme modifiers keyed by suffix, e.g.
            ``(("line-height", "1.25"),)`` renders ``--text-title--line-height``.
    """

    name: str
    value: str
    usage: str
    graphic_only: bool = False
    modifiers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TokenGroup:
    """A titled set of tokens sharing one CSS namespace.

    Attributes:
        title: Human-readable heading for the UI library page.
        namespace: Tailwind theme namespace, e.g. ``--color``.
        description: What the group is for.
        tokens: The tokens themselves.
        is_color: Whether values should render as swatches.
    """

    title: str
    namespace: str
    description: str
    tokens: tuple[Token, ...]
    is_color: bool = True

    def css_variables(self) -> list[tuple[str, str]]:
        """Return the ``(custom-property, value)`` pairs for this group.

        Returns:
            Pairs such as ``("--color-content-muted", "#475569")``, followed by
            any Tailwind modifiers a token declares.
        """
        pairs: list[tuple[str, str]] = []
        for token in self.tokens:
            base = f"{self.namespace}-{token.name}"
            pairs.append((base, token.value))
            pairs.extend(
                (f"{base}--{suffix}", value) for suffix, value in token.modifiers
            )
        return pairs


SURFACE_TOKENS = TokenGroup(
    title="Surfaces",
    namespace="--color",
    description=(
        "Backgrounds, from the page underneath to the cards on top of it. The "
        "neutrals carry a trace of the logo's teal rather than Tailwind's blue-grey, "
        "so the orange sits inside the palette instead of on top of it."
    ),
    tokens=(
        Token("canvas", "#f5f8f8", "The page background behind all content."),
        Token("surface", "#ffffff", "Cards, panels, the nav bar, form controls."),
        Token("surface-muted", "#e9f0f0", "Table headers, card footers, inset wells."),
        Token(
            "surface-inverse",
            "#04212a",
            "Code and credential blocks; pair with content-inverse.",
        ),
    ),
)

CONTENT_TOKENS = TokenGroup(
    title="Content",
    namespace="--color",
    description=(
        "Text and icon colours, drawn from the logo's ink. Three weights is the "
        "whole vocabulary."
    ),
    tokens=(
        Token("content", "#04212a", "Headings and primary body copy."),
        Token("content-muted", "#3d5a62", "Secondary copy, labels, help text."),
        Token("content-subtle", "#506b73", "Captions, metadata, standalone icons."),
        Token(
            "content-inverse", "#ffffff", "Text on any -solid fill or surface-inverse."
        ),
    ),
)

LINE_TOKENS = TokenGroup(
    title="Lines",
    namespace="--color",
    description="Borders and dividers. -control is the only one that carries meaning.",
    tokens=(
        Token("border", "#d8e3e4", "Card edges, dividers, table rules."),
        Token("border-strong", "#bcccce", "Emphasised separators and hovered edges."),
        Token("border-control", "#6f878c", "Input and checkbox outlines; clears 3:1."),
    ),
)

ACTION_TOKENS = TokenGroup(
    title="Action",
    namespace="--color",
    description=(
        "The primary interactive fill: the logo's ink. White text on it clears "
        "16:1, which no orange can do — the brand orange tops out at 2.73:1 with "
        "white. Buttons are therefore ink, and the orange does accent work."
    ),
    tokens=(
        Token("action", "#04212a", "Primary button and toggle fills."),
        Token("action-hover", "#0d3946", "Hover and active state of those fills."),
    ),
)

BRAND_TOKENS = TokenGroup(
    title="Brand",
    namespace="--color",
    description=(
        "Notipus orange, taken from the logo. It is an accent, not a fill for white "
        "text: brand-bright is the true logo value and only ever sits behind ink "
        "text; brand is the deeper value that clears 3:1 as a graphic on light."
    ),
    tokens=(
        Token(
            "brand",
            "#e8590c",
            "Meters, status dots, active markers. Never behind text.",
            graphic_only=True,
        ),
        Token("brand-bright", "#f57a1a", "The logo orange; fills that carry ink text."),
        Token("brand-text", "#b8430a", "Links and brand-coloured text on light."),
        Token("brand-surface", "#fef3e8", "Tinted panels, selected rows, badges."),
        Token("brand-border", "#f9c99a", "Edges of brand-surface elements."),
    ),
)

SUCCESS_TOKENS = TokenGroup(
    title="Success",
    namespace="--color",
    description="Connected integrations, completed payments, confirmations.",
    tokens=(
        Token("success-solid", "#12714a", "Filled buttons and dots."),
        Token("success-solid-hover", "#0e5c3c", "Hover state of that fill."),
        Token("success-text", "#116b45", "Success text on light backgrounds."),
        Token("success-surface", "#eef8f2", "Alert and badge background."),
        Token("success-border", "#c3e5d4", "Alert and badge edge."),
    ),
)

WARNING_TOKENS = TokenGroup(
    title="Warning",
    namespace="--color",
    description=(
        "Trials running out, quotas near their limit, setup left undone. A tint-first "
        "family: any yellow dark enough to carry white text turns brown and reads as "
        "the brand orange, so warnings are carried by warning-surface plus an icon "
        "and the solid value is reserved for dots and meters — never a button."
    ),
    tokens=(
        Token("warning-solid", "#a16207", "Status dots and meter fills. Not buttons."),
        Token("warning-solid-hover", "#854d0e", "Hover state of those graphics."),
        Token("warning-text", "#8f5a08", "Warning text on light backgrounds."),
        Token("warning-surface", "#fdf6e7", "Alert and badge background."),
        Token("warning-border", "#f5dfa8", "Alert and badge edge."),
    ),
)

DANGER_TOKENS = TokenGroup(
    title="Danger",
    namespace="--color",
    description="Destructive actions, failed payments, errors.",
    tokens=(
        Token("danger-solid", "#b32318", "Filled destructive buttons."),
        Token("danger-solid-hover", "#96190f", "Hover state of that fill."),
        Token("danger-text", "#b32318", "Error text and field messages."),
        Token("danger-surface", "#fdf1ef", "Alert and badge background."),
        Token("danger-border", "#f7cfc8", "Alert and badge edge."),
    ),
)

INFO_TOKENS = TokenGroup(
    title="Info",
    namespace="--color",
    description=(
        "Neutral guidance: what a page does, what happens next. A mid-tone teal from "
        "the logo's own hue family rather than a saturated blue, so it harmonises "
        "with the brand instead of competing with it for attention."
    ),
    tokens=(
        Token("info-solid", "#0d5e6e", "Filled informational buttons."),
        Token("info-solid-hover", "#0a4b58", "Hover state of that fill."),
        Token("info-text", "#0d5e6e", "Informational text on light backgrounds."),
        Token("info-surface", "#eaf4f6", "Alert and badge background."),
        Token("info-border", "#c3dfe5", "Alert and badge edge."),
    ),
)

FOCUS_TOKENS = TokenGroup(
    title="Focus",
    namespace="--color",
    description=(
        "One focus colour for the whole interface. The brand orange, so the one "
        "moment every keyboard user sees on every control is the brand's."
    ),
    tokens=(Token("focus", "#e8590c", "Focus ring on every interactive element."),),
)

RADIUS_TOKENS = TokenGroup(
    title="Radius",
    namespace="--radius",
    description="Three steps. Controls are pill-free; cards are the softest thing.",
    tokens=(
        Token("control", "0.5rem", "Buttons, inputs, badges, menu items."),
        Token("card", "0.75rem", "Cards, modals, alerts."),
        Token("pill", "9999px", "Status pills and avatars only."),
    ),
    is_color=False,
)

SHADOW_TOKENS = TokenGroup(
    title="Shadow",
    namespace="--shadow",
    description="Elevation is rare: a card sits flat, only overlays lift.",
    tokens=(
        Token(
            "card", "0 1px 2px 0 rgb(15 23 42 / 0.04)", "Resting cards and controls."
        ),
        Token(
            "raised",
            "0 4px 12px -2px rgb(15 23 42 / 0.10)",
            "Hovered cards, dropdowns.",
        ),
        Token(
            "overlay", "0 24px 48px -12px rgb(15 23 42 / 0.25)", "Modals and toasts."
        ),
    ),
    is_color=False,
)

FONT_TOKENS = TokenGroup(
    title="Typefaces",
    namespace="--font",
    description=(
        "System stacks, deliberately. A webfont would cost a render-blocking "
        "request and LCP on the very audit we are targeting, and this product's "
        "personality lives in its colour and rhythm rather than its letterforms."
    ),
    tokens=(
        Token(
            "sans",
            (
                "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, "
                "'Helvetica Neue', Arial, sans-serif"
            ),
            "Everything the interface says.",
        ),
        Token(
            "mono",
            (
                "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
                "'Liberation Mono', monospace"
            ),
            "Webhook URLs, signing secrets, IDs — anything meant to be copied.",
        ),
    ),
    is_color=False,
)

TEXT_TOKENS = TokenGroup(
    title="Type scale",
    namespace="--text",
    description=(
        "Six semantic steps, each shipping its own leading so a heading is never "
        "sized or spaced by eye. Reach for the role, not the number."
    ),
    tokens=(
        Token(
            "display",
            "2.25rem",
            "Marketing and empty-state headlines.",
            modifiers=(("line-height", "1.1"), ("letter-spacing", "-0.02em")),
        ),
        Token(
            "title",
            "1.5rem",
            "The single h1 on a page.",
            modifiers=(("line-height", "1.25"), ("letter-spacing", "-0.01em")),
        ),
        Token(
            "heading",
            "1.125rem",
            "Card and section headings.",
            modifiers=(("line-height", "1.4"),),
        ),
        Token(
            "lead",
            "1rem",
            "Page subtitles and intro copy — the step above default body.",
            modifiers=(("line-height", "1.6"),),
        ),
        Token(
            "body",
            "0.875rem",
            "Body copy and controls — the default.",
            modifiers=(("line-height", "1.55"),),
        ),
        Token(
            "caption",
            "0.75rem",
            "Badges, table headers, metadata.",
            modifiers=(("line-height", "1.4"),),
        ),
    ),
    is_color=False,
)

SPACING_TOKENS = TokenGroup(
    title="Layout rhythm",
    namespace="--spacing",
    description=(
        "Named spacing for the three decisions that were previously made ad hoc on "
        "every page, which is where most of the inconsistency came from."
    ),
    tokens=(
        Token("gutter", "1.5rem", "Padding inside a card or panel."),
        Token("stack", "1.5rem", "Vertical gap between sibling cards."),
        Token("section", "3rem", "Vertical gap between major page sections."),
    ),
    is_color=False,
)

TOKEN_GROUPS: tuple[TokenGroup, ...] = (
    SURFACE_TOKENS,
    CONTENT_TOKENS,
    LINE_TOKENS,
    ACTION_TOKENS,
    BRAND_TOKENS,
    SUCCESS_TOKENS,
    WARNING_TOKENS,
    DANGER_TOKENS,
    INFO_TOKENS,
    FOCUS_TOKENS,
    RADIUS_TOKENS,
    SHADOW_TOKENS,
    FONT_TOKENS,
    TEXT_TOKENS,
    SPACING_TOKENS,
)


def color_value(name: str) -> str:
    """Look up a colour token's hex value by token name.

    Args:
        name: Token name without namespace, e.g. ``brand-solid``.

    Returns:
        The hex value.

    Raises:
        KeyError: If no colour token by that name exists.
    """
    for group in TOKEN_GROUPS:
        if not group.is_color:
            continue
        for token in group.tokens:
            if token.name == name:
                return token.value
    raise KeyError(f"Unknown colour token: {name}")


# --------------------------------------------------------------------------
# Contrast rules
# --------------------------------------------------------------------------


class ContrastKind(StrEnum):
    """What a pairing is used for, which sets its WCAG threshold."""

    BODY_TEXT = "body text"
    LARGE_TEXT = "large text"
    NON_TEXT = "non-text"


MINIMUM_RATIOS: dict[ContrastKind, float] = {
    ContrastKind.BODY_TEXT: 4.5,
    ContrastKind.LARGE_TEXT: 3.0,
    ContrastKind.NON_TEXT: 3.0,
}


@dataclass(frozen=True)
class ContrastRule:
    """A foreground/background pairing the components actually render.

    Attributes:
        foreground: Colour token name of the text or graphic.
        background: Colour token name it sits on.
        kind: Which WCAG threshold applies.
        where: Where in the UI this pairing occurs.
    """

    foreground: str
    background: str
    kind: ContrastKind = ContrastKind.BODY_TEXT
    where: str = ""
    ratio: float = field(init=False)

    def __post_init__(self) -> None:
        """Measure the pairing once, at construction."""
        object.__setattr__(
            self,
            "ratio",
            contrast_ratio(color_value(self.foreground), color_value(self.background)),
        )

    @property
    def minimum(self) -> float:
        """Return the ratio this pairing must clear.

        Returns:
            4.5 for body text, 3.0 for large text and non-text.
        """
        return MINIMUM_RATIOS[self.kind]

    @property
    def passes(self) -> bool:
        """Whether the measured ratio clears the threshold.

        Returns:
            True if the pairing is compliant.
        """
        return self.ratio >= self.minimum


def _text_on_backgrounds(
    foreground: str, where: str, kind: ContrastKind = ContrastKind.BODY_TEXT
) -> list[ContrastRule]:
    """Build rules for one text colour over every neutral background.

    Args:
        foreground: Colour token name of the text.
        where: Where the pairing occurs, for failure messages.
        kind: WCAG threshold to apply.

    Returns:
        One rule per neutral surface token.
    """
    return [
        ContrastRule(foreground, background, kind, where)
        for background in ("canvas", "surface", "surface-muted")
    ]


def _status_rules(
    status: str, *, solid_carries_text: bool = True
) -> list[ContrastRule]:
    """Build the standard rule set for one status colour family.

    Args:
        status: Family prefix, one of ``success``/``warning``/``danger``/``info``.
        solid_carries_text: Whether the family's solid fill ever sits behind a
            white label. False for warning, whose solid is graphics-only.

    Returns:
        Rules covering tinted text, solid fills and the surface edge.
    """
    rules = [
        *_text_on_backgrounds(f"{status}-text", f"{status} text on neutral surfaces"),
        ContrastRule(
            f"{status}-text",
            f"{status}-surface",
            where=f"{status} alert and badge copy",
        ),
        ContrastRule(
            f"{status}-solid",
            f"{status}-surface",
            ContrastKind.NON_TEXT,
            where=f"{status} status dot on its own tint",
        ),
        ContrastRule(
            f"{status}-solid",
            "surface",
            ContrastKind.NON_TEXT,
            where=f"{status} status dot on a card",
        ),
    ]
    if solid_carries_text:
        rules += [
            ContrastRule(
                "content-inverse",
                f"{status}-solid",
                where=f"label on a {status} button",
            ),
            ContrastRule(
                "content-inverse",
                f"{status}-solid-hover",
                where=f"label on a hovered {status} button",
            ),
        ]
    return rules


CONTRAST_RULES: tuple[ContrastRule, ...] = (
    # Neutral text.
    *_text_on_backgrounds("content", "headings and body copy"),
    *_text_on_backgrounds("content-muted", "labels and secondary copy"),
    *_text_on_backgrounds("content-subtle", "captions, metadata and icons"),
    ContrastRule("content-inverse", "surface-inverse", where="copy on a dark panel"),
    # The primary action.
    ContrastRule("content-inverse", "action", where="primary button label"),
    ContrastRule(
        "content-inverse", "action-hover", where="hovered primary button label"
    ),
    # Brand.
    *_text_on_backgrounds("brand-text", "links and brand text"),
    ContrastRule("brand-text", "brand-surface", where="text in a brand-tinted panel"),
    ContrastRule(
        "content", "brand-bright", where="ink label on a bright orange highlight"
    ),
    ContrastRule(
        "brand", "surface", ContrastKind.NON_TEXT, where="brand accent bars and icons"
    ),
    ContrastRule(
        "brand", "canvas", ContrastKind.NON_TEXT, where="brand accent bars on the page"
    ),
    ContrastRule(
        "brand",
        "surface-muted",
        ContrastKind.NON_TEXT,
        where="brand meter fill in an inset well",
    ),
    ContrastRule(
        "brand",
        "brand-surface",
        ContrastKind.NON_TEXT,
        where="brand dot on its own tint",
    ),
    # Controls and focus.
    ContrastRule(
        "border-control", "surface", ContrastKind.NON_TEXT, where="input outline"
    ),
    ContrastRule(
        "border-control", "canvas", ContrastKind.NON_TEXT, where="input outline on page"
    ),
    ContrastRule(
        "focus", "surface", ContrastKind.NON_TEXT, where="focus ring on a card"
    ),
    ContrastRule(
        "focus", "canvas", ContrastKind.NON_TEXT, where="focus ring on the page"
    ),
    # Status families.
    *_status_rules("success"),
    *_status_rules("warning", solid_carries_text=False),
    *_status_rules("danger"),
    *_status_rules("info"),
)


def failing_rules() -> list[ContrastRule]:
    """Return every declared pairing that misses its WCAG threshold.

    Returns:
        The non-compliant rules; empty when the palette is clean.
    """
    return [rule for rule in CONTRAST_RULES if not rule.passes]


# --------------------------------------------------------------------------
# CSS generation
# --------------------------------------------------------------------------

CSS_HEADER = """\
/* Generated by `uv run python app/manage.py build_design_tokens`.
 *
 * Do not edit by hand — change app/core/design_tokens.py and regenerate.
 * tests/test_design_tokens.py fails if this file drifts from that module.
 */
"""


def render_theme_css() -> str:
    """Render the Tailwind ``@theme`` block for every token.

    Returns:
        The full contents of ``src/css/tokens.css``.
    """
    lines = [CSS_HEADER, "", "@theme {"]
    for index, group in enumerate(TOKEN_GROUPS):
        if index:
            lines.append("")
        lines.append(f"  /* {group.title} — {group.description} */")
        for name, value in group.css_variables():
            lines.append(f"  {name}: {value};")
    lines.append("}")
    return "\n".join(lines) + "\n"

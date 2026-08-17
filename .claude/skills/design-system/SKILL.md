---
name: design-system
description: >-
  Notipus's design tokens, colour rules and visual-balance conventions. Read
  this before choosing any colour, adding or changing a design token, building
  a new Cotton component, or judging whether a screen looks right — and
  whenever a change touches app/core/design_tokens.py, src/css/tokens.css, or
  the look of app/core/templates/cotton/. Covers where the palette comes from,
  why buttons are ink rather than orange, the WCAG budget Google PageSpeed
  audits, and the specific balance mistakes this UI has already made once.
---

# Notipus design system

Two files hold the whole system:

```
app/core/design_tokens.py       every colour, type step, radius, shadow, spacing
app/core/templates/cotton/      every component, each owning its own classes
```

`src/css/tokens.css` is **generated** — never edit it:

```bash
uv run python app/manage.py build_design_tokens          # regenerate
uv run python app/manage.py build_design_tokens --check  # CI gate
```

See `docs/adr/002-design-system.md` for the full rationale.

## Look at it before you change it

```bash
DEBUG=True PYTHONPATH=app DJANGO_SETTINGS_MODULE=django_notipus.test_settings \
  uv run python app/manage.py runserver   # then open /ui/
```

`/ui/` renders every token and component with live contrast measurements. It is
also a smoke test: one page exercises all of them.

Screenshot your change. Colour and balance problems are invisible in a diff and
obvious in a picture.

## The palette comes from the logo

`app/static/img/notipus-logo.png` is the source of truth for hue:

| | value | hue |
|---|---|---|
| ink | `#04212a` | 194 (deep teal) |
| orange | `#f57a1a` | 26 |

The neutrals are **teal-tinted**, derived from that ink — *not* Tailwind slate,
which sits at hue 210–222 and made the orange look like it had been dropped onto
someone else's greyscale. If you add a neutral, keep it in the 185–195 range at
very low chroma.

### Primary actions are ink, not orange

This is the rule people try to undo, so here is the arithmetic:

| fill | white text | ink text |
|---|---|---|
| `#f57a1a` the logo orange | **2.73** ✗ | 6.11 ✓ |
| `#f05800` | 3.44 ✗ | 4.86 ✓ |
| `#bb3700` rust | 5.74 ✓ | 2.91 ✗ |
| `#04212a` ink | **16.72** ✓ | — |

No orange carries white text at 4.5:1. Forcing one to comply produces rust,
which does not look like the brand. So:

- `--color-action` / `--color-action-hover` — the ink. Every primary button.
- `--color-brand` (`#e8590c`) — **graphic only**, clears 3:1 as a mark on light.
  Meters, status dots, active markers. Never behind text.
- `--color-brand-bright` (`#f57a1a`) — the true logo orange, only ever a fill
  under ink text. Currently one appearance: the "Most popular" ribbon.
- `--color-brand-text` (`#b8430a`) — links and brand-coloured text.
- `--color-focus` — the brand orange, so the one thing every keyboard user sees
  on every control is the brand's.

Orange earns its presence by being rare. If a screen has orange buttons *and*
orange badges *and* orange headings, take some away.

### Status colours

`{success,warning,danger,info}-{solid,solid-hover,text,surface,border}`.

- **Warning is tint-first.** Any yellow dark enough for white text turns brown
  and reads as the brand orange. Warnings are a tint plus an icon; the solid
  value is for dots and meters, never buttons. The JS confirm dialog's warning
  variant uses the danger button for this reason.
- **Info is teal** (`#0d5e6e`), from the logo's hue family. It used to be a
  saturated blue — a fifth hue competing with the brand while carrying the least
  meaning.

## Never use Tailwind's palette

`bg-gray-100`, `text-red-600`, `border-slate-300` and friends are **rejected by
`tests/test_design_tokens.py`** in templates and in `app/static/js/ui.js`. Reach
for a semantic token:

```
surfaces  bg-canvas  bg-surface  bg-surface-muted  bg-surface-inverse
text      text-content  text-content-muted  text-content-subtle  text-content-inverse
lines     border-border  border-border-strong  border-border-control
action    bg-action  hover:bg-action-hover      (with text-content-inverse)
brand     bg-brand  bg-brand-bright  text-brand-text  bg-brand-surface  border-brand-border
rhythm    p-gutter  gap-stack  rounded-control  rounded-card  text-body  text-title
```

Provider brand hexes (Stripe `#635bff`, Shopify `#95BF47`, Telegram `#0088cc`)
are the exception: they belong to those companies, so they stay hardcoded in
`cotton/provider_logo.html` and must not become tokens or shift with our palette.

## Changing a colour

1. Edit the token in `app/core/design_tokens.py`.
2. Add a `ContrastRule` for **every pairing it creates**. A colour with no rule
   is a colour nobody has checked.
3. `uv run python app/manage.py build_design_tokens`
4. `uv run pytest tests/test_design_tokens.py`
5. `bun run build:css`, then look at `/ui/`.

### The contrast budget

Google PageSpeed runs Lighthouse, whose `color-contrast` audit is axe-core's
implementation of WCAG 2.1 AA:

| kind | ratio |
|---|---|
| body text | 4.5:1 |
| large text (≥24px, or ≥18.7px bold) | 3:1 |
| non-text (control borders, graphic marks) | 3:1 — SC 1.4.11, not audited by Lighthouse, held anyway |

Disabled controls are exempt (WCAG 1.4.3 "inactive"), which is why
`disabled:opacity-60` is allowed.

Helpers live in the same module: `contrast_ratio()`, `relative_luminance()`,
`parse_hex()`. Use them to check a candidate *before* committing to it.

## Balance

These are mistakes this UI has already made. Check for them.

**A leading icon must not orphan the content it labels.** `<c-stat>` had a 32px
icon tile top-aligned against a 14px label, so the tile floated, indented every
line by its own width, and left the big number in an L-shaped void. A stat is
*label → number* on one flush left edge; the icon is now an inline glyph at
label size. Where a tile genuinely belongs (card headers, action rows) it is
vertically centred against the whole block, not pinned to the first line.

**Sibling rows need the same leading size.** `<c-list.row>` mixed 40px avatars
with 32px icon tiles, shifting text 7px between rows. If two things can appear
in the same slot, they are the same size.

**Never render an empty container.** `<c-card>` used to emit its padded body div
even with nothing in it, so a header-only card showed a blank strip that read as
broken or still loading. It now checks `slot.strip`.

**One treatment per kind of value.** A workspace ID and a webhook URL are the
same thing — an identifier you copy — and had two different looks. `<c-code-block>`
is now the only monospace-and-copyable treatment; `<c-readonly>` is for
non-copyable facts like the current plan.

**Sizes come from the scale.** `text-title`, `text-heading`, `text-lead`,
`text-body`, `text-caption` — each ships its own line-height. Never size by eye
with `text-[15px]` or reach for a raw Tailwind step when a semantic one exists.

**Spacing comes from the rhythm tokens.** `p-gutter` inside cards, `gap-stack`
between them, `gap-section` between page sections. Ad-hoc `px-6 py-4` on every
page is where the original inconsistency came from.

## What the tests enforce

`tests/test_design_tokens.py` fails the build on:

- any declared pairing missing its WCAG threshold
- `src/css/tokens.css` drifting from `design_tokens.py`
- raw Tailwind palette classes in templates or `ui.js`
- a Tabler icon name that does not exist in the webfont (a missing icon renders
  as blank space and is otherwise silent — `ti-star-filled` shipped that way,
  because the webfont has no `-filled` variants at all)
- a Cotton `<c-vars>` prop without a default, which would let it leak from the
  surrounding page context

`tests/test_ui_library.py` renders `/ui/`, exercising every component at once.

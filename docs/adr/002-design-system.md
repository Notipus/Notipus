# ADR-002: Design tokens and a Cotton component library

**Status:** Accepted
**Date:** 2026-08-16
**Authors:** Team

## Context

The interface had drifted into several parallel systems. A single page could
contain all of these ways of drawing the same thing:

| Concern | Ways it was done |
|---------|------------------|
| Buttons | `.btn-primary` CSS class, an inline `inline-flex px-4 py-2 …` chain, a per-provider brand-coloured button, a gradient button |
| Icons | Tabler webfont (`ti ti-*`), hand-pasted Heroicons SVG, emoji (💰, 📡, 🤔) |
| Flash messages | Rendered by `base.html.j2`, and re-rendered with different markup by four pages |
| Cards | `.card`, `bg-white shadow rounded-lg`, `bg-white shadow-lg rounded-xl border border-gray-100`, `rounded-2xl shadow-sm` |
| Page headers | Six variants, differing in max-width, padding, heading size and back-link treatment |
| Colour | Raw Tailwind palette (`gray-500`, `amber-600`, `purple-100`), a `primary-*` ramp, a near-duplicate `notipus-*` ramp, and hardcoded hexes |

Two concrete consequences, beyond the visual inconsistency:

- **Contrast was unverifiable.** Gradient fills have no single background colour
  to measure, and colours chosen per-page were never checked. `text-primary-600`
  (`#e14a00`) on white is 4.05:1 — below the 4.5:1 that Google PageSpeed's
  Lighthouse `color-contrast` audit requires.
- **Duplication hid bugs.** `organization_settings.html.j2` was a 228-line
  unreferenced copy of `workspace_settings.html.j2`. `integrate_stripe.html.j2`
  used Jinja's `{{ loop.index }}`, which Django renders as empty, emitting
  `copyEvent('evt', )` — a JavaScript syntax error. `ti-star-filled` had never
  rendered, because the webfont ships no `-filled` variants.

## Decision

Adopt **Django Cotton** for components and a **generated design token** layer,
and make both enforceable by test rather than by convention.

### 1. Tokens are Python, CSS is generated

`app/core/design_tokens.py` is the single source of truth for colour, type,
radius, shadow, font and layout rhythm. `manage.py build_design_tokens` renders
it into `src/css/tokens.css`, which `src/css/main.css` imports into Tailwind's
`@theme` block.

Python was chosen over hand-written CSS because it lets the same declarations
drive three consumers: the stylesheet, the component library page, and the
contrast test. A token cannot exist without a documented use and a measured
contrast ratio.

### 2. Colour is semantic, and the palette is banned

Tokens name a role, not a hue: `surface`, `content-muted`, `border-control`,
`action`, `danger-surface`. Tailwind's stock palette (`bg-gray-100`,
`text-red-600`) is rejected by `tests/test_design_tokens.py` in both templates
and `ui.js`.

### 2a. The palette comes from the logo, not from Tailwind

The first version of this system kept Tailwind's slate neutrals. Sampling the
logo showed why that was wrong: its ink is `#04212a`, a deep **teal** at hue
194, and its orange is `#f57a1a` at hue 26. Tailwind slate sits at hue 210–222 —
a blue-grey. The brand orange was therefore sitting *on* someone else's
greyscale rather than inside its own palette, which is what made the interface
read as a generic admin template.

The neutrals are now derived from the logo's ink: canvas `#f5f8f8`, surface
white, muted `#e9f0f0`, content `#04212a`. The orange and the neutrals share a
complementary relationship that the logo already establishes.

Consequences worth recording:

- **Primary buttons are ink, not orange.** No orange can carry white text at
  4.5:1 — the true logo orange manages 2.73:1, and forcing it down to compliance
  produces the rust `#bb3700`, which does not look like the brand at all. So
  `--color-action` is the logo's ink (16.72:1 with white) and the orange does
  accent work instead: focus rings, active nav, meters, icon tints, the featured
  plan ribbon. The brand punctuates rather than shouts, and it is the *real*
  orange wherever it appears.
- **`--color-brand` (`#e8590c`) is graphic-only**, clearing the 3:1 non-text
  threshold. `--color-brand-bright` (`#f57a1a`) is the true logo value and only
  ever sits behind ink text (6.11:1) — the "Most popular" ribbon is its one
  appearance as a fill.
- **Warning is a tint-first family.** Any yellow dark enough to carry white text
  at 4.5:1 turns brown and reads as the brand orange. Warnings are therefore
  carried by `warning-surface` plus an icon, and the solid value is reserved for
  dots and meters, never buttons. The JS confirm dialog's "warning" variant uses
  the danger button for the same reason.
- **Info is teal, not blue.** A saturated blue was a fifth hue competing with the
  brand for attention while carrying the least meaning. Info now draws from the
  logo's own hue family (`#0d5e6e`), which harmonises instead of competing and is
  clearly distinct from the near-black action colour.

### 3. Components own their styling

Every reusable piece of UI is a Cotton component under
`app/core/templates/cotton/`. There is deliberately **no** `@layer components`
block in `main.css`: maintaining `.btn-primary` alongside `<c-button>` is what
produced two divergent systems in the first place.

### 4. Contrast is a test, not a review step

`CONTRAST_RULES` declares every foreground/background pairing the components
actually render — 52 of them — and asserts each against the WCAG 2.1 AA
threshold Lighthouse audits (4.5:1 body text, 3:1 large text). Non-text pairings
(control borders, status dots) are additionally held to SC 1.4.11's 3:1, which
Lighthouse does not check.

### 5. A component library page, in development only

`/ui/` renders every token and component with source snippets. It is gated on
`settings.UI_LIBRARY_ENABLED`, which is `DEBUG and not UI_LIBRARY=false`; the
URL is not registered and the view 404s when it is off, so no environment
variable can expose it in production.

It doubles as a smoke test: rendering one page exercises every component, so a
broken component fails in CI rather than on a customer's screen.

## Consequences

**Good**

- One way to draw each thing; a new page is composed, not styled.
- Contrast, token sync, icon names and prop hygiene are CI gates.
- The CSS bundle shrank (the `@layer components` block and two unused colour
  ramps went away).
- Fixed along the way: the `{{ loop.index }}` JS syntax error, the invisible
  `ti-star-filled` icon, the duplicated settings template, four duplicated flash
  message blocks, and a Hunter.io field that rendered a placeholder of bullet
  characters into a password input and submitted them as an API key.

**Costs**

- Cotton's template loader replaces `APP_DIRS`, and the cached loader is only
  enabled outside `DEBUG` so component edits still hot-reload.
- Cotton's default context is not isolated, which makes prop naming a real
  hazard — see below. Context isolation was left **off** deliberately: turning it
  on constructs a fresh `RequestContext` per component render, which would re-run
  the `workspace_role` context processor and its database query for every
  component on the page.
- Components must be `.html` (Cotton resolves `<c-name>` to `cotton/name.html`),
  so djlint runs twice, once per extension.

**The trap worth knowing about**

Cotton strips bare `<c-vars>` prop names from `{{ attrs }}` but never binds them.
With the non-isolated context, an unset bare prop resolves against whatever the
page has in scope — and Django's `{% block %}` tag puts a truthy `block` into the
context. Every `<c-button>` inside `{% block content %}` was therefore rendering
full-width. Declaring an explicit default (`block=""`) shadows the outer context;
a test enforces that every prop has one.

## Alternatives considered

- **Tidy the existing CSS component layer.** Cheaper, but leaves the layer and
  the templates as two places to change one thing, and does nothing about
  contrast.
- **A JS component framework.** The app is server-rendered with a little
  sprinkled JavaScript; a build step and hydration would be a large cost for
  markup reuse.
- **Tokens hand-written in CSS.** Simpler, but the contrast test and the library
  page would each need their own copy of the palette, and copies drift.

---
name: django-templates
description: >-
  Conventions and gotchas for Notipus's HTML templates and its Django Cotton
  component library. Read this before editing anything under
  app/core/templates — adding a page, a component, a form field, an
  integration/connect page, or otherwise touching markup. Covers the
  .j2-is-actually-Django trap, Cotton's prop-leak and dynamic-binding traps,
  design tokens, the single-line comment gotcha, not leaking secrets into
  inputs, and how to actually verify a template renders.
---

# Notipus HTML templates

Two kinds of file live under `app/core/templates/`:

| What | Extension | Where |
| --- | --- | --- |
| Pages | `.html.j2` | `core/`, `account/`, plus `404`/`500` at the root |
| Cotton components | `.html` | `cotton/` |
| allauth/admin overrides | `.html` | `account/`, `socialaccount/`, `admin/` |

Components **must** be `.html`: Cotton resolves `<c-name>` to `cotton/name.html`
and the extension is fixed in the library.

## Start from the component library

Before writing markup, look at what already exists:

```bash
DEBUG=True PYTHONPATH=app DJANGO_SETTINGS_MODULE=django_notipus.test_settings \
  uv run python app/manage.py runserver   # then open /ui/
```

`/ui/` renders every component and every design token, with source snippets. It
exists only when `settings.UI_LIBRARY_ENABLED` is on, which follows `DEBUG`.

Compose pages from components. A page should read as `<c-page>` wrapping
`<c-card>`s, not as a wall of utility classes.

For anything about colour, tokens or visual balance, read the **design-system**
skill instead — this file covers template mechanics. The short version follows.

## Colours come from tokens, never from Tailwind's palette

`bg-gray-100`, `text-red-600` and friends are **banned in templates and in
`app/static/js/ui.js`** — `tests/test_design_tokens.py` fails the build if one
appears. Use the semantic tokens instead:

- surfaces: `bg-canvas`, `bg-surface`, `bg-surface-muted`, `bg-surface-inverse`
- text: `text-content`, `text-content-muted`, `text-content-subtle`, `text-content-inverse`
- lines: `border-border`, `border-border-strong`, `border-border-control`
- primary action: `bg-action`, `hover:bg-action-hover` (always with `text-content-inverse`)
- brand: `bg-brand` (graphics), `bg-brand-bright` (fills under ink text),
  `text-brand-text`, `bg-brand-surface`, `border-brand-border`
- status: `{success,warning,danger,info}-{solid,text,surface,border}`
- rhythm: `p-gutter`, `gap-stack`, `rounded-control`, `rounded-card`, `text-body`, `text-title`

Tokens are defined once in `app/core/design_tokens.py` and generated into
`src/css/tokens.css`:

```bash
uv run python app/manage.py build_design_tokens          # regenerate
uv run python app/manage.py build_design_tokens --check  # CI gate
```

Adding or changing a colour means adding a `ContrastRule` for every pairing it
creates. The test suite enforces WCAG AA — 4.5:1 body text, 3:1 large text and
non-text — which is exactly what Google PageSpeed's Lighthouse audit checks.

The palette is derived from the logo, not from Tailwind: the neutrals carry a
trace of the logo's teal ink (`#04212a`) and the orange is the logo's own.

**Primary buttons are ink, not orange.** No orange can carry white text — the
logo orange is 2.73:1 with white — so `action` is the ink and the orange does
accent work: focus rings, active nav, meters, icon tints, the featured plan
ribbon. `--color-brand` is graphic-only; `--color-brand-bright` is the true logo
orange and only ever sits behind ink text.

## `.html.j2` is Django Template Language, NOT Jinja2

Despite the extension, these are rendered by the **Django template backend**
(see `TEMPLATES` in `app/django_notipus/settings.py`). Jinja2 syntax does not
work: `{% macro %}`, `{{ x if y else z }}`, `{%- -%}` whitespace control, and
`{{ loop.index }}` all fail or silently render nothing. Django's loop counter is
`{{ forloop.counter }}` — a `{{ loop.index }}` left in `integrate_stripe` used
to emit `copyEvent('evt', )`, a JavaScript syntax error.

## Cotton traps

### Every `<c-vars>` prop needs a default

Cotton strips bare prop names from `{{ attrs }}` but never binds them, and its
default context is **not** isolated. An unset bare prop therefore resolves
against whatever the page has in scope. Django's `{% block %}` tag puts a truthy
`block` into the context, which silently made every `<c-button>` inside
`{% block content %}` full-width.

```django
<c-vars variant="primary" href="" icon="" block="" class="" />   {# right #}
<c-vars variant="primary" href icon block class />               {# leaks #}
```

`tests/test_design_tokens.py::test_components_bind_every_declared_prop` enforces this.

### `:prop="…"` resolves plain variables only

Cotton's dynamic binding does a `Variable` lookup then a literal eval. Filters
and expressions are not supported and fail **silently** — the attribute is
simply dropped:

```django
:for="id|default:name"              {# broken: renders for="" #}
:checked="key in enabled_categories" {# broken: always falsy #}

for="{{ id|default:name }}"                          {# right #}
checked="{% if key in enabled_categories %}1{% endif %}"  {# right #}
```

A quoted value containing `{{ }}` or `{% %}` is compiled as an inline template,
which is the escape hatch for anything more than a variable path.

### djlint reindents slot content

Anything whitespace-sensitive must go through a prop rather than the slot.
`<c-code-block>` takes `value=` for exactly this reason — a webhook URL passed
through the slot picks up stray newlines on the next `djlint --reformat`.

## Comments: `{# #}` is single-line only

Django's `{# ... #}` inline comment must be on **one line**. A multi-line
`{# ... #}` is **not** treated as a comment — Django renders it as literal text
on the page. (This shipped once: a multi-line `{# #}` note above a form field
printed verbatim in the UI.)

```django
{# ok: single-line note #}

{% comment %}
Multi-line notes MUST use comment/endcomment, or they render on the page.
{% endcomment %}
```

Avoid angle brackets inside comments in component files — djlint reformats
anything that looks like markup, even in a comment, and will break the line up.

Never put a `{% comment %}` block **inside an element's attribute list**. djlint
reflows it to one word per line. Explain the attribute in the component's
doc comment at the top of the file instead.

## Test props for presence, not truthiness

Cotton binds `:value="count"` as an int, and the unset default is `""`, so a
plain `{% if value %}` silently drops a legitimate `0`:

```django
{% if value %}value="{{ value }}"{% endif %}                        {# drops 0 #}
{% if value is not None and value != "" %}value="{{ value }}"{% endif %}  {# right #}
```

This bit `<c-input>`, `<c-checkbox>`, `<c-code-block>` and `<c-definition.row>`
at once. `tests/test_ui_library.py::TestZeroValueRegression` covers them.

## Never render secrets back into inputs

Do not echo a stored secret (API key, bot token, webhook secret) into an
input's `value=` — even a `type="password"` field puts it in the DOM/page
source where an extension or XSS can read it. `<c-input secret>` handles this:
it renders an empty password field with `autocomplete="off"` regardless of what
you pass. Non-secret identifiers (e.g. a chat/channel id) are fine to prefill.

## Icons

One library: Tabler, via `<c-icon name="…">`, `<c-icon-tile>`, or an
`icon="…"` prop. No inline SVG except provider brand marks, which all live in
`cotton/provider_logo.html`.

The webfont ships **no `-filled` variants**; `ti-star-filled` rendered as blank
space for months. `tests/test_design_tokens.py::TestIconNames` checks every name
against the installed webfont, so a typo fails the build instead of vanishing.

## Other conventions

- **Headings:** `<c-page title>` renders the single `<h1>`; card titles are `<h3>`.
- **External links:** `target="_blank"` links carry `rel="noopener noreferrer"`.
- **Brand accents:** provider hexes (Stripe `#635bff`, Shopify `#95BF47`,
  Telegram `#0088cc`) belong to those companies, so they stay hardcoded in
  `cotton/provider_logo.html` rather than becoming tokens.
- **Confirmations:** destructive forms use
  `onsubmit="return NotipusUI.confirmSubmit(this, {…})"`, never the browser's
  `confirm()`.

## Verifying a template — djlint is necessary but NOT sufficient

The CI gate runs djlint over both extensions:

```bash
uv run djlint app/core/templates --extension=html.j2 --check
uv run djlint app/core/templates --extension=html.j2 --lint
uv run djlint app/core/templates --extension=html --check
uv run djlint app/core/templates --extension=html --lint
```

djlint catches formatting and HTML issues, but it does **not** evaluate template
semantics — a malformed multi-line `{# #}`, a dropped `:prop` binding, or a
missing icon all pass djlint clean. For anything subtle add a render test that
GETs the page through Django's test client and asserts on the response body. See
`tests/test_telegram_connect_page.py` for the pattern, and `tests/test_ui_library.py`,
which renders every component at once by loading `/ui/`.

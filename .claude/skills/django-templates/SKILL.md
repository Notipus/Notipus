---
name: django-templates
description: >-
  Conventions and gotchas for Notipus's HTML templates. Read this before
  editing anything under app/core/templates (the *.html.j2 files) — adding a
  template comment, a form field, an integration/connect page, or otherwise
  touching markup. Covers the .j2-is-actually-Django trap, the single-line
  comment gotcha, not leaking secrets into inputs, and how to actually verify
  a template renders.
---

# Notipus HTML templates

Templates live in `app/core/templates/**` and use the **`.html.j2`** extension.

## `.html.j2` is Django Template Language, NOT Jinja2

Despite the extension, these are rendered by the **Django template backend**
(`django.template.backends.django.DjangoTemplates` — see `TEMPLATES` in
`app/django_notipus/settings.py`). Tells throughout the tree: `{% load static %}`,
`{% url '...' %}`, `{{ user.get_short_name|default:user.username|title }}`. Jinja2
syntax (`{% macro %}`, `{{ x if y else z }}`, multi-line `{# #}`) does **not**
work here. When in doubt, use the [Django template language](https://docs.djangoproject.com/en/stable/ref/templates/language/), not Jinja2.

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

Use `{% comment %} … {% endcomment %}` for anything spanning more than one line.

## Never render secrets back into inputs

Do not echo a stored secret (API key, bot token, webhook secret) into an
input's `value=` — even a `type="password"` field puts it in the DOM/page
source where an extension or XSS can read it. Leave the field empty and ask the
user to re-enter to update; add `autocomplete="off"`. Non-secret identifiers
(e.g. a chat/channel id) are fine to prefill.

## Other conventions

- **Headings:** a page's primary heading is a single `<h1>` (the integrate/
  connect pages open with one); card/section titles are `<h3>`/`<h4>`.
- **External links:** `target="_blank"` links carry `rel="noopener noreferrer"`.
- **Brand accents:** integration pages hardcode the provider's brand hex
  (Stripe `#635bff`, Shopify `#95BF47`, Telegram `#0088cc`); elsewhere use the
  Tailwind `primary-*` palette.

## Verifying a template — djlint is necessary but NOT sufficient

The CI gate runs djlint:

```bash
uv run djlint app/core/templates --check   # formatting
uv run djlint app/core/templates --lint     # HTML lint
```

djlint catches formatting and HTML issues, but it does **not** evaluate template
semantics — a malformed multi-line `{# #}` passes djlint clean and still renders
on the page. For anything subtle (comments, conditional output, secret handling)
add a render test that GETs the page through Django's test client and asserts on
the response body — that's what would have caught the comment leak. See
`tests/test_telegram_connect_page.py` for the pattern.

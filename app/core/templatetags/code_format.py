"""Filters for rendering literal markup inside the component library."""

import textwrap

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

register = template.Library()


@register.filter
def code_sample(value: str) -> SafeString:
    """Escape a block of literal markup and strip its surrounding indentation.

    The component library shows each component's own source next to the rendered
    example. That source arrives from a Cotton slot, so it carries whatever
    indentation the template had — and djlint re-indents it on every reformat,
    which left the samples wandering across the page inside their <pre>.

    Args:
        value: Rendered slot content holding literal markup.

    Returns:
        The sample escaped for display and dedented to a flush left edge.
    """
    lines = str(value).strip("\n").splitlines()
    if not lines:
        return mark_safe("")

    # The first line is flush against the opening tag, so it has no indentation
    # to contribute; measure the block from the remaining lines and give the
    # first one the same treatment.
    body = textwrap.dedent("\n".join(lines[1:])) if len(lines) > 1 else ""
    sample = "\n".join(filter(None, [lines[0].strip(), body])).rstrip()
    return mark_safe(escape(sample))

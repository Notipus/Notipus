"""The developer-only component library.

Renders every design token and every Cotton component on one page so the system
can be reviewed as a whole rather than a screen at a time. It is not part of the
product: ``settings.UI_LIBRARY_ENABLED`` follows ``DEBUG``, and the view 404s
when it is off, so the page cannot appear in production even if something routes
to it.
"""

from typing import Any

from core.design_tokens import CONTRAST_RULES, TOKEN_GROUPS
from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

# Every icon the components use, so the page doubles as a check that a Tabler
# name still resolves after a webfont upgrade.
SAMPLE_ICONS: tuple[str, ...] = (
    "home",
    "plug",
    "plug-connected",
    "credit-card",
    "users",
    "settings",
    "bolt",
    "rocket",
    "sparkles",
    "circle-check",
    "alert-triangle",
    "alert-circle",
    "info-circle",
    "clock",
    "copy",
    "send",
    "trash",
    "unlink",
    "chevron-right",
    "arrow-left",
    "external-link",
    "loader-2",
    "fingerprint",
    "mail",
)


def ui_library(request: HttpRequest) -> HttpResponse:
    """Render the component library.

    Args:
        request: The current HTTP request.

    Returns:
        The rendered library page.

    Raises:
        Http404: When ``UI_LIBRARY_ENABLED`` is off, which includes every
            non-DEBUG deployment.
    """
    if not settings.UI_LIBRARY_ENABLED:
        raise Http404("The component library is only available in development.")

    context: dict[str, Any] = {
        "token_groups": TOKEN_GROUPS,
        "contrast_rules": CONTRAST_RULES,
        "contrast_failures": [rule for rule in CONTRAST_RULES if not rule.passes],
        "sample_icons": SAMPLE_ICONS,
    }
    return render(request, "core/ui_library.html.j2", context)

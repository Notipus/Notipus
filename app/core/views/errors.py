"""Custom error handlers for the application.

These handlers render custom error templates to provide a better user experience
and avoid issues with Django's default debug error templates.
"""

import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

logger = logging.getLogger(__name__)

# Served when the styled 500 page itself cannot render. It must not touch the
# database, the cache, the template engine or static files: the error that got
# us here may be any of those being down.
STATIC_500_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Server error - Notipus</title>
</head>
<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto;
padding: 0 1rem; color: #04212a;">
<p style="font-family: monospace; text-transform: uppercase;">Error 500</p>
<h1>Server error</h1>
<p>Something broke on our side and we've been told about it automatically.
Reloading often works &mdash; and your webhooks keep being received either way.</p>
<p><a href="/">Go to the home page</a></p>
</body>
</html>
"""


def custom_404(request: HttpRequest, exception: Exception) -> HttpResponse:
    """Render custom 404 page.

    Args:
        request: The HTTP request that resulted in 404.
        exception: The exception that triggered the 404.

    Returns:
        HttpResponse with 404 status code.
    """
    return render(request, "404.html.j2", status=404)


def custom_500(request: HttpRequest) -> HttpResponse:
    """Render custom 500 page.

    Args:
        request: The HTTP request that resulted in 500.

    The styled page extends base.html.j2, whose context processors and nav
    query the database for the signed-in user. When the 500 was caused by
    the database being unreachable, that render raises as well and the
    visitor gets the server's bare error instead. So the styled page is
    tried once and replaced by a self-contained static page on any error.

    Returns:
        HttpResponse with 500 status code.
    """
    try:
        return render(request, "500.html.j2", status=500)
    except Exception:
        logger.exception("Rendering the 500 page failed; serving the static one")
        return HttpResponse(STATIC_500_HTML, status=500)

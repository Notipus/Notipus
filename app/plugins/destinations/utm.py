"""Tag outbound notification links with a Notipus UTM source.

Every link Notipus posts into a Slack, Telegram, or Teams channel
carries ``utm_source=notipus``, so sites on the receiving end attribute
the visit to Notipus instead of filing it under direct traffic.

Shared by all three destination plugins so a link is tagged the same way
no matter which channel it goes out through.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Value used for ``utm_source`` on every link we post.
UTM_SOURCE = "notipus"

#: Where the attribution link in the notification footer points.
ATTRIBUTION_URL = "https://notipus.com/"

#: Label for that link.
ATTRIBUTION_LABEL = "Powered by Notipus"

_UTM_SOURCE_PARAM = "utm_source"

# mailto: and tel: links carry no query string, so tagging them would
# corrupt the address rather than annotate it.
_TAGGABLE_SCHEMES = frozenset({"http", "https"})


def tag_url(url: str | None) -> str | None:
    """Return ``url`` with ``utm_source=notipus`` appended.

    Args:
        url: URL to tag. May be None.

    Returns:
        The tagged URL, or ``url`` unchanged when it is empty, when the
        scheme is not http(s), or when it already carries a
        ``utm_source`` - a link that already names its source is not
        ours to relabel. Existing query parameters and fragments are
        preserved.
    """
    if not url:
        return url

    try:
        parts = urlsplit(url)
    except ValueError:
        # Malformed enough that urlsplit rejects it; leave it alone and
        # let the caller's own sanitizer decide whether to drop it.
        return url

    if parts.scheme.lower() not in _TAGGABLE_SCHEMES:
        return url

    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key.lower() == _UTM_SOURCE_PARAM for key, _ in query):
        return url

    query.append((_UTM_SOURCE_PARAM, UTM_SOURCE))
    return urlunsplit(parts._replace(query=urlencode(query)))


def attribution_url() -> str:
    """Return the tagged URL for the footer attribution link.

    Returns:
        ``ATTRIBUTION_URL`` carrying ``utm_source=notipus``.
    """
    tagged = tag_url(ATTRIBUTION_URL)
    # tag_url only returns None for a falsy input, which ATTRIBUTION_URL
    # is not; assert it for the type checker.
    assert tagged is not None
    return tagged

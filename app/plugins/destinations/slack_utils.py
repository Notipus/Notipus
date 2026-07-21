"""Utility functions for Slack destination plugin.

This module provides helper functions for formatting content for Slack,
including HTML to mrkdwn conversion.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

# Maximum output length to prevent abuse
MAX_OUTPUT_LENGTH = 2000


class HTMLToSlackMrkdwnParser(HTMLParser):
    """HTML parser that converts HTML to Slack mrkdwn format.

    Converts common HTML tags to their Slack mrkdwn equivalents:
    - <a href="url">text</a> -> <url|text>
    - <strong>/<b> -> *text*
    - <em>/<i> -> _text_
    - <p> -> newlines
    - <br> -> newline
    - Other tags are stripped, content preserved
    """

    def __init__(self) -> None:
        """Initialize the parser."""
        super().__init__()
        self.result: list[str] = []
        self.tag_stack: list[str] = []
        self.current_link_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle opening tags.

        Args:
            tag: The tag name (lowercase).
            attrs: List of (name, value) tuples for attributes.
        """
        tag = tag.lower()
        self.tag_stack.append(tag)

        if tag == "a":
            # Extract and validate href
            href = None
            for name, value in attrs:
                if name == "href" and value:
                    href = value
                    break
            self.current_link_url = _sanitize_url(href) if href else None
        elif tag in ("strong", "b"):
            self.result.append("*")
        elif tag in ("em", "i"):
            self.result.append("_")
        elif tag == "p":
            # Add newline before paragraph if there's existing content
            if self.result and self.result[-1] not in ("\n", ""):
                self.result.append("\n")
        elif tag == "br":
            self.result.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Handle closing tags.

        Args:
            tag: The tag name (lowercase).
        """
        tag = tag.lower()

        # Pop from stack if matching
        if self.tag_stack and self.tag_stack[-1] == tag:
            self.tag_stack.pop()

        if tag == "a":
            self.current_link_url = None
        elif tag in ("strong", "b"):
            self.result.append("*")
        elif tag in ("em", "i"):
            self.result.append("_")
        elif tag == "p":
            self.result.append("\n")

    def handle_data(self, data: str) -> None:
        """Handle text content.

        Args:
            data: The text content.
        """
        # Normalize non-breaking spaces to regular spaces
        data = data.replace("\xa0", " ")
        # Clean control characters
        data = _clean_control_characters(data)

        if self.current_link_url:
            # Inside an <a> tag - format as Slack link
            escaped_text = _escape_slack_link_text(data)
            self.result.append(f"<{self.current_link_url}|{escaped_text}>")
            self.current_link_url = None  # Consume the URL
        else:
            # Regular text - escape special characters
            self.result.append(_escape_slack_mrkdwn(data))

    def handle_entityref(self, name: str) -> None:
        """Handle named character references like &amp;.

        Args:
            name: Entity name without the & and ;.
        """
        entity_map = {
            "amp": "&",
            "lt": "<",
            "gt": ">",
            "quot": '"',
            "nbsp": " ",
        }
        char = entity_map.get(name, f"&{name};")
        self.handle_data(char)

    def handle_charref(self, name: str) -> None:
        """Handle numeric character references like &#60;.

        Args:
            name: Character code (decimal or hex with x prefix).
        """
        try:
            if name.startswith(("x", "X")):
                char = chr(int(name[1:], 16))
            else:
                char = chr(int(name))
            self.handle_data(char)
        except (ValueError, OverflowError):
            # Invalid character reference, skip
            pass

    def get_result(self) -> str:
        """Get the converted mrkdwn string.

        Returns:
            The converted Slack mrkdwn string.
        """
        return "".join(self.result)


def _sanitize_url(url: str) -> str | None:
    """Sanitize a URL for use in Slack links.

    Only allows http and https schemes to prevent XSS attacks.

    Args:
        url: The URL to sanitize.

    Returns:
        The sanitized URL, or None if the URL is invalid/dangerous.
    """
    if not url:
        return None

    url = url.strip()

    # Remove null bytes and control characters
    url = _clean_control_characters(url)

    try:
        parsed = urlparse(url)
        # Only allow http and https schemes
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        # Reconstruct URL to ensure it's properly formatted
        return url
    except Exception:
        return None


def _clean_control_characters(text: str) -> str:
    """Remove null bytes and control characters from text.

    Args:
        text: The text to clean.

    Returns:
        The cleaned text.
    """
    # Remove null bytes and ASCII control characters (except newline and tab)
    return "".join(
        char
        for char in text
        if char in ("\n", "\t") or (ord(char) >= 32 and char != "\x7f")
    )


def _escape_slack_mrkdwn(text: str) -> str:
    """Escape special Slack mrkdwn characters in plain text.

    Slack requires escaping <, >, and & characters.

    Args:
        text: The text to escape.

    Returns:
        The escaped text safe for Slack mrkdwn.
    """
    # Order matters: escape & first since it's used in other escapes
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


# Patterns for Slack special syntax that could be injected
# These patterns match Slack's special link/mention syntax
_SLACK_INJECTION_PATTERNS = [
    # Broadcast mentions: <!channel>, <!here>, <!everyone>
    (re.compile(r"<!(channel|here|everyone)(\|[^>]*)?>", re.IGNORECASE), r"@\1"),
    # User mentions: <@U123ABC>
    (re.compile(r"<@[UW][A-Z0-9]+(\|[^>]*)?>", re.IGNORECASE), "@user"),
    # Channel mentions: <#C123ABC>
    (re.compile(r"<#[C][A-Z0-9]+(\|[^>]*)?>", re.IGNORECASE), "#channel"),
    # Subteam mentions: <!subteam^S123|@team>
    (re.compile(r"<!subteam\^[A-Z0-9]+(\|[^>]*)?>", re.IGNORECASE), "@team"),
    # Date formatting: <!date^...>
    (re.compile(r"<!date\^[^>]+>", re.IGNORECASE), "[date]"),
]


def _sanitize_slack_injection(text: str) -> str:
    """Remove Slack-specific injection patterns from text.

    Protects against malicious content that could:
    - Trigger @channel/@here/@everyone mentions (spam entire channels)
    - Mention specific users (<@U123>)
    - Reference channels (<#C123>)
    - Use other Slack special syntax

    This is defense-in-depth for content from untrusted sources like
    third-party APIs (e.g., Brandfetch company descriptions).

    Args:
        text: Text that may contain Slack injection patterns.

    Returns:
        Sanitized text with injection patterns neutralized.
    """
    for pattern, replacement in _SLACK_INJECTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_mrkdwn(text: str | None) -> str:
    """Make an untrusted plain-text string safe for a Slack mrkdwn block.

    Webhook payloads (customer names, order numbers, plan names, line-item
    names, ...) are attacker-controllable and must never be interpolated
    raw into a Slack mrkdwn block. Otherwise an attacker could inject a
    broadcast mention (``<!channel>``) to spam a channel or a fake link
    (``<https://evil/login|Update billing>``) to phish.

    Slack special syntax is neutralized first so mentions render as
    readable text (e.g. ``@channel``), then the mrkdwn control characters
    (``<``, ``>``, ``&``) are escaped so any remaining angle brackets can
    no longer form links or mentions.

    This is intended for plain, non-HTML strings. Do not apply it to
    values already produced by :func:`html_to_slack_mrkdwn` (e.g. company
    descriptions), which are sanitized and escaped there; doing so would
    double-escape the ``&``/``<``/``>`` characters.

    Args:
        text: The untrusted text. May be None.

    Returns:
        Text safe to embed in a Slack mrkdwn block. Empty string for
        None/empty input.
    """
    if not text:
        return ""
    text = _clean_control_characters(text)
    text = _sanitize_slack_injection(text)
    return _escape_slack_mrkdwn(text)


def safe_mrkdwn_link(url: str | None, label: str) -> str | None:
    """Build a Slack ``<url|label>`` mrkdwn link from an untrusted URL.

    Enrichment sources (Brandfetch, Hunter.io) and webhook payloads
    supply the URLs shown in notifications; a crafted value containing
    ``<``, ``>``, ``|``, or whitespace could break out of the link
    syntax and inject arbitrary mrkdwn (fake links, mentions). Those
    characters are invalid in a properly-encoded URL, so URLs carrying
    them are rejected rather than repaired. Only http/https schemes are
    allowed, and the label is escaped for the link-text position.

    Args:
        url: The untrusted URL. May be None.
        label: Human-readable link text.

    Returns:
        A safe ``<url|label>`` string, or None when the URL is missing
        or unusable.
    """
    if not url:
        return None
    url = _clean_control_characters(url.strip())
    if _sanitize_url(url) is None:
        return None
    if any(c in url for c in "<>|") or any(c.isspace() for c in url):
        return None
    return f"<{url}|{_escape_slack_link_text(label)}>"


def _escape_slack_link_text(text: str) -> str:
    """Escape text for use inside Slack link display text.

    Inside Slack links <url|text>, the pipe character must be avoided.

    Args:
        text: The text to escape.

    Returns:
        The escaped text safe for Slack link display.
    """
    # Escape basic mrkdwn characters first
    text = _escape_slack_mrkdwn(text)
    # Replace pipe character which would break the link syntax
    text = text.replace("|", "-")
    return text


def _fallback_strip_tags(html: str) -> str:
    """Fallback function to strip HTML tags using regex.

    Used when the HTML parser fails due to malformed HTML.

    Args:
        html: The HTML string to strip.

    Returns:
        Plain text with HTML tags removed.
    """
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", html)
    # Decode common HTML entities
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&nbsp;", " ")
    # Clean and escape for Slack
    text = _clean_control_characters(text)
    text = _escape_slack_mrkdwn(text)
    return text


def html_to_slack_mrkdwn(html: str | None, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """Convert HTML to Slack mrkdwn format.

    Converts common HTML formatting to Slack's mrkdwn syntax:
    - Links: <a href="url">text</a> -> <url|text>
    - Bold: <strong>/<b> -> *text*
    - Italic: <em>/<i> -> _text_
    - Paragraphs: <p> -> newlines
    - Line breaks: <br> -> newline
    - Other tags are stripped, content preserved

    Includes security measures:
    - Only http/https URLs are allowed in links
    - Control characters are removed
    - Slack special characters (<, >, &) are escaped
    - Slack injection patterns (<!channel>, <@user>, etc.) are neutralized
    - Output is truncated to max_length

    Args:
        html: The HTML string to convert. Can be None.
        max_length: Maximum length of output (default 2000).

    Returns:
        Slack mrkdwn formatted string. Empty string if input is None/empty.
    """
    if not html:
        return ""

    # Clean control characters from input
    html = _clean_control_characters(html)

    try:
        parser = HTMLToSlackMrkdwnParser()
        parser.feed(html)
        result = parser.get_result()
    except Exception:
        # Fallback to regex stripping on any parse error
        result = _fallback_strip_tags(html)

    # Clean up multiple consecutive newlines
    result = re.sub(r"\n{3,}", "\n\n", result)

    # Strip leading/trailing whitespace
    result = result.strip()

    # Defense-in-depth: sanitize any Slack injection patterns that might
    # have gotten through (e.g., from malicious Brandfetch data)
    result = _sanitize_slack_injection(result)

    # Truncate if needed
    if len(result) > max_length:
        result = result[:max_length]

    return result

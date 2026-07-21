"""Tests for Slack utility functions.

This module tests the html_to_slack_mrkdwn function that converts
HTML content to Slack's mrkdwn format.
"""

from plugins.destinations.slack_utils import (
    _clean_control_characters,
    _escape_slack_mrkdwn,
    _sanitize_slack_injection,
    _sanitize_url,
    html_to_slack_mrkdwn,
    safe_mrkdwn_link,
)


class TestHtmlToSlackMrkdwnBasic:
    """Test basic HTML to mrkdwn conversion."""

    def test_empty_input(self) -> None:
        """Test empty string returns empty string."""
        assert html_to_slack_mrkdwn("") == ""

    def test_none_input(self) -> None:
        """Test None input returns empty string."""
        assert html_to_slack_mrkdwn(None) == ""

    def test_plain_text_passthrough(self) -> None:
        """Test plain text without HTML passes through."""
        text = "Hello world"
        assert html_to_slack_mrkdwn(text) == "Hello world"

    def test_whitespace_only(self) -> None:
        """Test whitespace-only input is stripped."""
        assert html_to_slack_mrkdwn("   ") == ""
        assert html_to_slack_mrkdwn("\n\n") == ""


class TestHtmlToSlackMrkdwnLinks:
    """Test link conversion from <a> tags."""

    def test_simple_link(self) -> None:
        """Test basic <a> tag conversion."""
        html = '<a href="https://example.com">Example</a>'
        result = html_to_slack_mrkdwn(html)
        assert result == "<https://example.com|Example>"

    def test_link_with_surrounding_text(self) -> None:
        """Test link with text before and after."""
        html = 'Visit <a href="https://example.com">our site</a> today.'
        result = html_to_slack_mrkdwn(html)
        assert result == "Visit <https://example.com|our site> today."

    def test_multiple_links(self) -> None:
        """Test multiple links in one string."""
        html = '<a href="https://a.com">A</a> and <a href="https://b.com">B</a>'
        result = html_to_slack_mrkdwn(html)
        assert "<https://a.com|A>" in result
        assert "<https://b.com|B>" in result

    def test_link_without_href(self) -> None:
        """Test <a> tag without href attribute."""
        html = "<a>Just text</a>"
        result = html_to_slack_mrkdwn(html)
        assert result == "Just text"

    def test_link_with_empty_href(self) -> None:
        """Test <a> tag with empty href."""
        html = '<a href="">Click here</a>'
        result = html_to_slack_mrkdwn(html)
        assert result == "Click here"

    def test_link_with_http_scheme(self) -> None:
        """Test link with http:// scheme is allowed."""
        html = '<a href="http://example.com">Example</a>'
        result = html_to_slack_mrkdwn(html)
        assert result == "<http://example.com|Example>"


class TestHtmlToSlackMrkdwnBoldItalic:
    """Test bold and italic formatting."""

    def test_strong_tag(self) -> None:
        """Test <strong> converts to *bold*."""
        html = "<strong>important</strong>"
        result = html_to_slack_mrkdwn(html)
        assert result == "*important*"

    def test_b_tag(self) -> None:
        """Test <b> converts to *bold*."""
        html = "<b>bold text</b>"
        result = html_to_slack_mrkdwn(html)
        assert result == "*bold text*"

    def test_em_tag(self) -> None:
        """Test <em> converts to _italic_."""
        html = "<em>emphasized</em>"
        result = html_to_slack_mrkdwn(html)
        assert result == "_emphasized_"

    def test_i_tag(self) -> None:
        """Test <i> converts to _italic_."""
        html = "<i>italic text</i>"
        result = html_to_slack_mrkdwn(html)
        assert result == "_italic text_"

    def test_nested_bold_italic(self) -> None:
        """Test nested bold and italic."""
        html = "<strong><em>bold and italic</em></strong>"
        result = html_to_slack_mrkdwn(html)
        assert "*" in result
        assert "_" in result


class TestHtmlToSlackMrkdwnParagraphs:
    """Test paragraph and line break handling."""

    def test_paragraph_tags(self) -> None:
        """Test <p> tags create newlines."""
        html = "<p>First paragraph.</p><p>Second paragraph.</p>"
        result = html_to_slack_mrkdwn(html)
        assert "First paragraph." in result
        assert "Second paragraph." in result
        assert "\n" in result

    def test_br_tag(self) -> None:
        """Test <br> creates newline."""
        html = "Line one<br>Line two"
        result = html_to_slack_mrkdwn(html)
        assert "Line one\nLine two" == result

    def test_br_self_closing(self) -> None:
        """Test self-closing <br /> tag."""
        html = "Line one<br />Line two"
        result = html_to_slack_mrkdwn(html)
        assert "Line one\nLine two" == result

    def test_multiple_consecutive_newlines_collapsed(self) -> None:
        """Test multiple consecutive newlines are collapsed."""
        html = "<p>One</p><p></p><p></p><p>Two</p>"
        result = html_to_slack_mrkdwn(html)
        # Should not have more than 2 consecutive newlines
        assert "\n\n\n" not in result


class TestHtmlToSlackMrkdwnUnknownTags:
    """Test handling of unknown/unsupported tags."""

    def test_unknown_tags_stripped(self) -> None:
        """Test unknown tags are stripped but content preserved."""
        html = "<div>content inside div</div>"
        result = html_to_slack_mrkdwn(html)
        assert result == "content inside div"

    def test_span_stripped(self) -> None:
        """Test <span> is stripped but content preserved."""
        html = "Hello <span class='highlight'>world</span>!"
        result = html_to_slack_mrkdwn(html)
        assert result == "Hello world!"

    def test_nested_unknown_tags(self) -> None:
        """Test nested unknown tags."""
        html = "<div><span>nested content</span></div>"
        result = html_to_slack_mrkdwn(html)
        assert result == "nested content"


class TestHtmlToSlackMrkdwnHtmlEntities:
    """Test HTML entity handling."""

    def test_amp_entity(self) -> None:
        """Test &amp; is decoded and re-escaped."""
        html = "Tom &amp; Jerry"
        result = html_to_slack_mrkdwn(html)
        # & should be escaped for Slack
        assert "&amp;" in result

    def test_lt_gt_entities(self) -> None:
        """Test &lt; and &gt; are decoded and re-escaped."""
        html = "&lt;code&gt;"
        result = html_to_slack_mrkdwn(html)
        # Should be escaped for Slack
        assert "&lt;" in result
        assert "&gt;" in result

    def test_nbsp_entity(self) -> None:
        """Test &nbsp; is converted to space."""
        html = "word&nbsp;word"
        result = html_to_slack_mrkdwn(html)
        # Our implementation converts nbsp to regular space
        assert result == "word word"

    def test_numeric_entity(self) -> None:
        """Test numeric character references."""
        html = "&#60;test&#62;"  # <test>
        result = html_to_slack_mrkdwn(html)
        assert "&lt;test&gt;" == result


class TestHtmlToSlackMrkdwnSecurity:
    """Test security-related handling."""

    def test_brandfetch_injection_channel_mention(self) -> None:
        """Test Brandfetch-style attack with <!channel> injection."""
        # Attacker registers company with malicious description
        html = "<p>EvilCorp makes great products! <!channel> Check us out!</p>"
        result = html_to_slack_mrkdwn(html)
        # The <!channel> should be escaped (< becomes &lt;) or neutralized
        assert "<!channel>" not in result
        # Content should still be present
        assert "EvilCorp" in result

    def test_brandfetch_injection_user_mention(self) -> None:
        """Test Brandfetch-style attack with user mention injection."""
        html = "<p>Contact <@U123ABC> for sales!</p>"
        result = html_to_slack_mrkdwn(html)
        # The <@U123ABC> should be escaped or neutralized
        assert "<@U123ABC>" not in result

    def test_brandfetch_injection_mixed_attack(self) -> None:
        """Test Brandfetch-style attack with multiple injection vectors."""
        html = (
            "<p><!everyone> URGENT: Visit "
            '<a href="https://evil.com">https://stripe.com</a> '
            "to verify your account! <@U123> <#C456></p>"
        )
        result = html_to_slack_mrkdwn(html)
        # All injection patterns should be neutralized
        assert "<!everyone>" not in result
        assert "<@U123>" not in result
        assert "<#C456>" not in result
        # Legitimate link should be preserved (but with actual URL, not spoofed text)
        assert "https://evil.com" in result

    def test_javascript_url_blocked(self) -> None:
        """Test javascript: URLs are blocked."""
        html = '<a href="javascript:alert(1)">Click me</a>'
        result = html_to_slack_mrkdwn(html)
        # Link should not contain javascript:
        assert "javascript:" not in result
        # Text should still be present
        assert "Click me" in result

    def test_javascript_url_case_insensitive(self) -> None:
        """Test javascript: URL blocking is case insensitive."""
        html = '<a href="JAVASCRIPT:alert(1)">Click</a>'
        result = html_to_slack_mrkdwn(html)
        assert "javascript" not in result.lower() or "JAVASCRIPT" not in result

    def test_data_url_blocked(self) -> None:
        """Test data: URLs are blocked."""
        html = '<a href="data:text/html,<script>alert(1)</script>">Click</a>'
        result = html_to_slack_mrkdwn(html)
        assert "data:" not in result

    def test_vbscript_url_blocked(self) -> None:
        """Test vbscript: URLs are blocked."""
        html = '<a href="vbscript:msgbox(1)">Click</a>'
        result = html_to_slack_mrkdwn(html)
        assert "vbscript:" not in result

    def test_special_chars_escaped_in_text_content(self) -> None:
        """Test Slack special characters are escaped in text content."""
        # When text with < and > is inside tags, it gets escaped
        html = "<p>2 &lt; 3 and 5 &gt; 4</p>"
        result = html_to_slack_mrkdwn(html)
        # < and > should be escaped for Slack
        assert "&lt;" in result
        assert "&gt;" in result

    def test_ampersand_escaped(self) -> None:
        """Test ampersand is escaped in text content."""
        # Use proper HTML entity encoding for the input
        html = "<p>AT&amp;T</p>"
        result = html_to_slack_mrkdwn(html)
        assert "AT&amp;T" == result

    def test_null_bytes_removed(self) -> None:
        """Test null bytes are removed from input."""
        html = "hello\x00world"
        result = html_to_slack_mrkdwn(html)
        assert "\x00" not in result
        assert "helloworld" == result

    def test_control_characters_removed(self) -> None:
        """Test control characters are removed."""
        html = "hello\x01\x02\x03world"
        result = html_to_slack_mrkdwn(html)
        assert "helloworld" == result

    def test_newlines_preserved(self) -> None:
        """Test that newlines and tabs are preserved (not stripped as control chars)."""
        html = "line1\nline2\ttabbed"
        result = html_to_slack_mrkdwn(html)
        assert "\n" in result
        assert "\t" in result

    def test_max_length_enforced(self) -> None:
        """Test output is truncated to max length."""
        html = "a" * 3000
        result = html_to_slack_mrkdwn(html, max_length=100)
        assert len(result) == 100

    def test_custom_max_length(self) -> None:
        """Test custom max length parameter."""
        html = "a" * 100
        result = html_to_slack_mrkdwn(html, max_length=50)
        assert len(result) == 50


class TestHtmlToSlackMrkdwnEdgeCases:
    """Test edge cases and error handling."""

    def test_unclosed_tags(self) -> None:
        """Test handling of unclosed tags."""
        html = "<p>unclosed paragraph"
        # Should not raise, should return content
        result = html_to_slack_mrkdwn(html)
        assert "unclosed paragraph" in result

    def test_nested_same_tags(self) -> None:
        """Test nested identical tags."""
        html = "<b><b>double bold</b></b>"
        result = html_to_slack_mrkdwn(html)
        assert "double bold" in result

    def test_empty_tags(self) -> None:
        """Test empty tags."""
        html = "<p></p><b></b>"
        result = html_to_slack_mrkdwn(html)
        # Should just have formatting markers
        assert result == "**"

    def test_only_whitespace_in_tags(self) -> None:
        """Test tags containing only whitespace."""
        html = "<p>   </p>"
        result = html_to_slack_mrkdwn(html)
        # Whitespace should be preserved but result trimmed
        assert result == ""

    def test_pipe_in_link_text_escaped(self) -> None:
        """Test pipe character in link text is replaced."""
        html = '<a href="https://example.com">Choice A | Choice B</a>'
        result = html_to_slack_mrkdwn(html)
        # Pipe should be replaced to not break Slack link syntax
        assert "|Choice" not in result or result.count("|") == 1


class TestHtmlToSlackMrkdwnRealWorld:
    """Test with realistic Brandfetch-style descriptions."""

    def test_brandfetch_description_simple(self) -> None:
        """Test simple Brandfetch-style description."""
        html = (
            "<p>Acme Inc is a technology company that builds tools for developers.</p>"
        )
        result = html_to_slack_mrkdwn(html)
        assert "Acme Inc is a technology company" in result
        assert "<p>" not in result

    def test_brandfetch_description_with_link(self) -> None:
        """Test Brandfetch description with company website link."""
        html = (
            '<p>Founded in 2015, <a href="https://acme.com">Acme Inc</a> '
            "provides cloud solutions.</p>"
        )
        result = html_to_slack_mrkdwn(html)
        assert "<https://acme.com|Acme Inc>" in result
        assert "Founded in 2015" in result
        assert "provides cloud solutions" in result

    def test_brandfetch_description_complex(self) -> None:
        """Test complex Brandfetch description with multiple elements."""
        html = (
            '<p><a href="https://stripe.com">Stripe</a> is a technology company '
            "that builds economic infrastructure for the internet.</p>"
            "<p>Businesses of every size—from new startups to public companies—"
            "use our software to accept payments and manage their businesses "
            "online.</p>"
        )
        result = html_to_slack_mrkdwn(html)
        assert "<https://stripe.com|Stripe>" in result
        assert "technology company" in result
        assert "Businesses of every size" in result

    def test_brandfetch_description_with_formatting(self) -> None:
        """Test Brandfetch description with bold/italic formatting."""
        html = (
            "<p><strong>Acme Inc</strong> is the <em>leading</em> "
            "provider of widget solutions.</p>"
        )
        result = html_to_slack_mrkdwn(html)
        assert "*Acme Inc*" in result
        assert "_leading_" in result


class TestSanitizeUrl:
    """Test URL sanitization function."""

    def test_valid_https_url(self) -> None:
        """Test valid https URL is allowed."""
        assert _sanitize_url("https://example.com") == "https://example.com"

    def test_valid_http_url(self) -> None:
        """Test valid http URL is allowed."""
        assert _sanitize_url("http://example.com") == "http://example.com"

    def test_javascript_url_rejected(self) -> None:
        """Test javascript: URL is rejected."""
        assert _sanitize_url("javascript:alert(1)") is None

    def test_data_url_rejected(self) -> None:
        """Test data: URL is rejected."""
        assert _sanitize_url("data:text/html,test") is None

    def test_empty_url(self) -> None:
        """Test empty URL returns None."""
        assert _sanitize_url("") is None

    def test_whitespace_url(self) -> None:
        """Test whitespace-only URL returns None."""
        assert _sanitize_url("   ") is None

    def test_file_url_rejected(self) -> None:
        """Test file: URL is rejected."""
        assert _sanitize_url("file:///etc/passwd") is None

    def test_url_with_spaces_trimmed(self) -> None:
        """Test URL with leading/trailing spaces is trimmed."""
        assert _sanitize_url("  https://example.com  ") == "https://example.com"


class TestEscapeSlackMrkdwn:
    """Test Slack mrkdwn escaping function."""

    def test_escape_ampersand(self) -> None:
        """Test ampersand is escaped."""
        assert _escape_slack_mrkdwn("A & B") == "A &amp; B"

    def test_escape_less_than(self) -> None:
        """Test less than is escaped."""
        assert _escape_slack_mrkdwn("a < b") == "a &lt; b"

    def test_escape_greater_than(self) -> None:
        """Test greater than is escaped."""
        assert _escape_slack_mrkdwn("a > b") == "a &gt; b"

    def test_escape_all_special_chars(self) -> None:
        """Test all special characters are escaped correctly."""
        assert _escape_slack_mrkdwn("<a & b>") == "&lt;a &amp; b&gt;"


class TestSanitizeSlackInjection:
    """Test Slack injection pattern sanitization."""

    def test_channel_mention_neutralized(self) -> None:
        """Test <!channel> is neutralized."""
        assert _sanitize_slack_injection("<!channel>") == "@channel"
        result = _sanitize_slack_injection("Hey <!channel> check this")
        assert result == "Hey @channel check this"

    def test_here_mention_neutralized(self) -> None:
        """Test <!here> is neutralized."""
        assert _sanitize_slack_injection("<!here>") == "@here"

    def test_everyone_mention_neutralized(self) -> None:
        """Test <!everyone> is neutralized."""
        assert _sanitize_slack_injection("<!everyone>") == "@everyone"

    def test_broadcast_with_label_neutralized(self) -> None:
        """Test broadcast mentions with labels are neutralized."""
        assert _sanitize_slack_injection("<!channel|channel>") == "@channel"
        assert _sanitize_slack_injection("<!here|here>") == "@here"

    def test_user_mention_neutralized(self) -> None:
        """Test <@U123ABC> user mentions are neutralized."""
        assert _sanitize_slack_injection("<@U123ABC>") == "@user"
        assert _sanitize_slack_injection("<@W987XYZ>") == "@user"

    def test_user_mention_with_label_neutralized(self) -> None:
        """Test user mentions with display names are neutralized."""
        assert _sanitize_slack_injection("<@U123ABC|john>") == "@user"

    def test_channel_link_neutralized(self) -> None:
        """Test <#C123ABC> channel links are neutralized."""
        assert _sanitize_slack_injection("<#C123ABC>") == "#channel"
        assert _sanitize_slack_injection("<#C123ABC|general>") == "#channel"

    def test_subteam_mention_neutralized(self) -> None:
        """Test <!subteam^S123|@team> mentions are neutralized."""
        assert _sanitize_slack_injection("<!subteam^S123ABC|@engineering>") == "@team"

    def test_date_formatting_neutralized(self) -> None:
        """Test <!date^...> formatting is neutralized."""
        assert _sanitize_slack_injection("<!date^1234567890^{date}>") == "[date]"

    def test_case_insensitive(self) -> None:
        """Test patterns are matched case-insensitively."""
        # The injection is neutralized regardless of case
        assert _sanitize_slack_injection("<!CHANNEL>") == "@CHANNEL"
        assert _sanitize_slack_injection("<!Here>") == "@Here"
        # Key point: <! > syntax is removed, preventing Slack special handling
        assert "<!" not in _sanitize_slack_injection("<!CHANNEL>")
        assert "<!" not in _sanitize_slack_injection("<!Here>")

    def test_normal_text_unchanged(self) -> None:
        """Test normal text passes through unchanged."""
        text = "This is a normal company description."
        assert _sanitize_slack_injection(text) == text

    def test_legitimate_links_preserved(self) -> None:
        """Test legitimate Slack links are preserved."""
        # Our own link format should be preserved
        text = "<https://example.com|Example>"
        assert _sanitize_slack_injection(text) == text

    def test_multiple_injections_all_neutralized(self) -> None:
        """Test multiple injection attempts are all neutralized."""
        text = "<!channel> Hey <@U123> check <#C456> <!here>"
        result = _sanitize_slack_injection(text)
        assert "<!channel>" not in result
        assert "<@U123>" not in result
        assert "<#C456>" not in result
        assert "<!here>" not in result


class TestCleanControlCharacters:
    """Test control character cleaning function."""

    def test_remove_null_byte(self) -> None:
        """Test null byte is removed."""
        assert _clean_control_characters("a\x00b") == "ab"

    def test_remove_bell_character(self) -> None:
        """Test bell character is removed."""
        assert _clean_control_characters("a\x07b") == "ab"

    def test_preserve_newline(self) -> None:
        """Test newline is preserved."""
        assert _clean_control_characters("a\nb") == "a\nb"

    def test_preserve_tab(self) -> None:
        """Test tab is preserved."""
        assert _clean_control_characters("a\tb") == "a\tb"

    def test_remove_delete_character(self) -> None:
        """Test DEL character (0x7f) is removed."""
        assert _clean_control_characters("a\x7fb") == "ab"


class TestSafeMrkdwnLink:
    """Test safe_mrkdwn_link for untrusted URLs."""

    def test_valid_https_url(self) -> None:
        """Test a clean https URL builds a link."""
        result = safe_mrkdwn_link("https://example.com/profile", "Profile")
        assert result == "<https://example.com/profile|Profile>"

    def test_none_url(self) -> None:
        """Test None URL returns None."""
        assert safe_mrkdwn_link(None, "Label") is None

    def test_empty_url(self) -> None:
        """Test empty URL returns None."""
        assert safe_mrkdwn_link("", "Label") is None

    def test_rejects_non_http_scheme(self) -> None:
        """Test javascript: URLs are rejected."""
        assert safe_mrkdwn_link("javascript:alert(1)", "Label") is None

    def test_rejects_pipe_in_url(self) -> None:
        """Test a pipe in the URL cannot break the link syntax."""
        assert safe_mrkdwn_link("https://evil.example/|phish", "Label") is None

    def test_rejects_angle_brackets_in_url(self) -> None:
        """Test angle brackets in the URL cannot close the link early."""
        assert safe_mrkdwn_link("https://evil.example/><!channel", "Label") is None

    def test_rejects_interior_whitespace(self) -> None:
        """Test interior whitespace in the URL is rejected."""
        assert safe_mrkdwn_link("https://evil.example/a b", "Label") is None

    def test_strips_surrounding_whitespace(self) -> None:
        """Test surrounding whitespace is normalized, not rejected."""
        result = safe_mrkdwn_link("  https://example.com  ", "Site")
        assert result == "<https://example.com|Site>"

    def test_escapes_pipe_in_label(self) -> None:
        """Test a pipe in the label cannot break the link syntax."""
        result = safe_mrkdwn_link("https://example.com", "a|b")
        assert result == "<https://example.com|a-b>"

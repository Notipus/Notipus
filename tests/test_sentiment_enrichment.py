"""Tests for sentiment enrichment plugin graceful degradation.

Tests that the sentiment analysis plugin handles Ollama unavailability gracefully.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests
from plugins.enrichment.sentiment import SentimentEnrichmentPlugin, SentimentResult


class TestSentimentEnrichmentGracefulDegradation:
    """Tests for graceful degradation when Ollama is unavailable."""

    @pytest.fixture
    def plugin(self) -> SentimentEnrichmentPlugin:
        """Create a plugin instance with test configuration."""
        return SentimentEnrichmentPlugin(
            base_url="http://localhost:11434",
            model="llama3.2",
            timeout=5,
        )

    @pytest.fixture
    def sample_text(self) -> str:
        """Sample support ticket text for analysis."""
        return (
            "Cannot login to my account\n\n"
            "I've been trying to login for the past hour but keep getting "
            "'invalid password' even though I'm sure my password is correct. "
            "This is really frustrating as I need to access my account urgently."
        )

    # === is_available() tests ===

    def test_is_available_returns_false_when_disabled(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test is_available returns False when SENTIMENT_ANALYSIS_ENABLED is False."""
        with patch("plugins.enrichment.sentiment.settings") as mock_settings:
            mock_settings.SENTIMENT_ANALYSIS_ENABLED = False
            assert plugin.is_available() is False

    def test_is_available_returns_false_when_ollama_unreachable(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test is_available returns False when Ollama server is unreachable."""
        with patch("plugins.enrichment.sentiment.settings") as mock_settings:
            mock_settings.SENTIMENT_ANALYSIS_ENABLED = True

            with patch("plugins.enrichment.sentiment.requests.get") as mock_get:
                mock_get.side_effect = requests.exceptions.ConnectionError(
                    "Connection refused"
                )
                assert plugin.is_available() is False

    def test_is_available_returns_false_when_ollama_returns_error(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test is_available returns False when Ollama returns non-200 status."""
        with patch("plugins.enrichment.sentiment.settings") as mock_settings:
            mock_settings.SENTIMENT_ANALYSIS_ENABLED = True

            with patch("plugins.enrichment.sentiment.requests.get") as mock_get:
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response
                assert plugin.is_available() is False

    def test_is_available_returns_false_on_timeout(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test is_available returns False when Ollama request times out."""
        with patch("plugins.enrichment.sentiment.settings") as mock_settings:
            mock_settings.SENTIMENT_ANALYSIS_ENABLED = True

            with patch("plugins.enrichment.sentiment.requests.get") as mock_get:
                mock_get.side_effect = requests.exceptions.Timeout("Request timed out")
                assert plugin.is_available() is False

    def test_is_available_returns_true_when_ollama_healthy(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test is_available returns True when Ollama is healthy."""
        with patch("plugins.enrichment.sentiment.settings") as mock_settings:
            mock_settings.SENTIMENT_ANALYSIS_ENABLED = True

            with patch("plugins.enrichment.sentiment.requests.get") as mock_get:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_get.return_value = mock_response
                assert plugin.is_available() is True

    # === analyze() tests ===

    def test_analyze_returns_none_when_not_available(
        self, plugin: SentimentEnrichmentPlugin, sample_text: str
    ) -> None:
        """Test analyze returns None when is_available() returns False."""
        with patch.object(plugin, "is_available", return_value=False):
            result = plugin.analyze(sample_text)
            assert result is None

    def test_analyze_returns_none_for_short_text(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test analyze returns None for text shorter than 10 characters."""
        with patch.object(plugin, "is_available", return_value=True):
            result = plugin.analyze("Hi")
            assert result is None

    def test_analyze_returns_none_for_empty_text(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test analyze returns None for empty text."""
        with patch.object(plugin, "is_available", return_value=True):
            result = plugin.analyze("")
            assert result is None

    def test_analyze_returns_none_on_timeout(
        self, plugin: SentimentEnrichmentPlugin, sample_text: str
    ) -> None:
        """Test analyze returns None when Ollama request times out."""
        with patch.object(plugin, "is_available", return_value=True):
            with patch("plugins.enrichment.sentiment.requests.post") as mock_post:
                mock_post.side_effect = requests.exceptions.Timeout("Request timed out")
                result = plugin.analyze(sample_text)
                assert result is None

    def test_analyze_returns_none_on_connection_error(
        self, plugin: SentimentEnrichmentPlugin, sample_text: str
    ) -> None:
        """Test analyze returns None when Ollama connection fails."""
        with patch.object(plugin, "is_available", return_value=True):
            with patch("plugins.enrichment.sentiment.requests.post") as mock_post:
                mock_post.side_effect = requests.exceptions.ConnectionError(
                    "Connection refused"
                )
                result = plugin.analyze(sample_text)
                assert result is None

    def test_analyze_returns_none_on_invalid_json_response(
        self, plugin: SentimentEnrichmentPlugin, sample_text: str
    ) -> None:
        """Test analyze returns None when Ollama returns invalid JSON."""
        with patch.object(plugin, "is_available", return_value=True):
            with patch("plugins.enrichment.sentiment.requests.post") as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"response": "not valid json {{{"}
                mock_post.return_value = mock_response
                result = plugin.analyze(sample_text)
                assert result is None

    def test_analyze_returns_result_on_success(
        self, plugin: SentimentEnrichmentPlugin, sample_text: str
    ) -> None:
        """Test analyze returns SentimentResult on successful response."""
        valid_response = {
            "sentiment": "negative",
            "score": 0.85,
            "urgency": "high",
            "topics": ["account", "technical"],
            "summary": "User unable to login despite correct password.",
        }

        with patch.object(plugin, "is_available", return_value=True):
            with patch("plugins.enrichment.sentiment.requests.post") as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "response": '{"sentiment": "negative", "score": 0.85, '
                    '"urgency": "high", "topics": ["account", "technical"], '
                    '"summary": "User unable to login despite correct password."}'
                }
                mock_response.raise_for_status = MagicMock()
                mock_post.return_value = mock_response

                result = plugin.analyze(sample_text)

                assert result is not None
                assert isinstance(result, SentimentResult)
                assert result.sentiment == "negative"
                assert result.score == 0.85
                assert result.urgency == "high"
                assert result.topics == ["account", "technical"]

    # === enrich() interface tests ===

    def test_enrich_returns_empty_dict_without_text(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test enrich returns empty dict when no text provided."""
        result = plugin.enrich("example.com", data={})
        assert result == {}

    def test_enrich_returns_empty_dict_when_analysis_fails(
        self, plugin: SentimentEnrichmentPlugin, sample_text: str
    ) -> None:
        """Test enrich returns empty dict when analysis returns None."""
        with patch.object(plugin, "analyze", return_value=None):
            result = plugin.enrich("example.com", data={"text": sample_text})
            assert result == {}

    def test_enrich_returns_results_on_success(
        self, plugin: SentimentEnrichmentPlugin, sample_text: str
    ) -> None:
        """Test enrich returns sentiment data dict on success."""
        mock_result = SentimentResult(
            sentiment="negative",
            score=0.85,
            urgency="high",
            topics=["account"],
            summary="Test summary",
        )
        with patch.object(plugin, "analyze", return_value=mock_result):
            result = plugin.enrich("example.com", data={"text": sample_text})
            assert result["sentiment"] == "negative"
            assert result["sentiment_score"] == 0.85
            assert result["urgency"] == "high"
            assert result["topics"] == ["account"]
            assert result["summary"] == "Test summary"


class TestSentimentResultParsing:
    """Tests for parsing Ollama responses into SentimentResult."""

    @pytest.fixture
    def plugin(self) -> SentimentEnrichmentPlugin:
        """Create a plugin instance."""
        return SentimentEnrichmentPlugin()

    def test_parse_normalizes_invalid_sentiment(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test that invalid sentiment values are normalized to 'neutral'."""
        result = plugin._parse_result('{"sentiment": "angry", "score": 0.5}')
        assert result is not None
        assert result.sentiment == "neutral"

    def test_parse_clamps_score_to_valid_range(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test that score is clamped between 0.0 and 1.0."""
        result = plugin._parse_result('{"sentiment": "positive", "score": 1.5}')
        assert result is not None
        assert result.score == 1.0

        result = plugin._parse_result('{"sentiment": "negative", "score": -0.5}')
        assert result is not None
        assert result.score == 0.0

    def test_parse_normalizes_invalid_urgency(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test that invalid urgency values are normalized to 'medium'."""
        result = plugin._parse_result('{"sentiment": "neutral", "urgency": "critical"}')
        assert result is not None
        assert result.urgency == "medium"

    def test_parse_handles_markdown_code_blocks(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test that markdown code blocks are stripped from response."""
        response = '```json\n{"sentiment": "positive", "score": 0.9}\n```'
        result = plugin._parse_result(response)
        assert result is not None
        assert result.sentiment == "positive"

    def test_parse_limits_topics_to_five(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test that topics are limited to 5 items."""
        response = (
            '{"sentiment": "neutral", "topics": '
            '["a", "b", "c", "d", "e", "f", "g"]}'
        )
        result = plugin._parse_result(response)
        assert result is not None
        assert len(result.topics) == 5

    def test_parse_truncates_long_summary(
        self, plugin: SentimentEnrichmentPlugin
    ) -> None:
        """Test that summary is truncated to 100 characters."""
        long_summary = "A" * 200
        response = f'{{"sentiment": "neutral", "summary": "{long_summary}"}}'
        result = plugin._parse_result(response)
        assert result is not None
        assert len(result.summary) == 100

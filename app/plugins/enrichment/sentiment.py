"""Sentiment analysis enrichment plugin.

This module provides sentiment analysis for support ticket content using
a self-hosted Ollama instance for privacy. The plugin analyzes ticket
subject and description to determine sentiment, urgency, and topics.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.enrichment.base import BaseEnrichmentPlugin

logger = logging.getLogger(__name__)


@dataclass
class SentimentResult:
    """Result of sentiment analysis.

    Attributes:
        sentiment: Detected sentiment (positive, negative, neutral).
        score: Confidence score between 0.0 and 1.0.
        urgency: Detected urgency level (low, medium, high).
        topics: List of detected topics/categories.
        summary: Brief AI-generated summary of the content.
    """

    sentiment: str
    score: float
    urgency: str
    topics: list[str]
    summary: str | None = None


class SentimentEnrichmentPlugin(BaseEnrichmentPlugin):
    """Analyze support ticket content for sentiment and urgency.

    This plugin uses a self-hosted Ollama instance to analyze support
    ticket content, ensuring customer data privacy. It extracts:
    - Sentiment (positive, negative, neutral)
    - Urgency level (low, medium, high)
    - Topics/categories
    - Brief summary

    The plugin gracefully degrades if Ollama is unavailable.
    """

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata describing the sentiment enrichment plugin.
        """
        return PluginMetadata(
            name="sentiment",
            display_name="Sentiment Analysis",
            version="1.0.0",
            description="Analyze support content for sentiment using Ollama",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.CONTENT_ANALYSIS},
            priority=50,  # Lower priority, runs after other enrichment
        )

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        """Initialize the sentiment analysis plugin.

        Args:
            base_url: Ollama API base URL. Defaults to settings.OLLAMA_BASE_URL.
            model: Ollama model to use. Defaults to settings.OLLAMA_MODEL.
            timeout: Request timeout in seconds. Defaults to settings.OLLAMA_TIMEOUT.
        """
        self.base_url = base_url or getattr(
            settings, "OLLAMA_BASE_URL", "http://localhost:11434"
        )
        self.model = model or getattr(settings, "OLLAMA_MODEL", "llama3.2")
        self.timeout = timeout or getattr(settings, "OLLAMA_TIMEOUT", 30)

    def is_available(self) -> bool:
        """Check if sentiment analysis is available.

        Returns:
            True if sentiment analysis is enabled and Ollama is reachable.
        """
        if not getattr(settings, "SENTIMENT_ANALYSIS_ENABLED", False):
            return False

        try:
            response = requests.get(
                f"{self.base_url}/api/tags",
                timeout=5,
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def analyze(self, text: str) -> SentimentResult | None:
        """Analyze text for sentiment, urgency, and topics.

        Args:
            text: Text content to analyze (ticket subject + description).

        Returns:
            SentimentResult or None if analysis fails.
        """
        if not self.is_available():
            logger.debug("Sentiment analysis not available")
            return None

        if not text or len(text.strip()) < 10:
            logger.debug("Text too short for sentiment analysis")
            return None

        prompt = self._build_prompt(text)

        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            result_text = data.get("response", "")
            return self._parse_result(result_text)

        except requests.exceptions.Timeout:
            logger.debug("Ollama request timed out - sentiment analysis skipped")
            return None
        except requests.exceptions.RequestException as e:
            logger.debug(f"Ollama request failed (expected if Ollama unavailable): {e}")
            return None
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Failed to parse Ollama response: {e}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error in sentiment analysis: {e}")
            return None

    def _build_prompt(self, text: str) -> str:
        """Build the analysis prompt for Ollama.

        Args:
            text: Text content to analyze.

        Returns:
            Prompt string.
        """
        # Truncate very long text
        if len(text) > 2000:
            text = text[:2000] + "..."

        topics_list = (
            "billing, technical, account, feature, bug, docs, performance, security"
        )
        return f"""Analyze the support ticket and return JSON with:
- sentiment: "positive", "negative", or "neutral"
- score: confidence 0.0-1.0
- urgency: "low", "medium", or "high"
- topics: 1-3 from: {topics_list}
- summary: 1-sentence summary (max 100 chars)

Content:
\"\"\"
{text}
\"\"\"

Return only valid JSON:"""

    def _parse_result(self, result_text: str) -> SentimentResult | None:
        """Parse the Ollama response into a SentimentResult.

        Args:
            result_text: Raw response text from Ollama.

        Returns:
            SentimentResult or None if parsing fails.
        """
        try:
            # Try to extract JSON from the response
            result_text = result_text.strip()

            # Handle potential markdown code blocks
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                result_text = "\n".join(lines[1:-1])

            data = json.loads(result_text)

            # Validate and normalize values
            sentiment = data.get("sentiment", "neutral").lower()
            if sentiment not in ("positive", "negative", "neutral"):
                sentiment = "neutral"

            score = float(data.get("score", 0.5))
            score = max(0.0, min(1.0, score))

            urgency = data.get("urgency", "medium").lower()
            if urgency not in ("low", "medium", "high"):
                urgency = "medium"

            topics = data.get("topics", [])
            if not isinstance(topics, list):
                topics = []
            topics = [str(t) for t in topics[:5]]

            summary = data.get("summary")
            if summary:
                summary = str(summary)[:100]

            return SentimentResult(
                sentiment=sentiment,
                score=score,
                urgency=urgency,
                topics=topics,
                summary=summary,
            )

        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Failed to parse sentiment result: {e}")
            return None

    def enrich_domain(self, domain: str) -> dict[str, Any]:
        """Enrich domain data (not used for sentiment analysis).

        This method satisfies the BaseEnrichmentPlugin interface but
        sentiment analysis doesn't use domain-based enrichment.
        Use analyze() directly for sentiment analysis.

        Args:
            domain: Domain being enriched (not used).

        Returns:
            Empty dict - sentiment doesn't enrich by domain.
        """
        return {}

    def enrich(self, domain: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Enrich domain data with sentiment analysis.

        This method satisfies a common enrichment pattern but
        sentiment analysis is typically called directly via analyze().

        Args:
            domain: Domain being enriched (not used for sentiment).
            data: Optional data dict that may contain 'text' key.

        Returns:
            Dict with sentiment analysis results or empty dict.
        """
        if not data or "text" not in data:
            return {}

        result = self.analyze(data["text"])
        if not result:
            return {}

        return {
            "sentiment": result.sentiment,
            "sentiment_score": result.score,
            "urgency": result.urgency,
            "topics": result.topics,
            "summary": result.summary,
        }

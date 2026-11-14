"""Anthropic (Claude) LLM module for industry classification.

Uses the official ``anthropic`` SDK with structured outputs so the response
is guaranteed to match the classification schema.
"""

import logging
from typing import Any

import anthropic
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.enrichment.base_llm import BaseLLMEnrichmentPlugin, parse_json_object

logger = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (`fallbacks="default"`).
_FALLBACK_MODELS = frozenset(
    {
        "claude-fable-5-2",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
    }
)
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLMPlugin(BaseLLMEnrichmentPlugin):
    """Industry classification through the Claude API."""

    DEFAULT_MODEL = "claude-opus-5-5"
    MAX_TOKENS = 4096

    def __init__(self) -> None:
        """Initialise with no client; it is created lazily."""
        super().__init__()
        self._client: anthropic.Anthropic | None = None

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="anthropic",
            display_name="Anthropic Claude",
            version="1.0.0",
            description="Industry classification with Claude",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.INDUSTRY},
            priority=10,
            config_keys=["api_key"],
        )

    @classmethod
    def default_model(cls) -> str:
        """Default Claude model."""
        return cls.DEFAULT_MODEL

    def _get_client(self) -> anthropic.Anthropic:
        """Return a cached SDK client."""
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=self.timeout, max_retries=1
            )
        return self._client

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any] | None:
        """Request a schema-constrained JSON object from Claude."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            # Classification is a simple task: low effort keeps it cheap/fast.
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        client = self._get_client()
        if self.model in _FALLBACK_MODELS:
            response = client.beta.messages.create(
                betas=[_FALLBACK_BETA], fallbacks="default", **params
            )
        else:
            response = client.messages.create(**params)

        if response.stop_reason in ("refusal", "max_tokens"):
            logger.warning("Claude classification stopped: %s", response.stop_reason)
            return None

        text = "".join(block.text for block in response.content if block.type == "text")
        parsed: dict[str, Any] | None = parse_json_object(text)
        return parsed

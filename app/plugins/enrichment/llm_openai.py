"""OpenAI LLM module for industry classification.

Uses the official ``openai`` SDK with strict JSON-schema output.
"""

import logging
from typing import Any

import openai
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.enrichment.base_llm import BaseLLMEnrichmentPlugin, parse_json_object

logger = logging.getLogger(__name__)


class OpenAILLMPlugin(BaseLLMEnrichmentPlugin):
    """Industry classification through the OpenAI API."""

    DEFAULT_MODEL = "gpt-5-mini"

    def __init__(self) -> None:
        """Initialise with no client; it is created lazily."""
        super().__init__()
        self._client: openai.OpenAI | None = None

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="openai",
            display_name="OpenAI",
            version="1.0.0",
            description="Industry classification with OpenAI models",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.INDUSTRY},
            priority=10,
            config_keys=["api_key"],
        )

    @classmethod
    def default_model(cls) -> str:
        """Default OpenAI model."""
        return cls.DEFAULT_MODEL

    def _get_client(self) -> openai.OpenAI:
        """Return a cached SDK client."""
        if self._client is None:
            self._client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=1,
            )
        return self._client

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any] | None:
        """Request a strict JSON-schema object from OpenAI."""
        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        )
        if not response.choices:
            return None
        choice = response.choices[0]
        if choice.finish_reason == "length" or choice.message.refusal:
            logger.warning("OpenAI classification incomplete or refused")
            return None
        parsed: dict[str, Any] | None = parse_json_object(choice.message.content)
        return parsed

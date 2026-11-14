"""Ollama LLM module for industry classification.

Talks to a self-hosted Ollama server over its HTTP API, using the ``format``
parameter to constrain the reply to the classification schema. Enabled by
setting ``OLLAMA_BASE_URL``; no API key is needed (an optional bearer token
can be set for servers behind an authenticating proxy).
"""

import logging
from typing import Any

import requests
from plugins.base import PluginCapability, PluginMetadata, PluginType
from plugins.enrichment.base_llm import BaseLLMEnrichmentPlugin, parse_json_object

logger = logging.getLogger(__name__)


class OllamaLLMPlugin(BaseLLMEnrichmentPlugin):
    """Industry classification through a self-hosted Ollama server."""

    DEFAULT_MODEL = "llama3.2"
    DEFAULT_TIMEOUT = 60
    REQUIRED_CONFIG = "base_url"

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="ollama",
            display_name="Ollama",
            version="1.0.0",
            description="Industry classification with a self-hosted Ollama model",
            plugin_type=PluginType.ENRICHMENT,
            capabilities={PluginCapability.INDUSTRY},
            priority=10,
            config_keys=["base_url"],
        )

    @classmethod
    def default_model(cls) -> str:
        """Default Ollama model."""
        return cls.DEFAULT_MODEL

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any] | None:
        """Request a schema-constrained JSON object from Ollama."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = requests.post(
            f"{(self.base_url or '').rstrip('/')}/api/chat",
            headers=headers,
            json={
                "model": self.model,
                "stream": False,
                "format": schema,
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        message = response.json().get("message") or {}
        parsed: dict[str, Any] | None = parse_json_object(message.get("content"))
        return parsed

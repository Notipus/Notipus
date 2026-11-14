"""Tests for LLM industry classification modules.

Covers:
- Privacy: only a bare domain and email-free public metadata reach providers
- Output validation against the fixed taxonomy
- Round-robin selection and failover between LLM modules
- Brandfetch data used as grounding, and the domain-only fallback
- The OpenAI, Anthropic and Ollama modules' request shapes
- Blending, notification display and the backfill command
"""

import json
from collections.abc import Iterator
from io import StringIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.models import Company
from core.services.enrichment import DataBlender, DomainEnrichmentService
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from plugins import PluginRegistry
from plugins.base import PluginMetadata, PluginType
from plugins.enrichment import BaseEnrichmentPlugin
from plugins.enrichment.base_llm import (
    CLASSIFICATION_SCHEMA,
    BaseLLMEnrichmentPlugin,
    normalize_domain,
)
from plugins.enrichment.brandfetch import BrandfetchPlugin
from plugins.enrichment.llm_anthropic import AnthropicLLMPlugin
from plugins.enrichment.llm_ollama import OllamaLLMPlugin
from plugins.enrichment.llm_openai import OpenAILLMPlugin
from webhooks.models.rich_notification import CompanyInfo
from webhooks.services.notification_builder import NotificationBuilder

VALID_RESULT: dict[str, Any] = {
    "name": "Acme",
    "description": "Acme sells payroll software to mid-sized companies.",
    "year_founded": 2015,
    "employee_count": "51-200",
    "headquarters": "Berlin, Germany",
    "industry": "Software & SaaS",
    "sub_industry": "Developer Tools",
    "business_model": "B2B",
    "target_market": "Mid-market",
    "keywords": ["devtools", "api"],
    "confidence": "high",
}


class FakeLLM(BaseLLMEnrichmentPlugin):
    """LLM module that records prompts instead of calling a provider."""

    NAME = "fake_llm"

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        """Create a fake returning ``result`` from every call."""
        super().__init__()
        self.api_key = "key"
        self.model = "fake-model"
        self.result = result if result is not None else dict(VALID_RESULT)
        self.calls: list[str] = []

    @classmethod
    def get_metadata(cls) -> PluginMetadata:
        """Return metadata for the fake module."""
        return PluginMetadata(
            name=cls.NAME,
            display_name="Fake",
            version="1.0.0",
            description="Fake LLM",
            plugin_type=PluginType.ENRICHMENT,
        )

    @classmethod
    def default_model(cls) -> str:
        """Return the fake default model."""
        return "fake-model"

    def get_plugin_name(self) -> str:
        """Return a per-instance name so several fakes can coexist."""
        return self.NAME

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any] | None:
        """Record the prompt and return the canned result."""
        self.calls.append(user_prompt)
        return self.result


def make_fake(name: str, result: dict[str, Any] | None = None) -> FakeLLM:
    """Build a FakeLLM with a custom plugin name.

    Args:
        name: Plugin name to report.
        result: Canned classification ({} simulates a failure).

    Returns:
        Configured fake module.
    """
    fake = FakeLLM(result)
    fake.NAME = name  # type: ignore[misc]
    return fake


def build_service(plugins: list[BaseEnrichmentPlugin]) -> DomainEnrichmentService:
    """Build a DomainEnrichmentService over the given plugin instances.

    Args:
        plugins: Plugins the mocked registry reports as enabled.

    Returns:
        Service using exactly those plugins.
    """
    with (
        patch("core.services.enrichment.PluginRegistry") as registry_cls,
        patch("core.services.enrichment.settings") as mock_settings,
    ):
        mock_settings.PLUGIN_AUTODISCOVER = False
        registry_cls.instance.return_value.get_enabled.return_value = plugins
        return DomainEnrichmentService()


def brandfetch_plugin(data: dict[str, Any]) -> MagicMock:
    """Build a mock Brandfetch plugin returning ``data``.

    Args:
        data: Data returned by enrich_domain.

    Returns:
        Mock plugin.
    """
    plugin = MagicMock(spec=BaseEnrichmentPlugin)
    plugin.get_plugin_name.return_value = "brandfetch"
    plugin.enrich_domain.return_value = data
    plugin.cache_key.side_effect = lambda domain, context=None: f"brandfetch:{domain}"
    return plugin


@pytest.fixture(autouse=True)
def _reset_round_robin() -> Iterator[None]:
    """Give each test a deterministic process-local round-robin counter."""
    import itertools

    original = DomainEnrichmentService._local_counter
    DomainEnrichmentService._local_counter = itertools.count()
    yield
    DomainEnrichmentService._local_counter = original


# ============================================================================
# Privacy
# ============================================================================


class TestPrivacy:
    """Only a domain and email-free public metadata may leave the system."""

    @pytest.mark.parametrize(
        "value",
        [
            "jane@example.com",
            "https://example.com/path",
            "example.com/path",
            "exa mple.com",
            "localhost",
            "",
        ],
    )
    def test_normalize_domain_rejects_non_domains(self, value: str) -> None:
        """Emails, URLs and junk never pass as a domain."""
        assert normalize_domain(value) is None

    def test_normalize_domain_accepts_domain(self) -> None:
        """A plain domain is lower-cased and kept."""
        assert normalize_domain(" Sub.Example.CO.uk. ") == "sub.example.co.uk"

    def test_email_input_is_never_sent(self) -> None:
        """Passing an email address makes no provider call at all."""
        fake = FakeLLM()
        assert fake.enrich_domain("jane@example.com") == {}
        assert fake.calls == []

    def test_emails_in_context_are_redacted(self) -> None:
        """Email-like strings inside Brandfetch text are redacted."""
        fake = FakeLLM()
        fake.enrich_domain(
            "example.com",
            context={
                "name": "Example",
                "brand_info": {
                    "description": "Contact sales@example.com or jane.doe@corp.io",
                    "industry": "Software",
                },
            },
        )
        prompt = fake.calls[0]
        assert "@" not in prompt
        assert "[redacted]" in prompt
        assert "Example" in prompt

    def test_only_allow_listed_context_fields_are_sent(self) -> None:
        """Fields outside name/description/industry never reach the prompt."""
        fake = FakeLLM()
        fake.enrich_domain(
            "example.com",
            context={
                "name": "Example",
                "email": "customer@example.com",
                "customer_name": "Jane Doe",
                "brand_info": {"links": [{"url": "https://x.com/example"}]},
            },
        )
        payload = json.loads(fake.calls[0].split("\n", 1)[1])
        assert payload == {
            "domain": "example.com",
            "brand_database": {"name": "Example"},
        }

    def test_domain_only_without_context(self) -> None:
        """Without Brandfetch data, only the domain is sent."""
        fake = FakeLLM()
        result = fake.enrich_domain("example.com")
        payload = json.loads(fake.calls[0].split("\n", 1)[1])
        assert payload == {"domain": "example.com"}
        assert result["brand_info"]["industry_classification"]["grounded"] is False


# ============================================================================
# Validation
# ============================================================================


class TestValidation:
    """Provider output is coerced into the stored shape."""

    def test_valid_result_is_stored(self) -> None:
        """A valid result is returned with provenance."""
        result = FakeLLM().enrich_domain("example.com", context={"name": "Ex"})
        classification = result["brand_info"]["industry_classification"]
        assert result["brand_info"]["industry"] == "Software & SaaS"
        assert classification["sub_industry"] == "Developer Tools"
        assert classification["provider"] == "fake_llm"
        assert classification["model"] == "fake-model"
        assert classification["grounded"] is True

    def test_unknown_industry_is_rejected(self) -> None:
        """An industry outside the taxonomy yields no data."""
        fake = FakeLLM({**VALID_RESULT, "industry": "Space Pirates"})
        assert fake.enrich_domain("example.com") == {}

    def test_bad_enums_and_keywords_are_coerced(self) -> None:
        """Invalid enums fall back to safe defaults; keywords are capped."""
        fake = FakeLLM(
            {
                **VALID_RESULT,
                "business_model": "Pyramid",
                "target_market": 7,
                "confidence": "certain",
                "keywords": ["a", "a", "b", "c", "d", "e", "f", "x" * 100],
            }
        )
        c = fake.enrich_domain("example.com")["brand_info"]["industry_classification"]
        assert c["business_model"] == "Unknown"
        assert c["target_market"] == "Unknown"
        assert c["confidence"] == "low"
        assert c["keywords"] == ["a", "b", "c", "d", "e"]

    def test_provider_exception_returns_empty(self) -> None:
        """SDK errors are swallowed so webhook processing continues."""
        fake = FakeLLM()
        fake.complete_json = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        assert fake.enrich_domain("example.com") == {}

    def test_schema_is_strict(self) -> None:
        """Every property is required, as OpenAI strict mode demands."""
        assert CLASSIFICATION_SCHEMA["additionalProperties"] is False
        assert set(CLASSIFICATION_SCHEMA["required"]) == set(
            CLASSIFICATION_SCHEMA["properties"]
        )


# ============================================================================
# Round robin and grounding
# ============================================================================


@pytest.mark.django_db
class TestServiceRoundRobin:
    """The service calls one LLM module per domain, rotating between them."""

    def test_alternates_between_modules(self) -> None:
        """Consecutive domains go to alternating modules."""
        openai_fake = make_fake("openai")
        anthropic_fake = make_fake("anthropic")
        service = build_service([openai_fake, anthropic_fake])

        for i in range(4):
            service.enrich_domain(f"company{i}.com")

        assert len(openai_fake.calls) == 2
        assert len(anthropic_fake.calls) == 2

    def test_single_module_used_alone(self) -> None:
        """With one module configured, it handles every domain."""
        only = make_fake("anthropic")
        service = build_service([only])
        service.enrich_domain("a.com")
        service.enrich_domain("b.com")
        assert len(only.calls) == 2

    def test_fails_over_to_next_module(self) -> None:
        """If the module in turn fails, the next one is tried."""
        broken = make_fake("openai", result={})
        working = make_fake("anthropic")
        service = build_service([broken, working])

        company = service.enrich_domain("example.com")

        assert company is not None
        assert len(broken.calls) == 1
        assert len(working.calls) == 1
        classification = company.brand_info["industry_classification"]
        assert classification["provider"] == "anthropic"

    def test_brandfetch_data_grounds_llm_and_llm_industry_wins(self) -> None:
        """Brandfetch output is passed as context; LLM industry is preferred."""
        bf = brandfetch_plugin(
            {
                "name": "Acme",
                "logo_url": None,
                "brand_info": {"description": "Payroll for SMBs", "industry": "Tech"},
            }
        )
        llm = make_fake("openai")
        service = build_service([bf, llm])

        company = service.enrich_domain("acme.com")

        payload = json.loads(llm.calls[0].split("\n", 1)[1])
        assert payload["brand_database"]["description"] == "Payroll for SMBs"
        assert company is not None
        assert company.brand_info["industry"] == "Software & SaaS"
        # The LLM's rewrite of Brandfetch's description is preferred.
        assert company.brand_info["description"] == VALID_RESULT["description"]
        assert company.name == "Acme"

    def test_llm_alone_when_brandfetch_has_nothing(self) -> None:
        """No Brandfetch data: the LLM is the only source, given just the domain."""
        bf = brandfetch_plugin({})
        llm = make_fake("anthropic")
        service = build_service([bf, llm])

        company = service.enrich_domain("acme.com")

        assert json.loads(llm.calls[0].split("\n", 1)[1]) == {"domain": "acme.com"}
        assert company is not None
        assert company.brand_info["industry_classification"]["grounded"] is False

    def test_no_llm_modules_keeps_existing_behaviour(self) -> None:
        """Without LLM modules, enrichment is Brandfetch-only as before."""
        bf = brandfetch_plugin({"name": "Acme", "brand_info": {"industry": "Tech"}})
        service = build_service([bf])
        company = service.enrich_domain("acme.com")
        assert company is not None
        assert company.brand_info["industry"] == "Tech"
        assert "industry_classification" not in company.brand_info


# ============================================================================
# Provider modules
# ============================================================================


class TestAnthropicModule:
    """Request shape for the Claude module."""

    def _plugin(self, model: str = "") -> AnthropicLLMPlugin:
        """Build a configured Claude module."""
        plugin = AnthropicLLMPlugin()
        plugin.configure({"api_key": "sk-test", "model": model})
        return plugin

    def _response(self, text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
        """Build a fake SDK response."""
        return SimpleNamespace(
            stop_reason=stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=text),
            ],
        )

    def test_default_model_uses_structured_output_and_fallbacks(self) -> None:
        """Default model goes through the beta endpoint with fallbacks."""
        plugin = self._plugin()
        client = MagicMock()
        client.beta.messages.create.return_value = self._response(
            json.dumps(VALID_RESULT)
        )
        plugin._client = client

        result = plugin.enrich_domain("example.com")

        kwargs = client.beta.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-5-5"
        assert kwargs["fallbacks"] == "default"
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert kwargs["output_config"]["effort"] == "low"
        assert result["brand_info"]["industry"] == "Software & SaaS"

    def test_other_model_uses_plain_endpoint(self) -> None:
        """Models without server-side fallback use messages.create."""
        plugin = self._plugin("claude-haiku-5-5")
        client = MagicMock()
        client.messages.create.return_value = self._response(json.dumps(VALID_RESULT))
        plugin._client = client

        plugin.enrich_domain("example.com")

        client.messages.create.assert_called_once()
        client.beta.messages.create.assert_not_called()

    def test_refusal_returns_empty(self) -> None:
        """A refusal is treated as no data."""
        plugin = self._plugin()
        client = MagicMock()
        client.beta.messages.create.return_value = self._response("", "refusal")
        plugin._client = client
        assert plugin.enrich_domain("example.com") == {}


class TestOpenAIModule:
    """Request shape for the OpenAI module."""

    def _response(
        self, content: str | None, refusal: str | None = None
    ) -> SimpleNamespace:
        """Build a fake SDK response."""
        message = SimpleNamespace(content=content, refusal=refusal)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )

    def test_uses_strict_json_schema(self) -> None:
        """Requests strict JSON-schema output and parses it."""
        plugin = OpenAILLMPlugin()
        plugin.configure({"api_key": "sk-test"})
        client = MagicMock()
        client.chat.completions.create.return_value = self._response(
            json.dumps(VALID_RESULT)
        )
        plugin._client = client

        result = plugin.enrich_domain("example.com")

        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == OpenAILLMPlugin.DEFAULT_MODEL
        assert kwargs["response_format"]["json_schema"]["strict"] is True
        assert result["brand_info"]["industry_classification"]["provider"] == "openai"

    def test_refusal_returns_empty(self) -> None:
        """A refusal is treated as no data."""
        plugin = OpenAILLMPlugin()
        plugin.configure({"api_key": "sk-test"})
        client = MagicMock()
        client.chat.completions.create.return_value = self._response(None, "no")
        plugin._client = client
        assert plugin.enrich_domain("example.com") == {}


class TestOllamaModule:
    """Request shape for the Ollama module."""

    def test_posts_schema_to_chat_endpoint(self) -> None:
        """Calls /api/chat with the schema as the format."""
        plugin = OllamaLLMPlugin()
        plugin.configure({"base_url": "http://ollama:11434/", "model": "qwen3"})
        response = MagicMock()
        response.json.return_value = {"message": {"content": json.dumps(VALID_RESULT)}}

        with patch(
            "plugins.enrichment.llm_ollama.requests.post", return_value=response
        ) as post:
            result = plugin.enrich_domain("example.com")

        assert post.call_args.args[0] == "http://ollama:11434/api/chat"
        body = post.call_args.kwargs["json"]
        assert body["format"] == CLASSIFICATION_SCHEMA
        assert body["model"] == "qwen3"
        assert body["stream"] is False
        assert "Authorization" not in post.call_args.kwargs["headers"]
        assert result["brand_info"]["industry_classification"]["provider"] == "ollama"

    def test_unconfigured_makes_no_request(self) -> None:
        """Without a base URL nothing is sent."""
        plugin = OllamaLLMPlugin()
        plugin.configure({})
        with patch("plugins.enrichment.llm_ollama.requests.post") as post:
            assert plugin.enrich_domain("example.com") == {}
        post.assert_not_called()


class TestAvailability:
    """Modules are available only once configured."""

    @override_settings(
        PLUGINS={
            "enrichment": {
                "openai": {"config": {"api_key": ""}},
                "anthropic": {"config": {"api_key": "sk-ant"}},
                "ollama": {"config": {"base_url": "http://ollama:11434"}},
            }
        }
    )
    def test_availability_from_settings(self) -> None:
        """API-key modules need a key; Ollama needs a base URL."""
        assert OpenAILLMPlugin.is_available() is False
        assert AnthropicLLMPlugin.is_available() is True
        assert OllamaLLMPlugin.is_available() is True

    def test_modules_are_discovered(self) -> None:
        """Auto-discovery registers all three modules."""
        PluginRegistry.reset()
        registry = PluginRegistry.instance()
        discovered = registry.discover()[PluginType.ENRICHMENT]
        assert {"openai", "anthropic", "ollama"} <= set(discovered)
        PluginRegistry.reset()


# ============================================================================
# Blending, display, backfill
# ============================================================================


class TestBlendAndDisplay:
    """Classification flows through blending into notifications."""

    def test_blender_prefers_llm_industry(self) -> None:
        """LLM industry and classification beat Brandfetch's industry."""
        classification = {**VALID_RESULT, "provider": "anthropic"}
        blended = DataBlender().blend(
            {
                "brandfetch": {"name": "Acme", "brand_info": {"industry": "Tech"}},
                "anthropic": {
                    "brand_info": {
                        "industry": "Software & SaaS",
                        "industry_classification": classification,
                    }
                },
            }
        )
        assert blended["industry"] == "Software & SaaS"
        assert blended["industry_classification"] == classification

    def test_company_info_labels(self) -> None:
        """Labels combine industry/sub-industry and model/market."""
        info = CompanyInfo(
            name="Acme",
            domain="acme.com",
            industry="Software & SaaS",
            sub_industry="Payroll",
            business_model="B2B",
            target_market="Unknown",
        )
        assert info.industry_label == "Software & SaaS › Payroll"
        assert info.segment_label == "B2B"
        assert CompanyInfo(name="A", domain="a.com").segment_label is None

    def test_builder_reads_classification(self) -> None:
        """The notification builder exposes the classification fields."""
        company = Company(
            domain="acme.com",
            name="Acme",
            brand_info={
                "industry": "Software & SaaS",
                "industry_classification": VALID_RESULT,
            },
        )
        info = NotificationBuilder()._build_company_info(company)
        assert info.industry_label == "Software & SaaS › Developer Tools"
        assert info.segment_label == "B2B · Mid-market"


@pytest.mark.django_db
class TestBackfill:
    """Existing companies can be classified without re-calling Brandfetch."""

    def _company(self) -> Company:
        """Create a company enriched before LLM modules existed."""
        return Company.objects.create(
            domain="acme.com",
            name="Acme",
            brand_info={
                "name": "Acme",
                "description": "Payroll",
                "_blended_at": "2026-11-01T00:00:00+00:00",
                "_sources": {
                    "brandfetch": {
                        "fetched_at": "2026-11-01T00:00:00+00:00",
                        "raw": {
                            "name": "Acme",
                            "brand_info": {"description": "Payroll"},
                        },
                    }
                },
            },
        )

    def test_classify_company_reuses_stored_data(self) -> None:
        """Stored Brandfetch data grounds the LLM; freshness is untouched."""
        company = self._company()
        llm = make_fake("openai")
        service = build_service([llm])

        assert service.classify_company(company) is True

        company.refresh_from_db()
        assert company.brand_info["industry"] == "Software & SaaS"
        assert company.brand_info["_blended_at"] == "2026-11-01T00:00:00+00:00"
        assert set(company.brand_info["_sources"]) == {"brandfetch", "openai"}
        assert "Payroll" in llm.calls[0]
        sources = company.brand_info["_sources"]
        assert sources["brandfetch"]["fetched_at"] == "2026-11-01T00:00:00+00:00"
        assert company.brand_info["description"] == VALID_RESULT["description"]

    def test_command_classifies_unclassified(self) -> None:
        """The command classifies companies lacking a classification."""
        self._company()
        service = build_service([make_fake("openai")])
        out = StringIO()
        with patch(
            "core.management.commands.classify_companies.DomainEnrichmentService",
            return_value=service,
        ):
            call_command("classify_companies", stdout=out)
            call_command("classify_companies", stdout=out)

        output = out.getvalue()
        assert "acme.com: Software & SaaS" in output
        assert output.count("acme.com") == 1  # second run skips it

    def test_command_requires_llm_module(self) -> None:
        """Without any LLM module the command fails clearly."""
        service = build_service([])
        with (
            patch(
                "core.management.commands.classify_companies.DomainEnrichmentService",
                return_value=service,
            ),
            pytest.raises(CommandError),
        ):
            call_command("classify_companies")


# ============================================================================
# Company profile generation
# ============================================================================

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "llm-enrichment-tests",
    }
}

BRANDFETCH_FULL: dict[str, Any] = {
    "name": "Acme",
    "logo_url": None,
    "brand_info": {
        "description": "Acme. The #1 payroll platform! Email hello@acme.com",
        "long_description": "Founded in Berlin, Acme builds payroll tools.",
        "industry": "Software › Payroll",
        "industries": ["Software › Payroll", "Finance"],
        "year_founded": 2014,
        "employee_count": None,
        "headquarters": None,
        "company_kind": "PRIVATELY_HELD",
        "links": [{"name": "linkedin", "url": "https://linkedin.com/acme"}],
        "colors": [{"hex": "#ff0000"}],
    },
}


class TestBrandfetchFirmographics:
    """Brandfetch's company object is extracted for blending and LLM input."""

    def test_company_object_is_extracted(self) -> None:
        """Industries, founding year, headcount band and location are mapped."""
        plugin = BrandfetchPlugin()
        plugin.configure({"api_key": "k"})
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {
            "name": "Acme",
            "description": "Payroll",
            "longDescription": "Long payroll text",
            "logos": [],
            "company": {
                "employees": 51,
                "foundedYear": 2014,
                "kind": "PRIVATELY_HELD",
                "location": {"city": "Berlin", "country": "Germany"},
                "industries": [
                    {"name": "Payroll", "parent": {"name": "Software"}},
                    {"name": "Finance", "parent": None},
                ],
            },
        }
        with patch("plugins.enrichment.brandfetch.requests.get", return_value=response):
            info = plugin.enrich_domain("acme.com")["brand_info"]

        assert info["industry"] == "Software › Payroll"
        assert info["industries"] == ["Software › Payroll", "Finance"]
        assert info["year_founded"] == 2014
        assert info["employee_count"] == "51-200"
        assert info["headquarters"] == "Berlin, Germany"
        assert info["long_description"] == "Long payroll text"
        assert info["company_kind"] == "PRIVATELY_HELD"


@pytest.mark.django_db
class TestCompanyProfile:
    """The LLM produces the Brandfetch-style profile, using Brandfetch as input."""

    def test_full_brandfetch_record_is_sent_minus_links_and_emails(self) -> None:
        """All firmographic fields go in; links, colours and emails do not."""
        fake = FakeLLM()
        fake.enrich_domain("acme.com", context=BRANDFETCH_FULL)
        payload = json.loads(fake.calls[0].split("\n", 1)[1])["brand_database"]

        assert payload["long_description"].startswith("Founded in Berlin")
        assert payload["year_founded"] == 2014
        assert payload["industries"] == "Software › Payroll, Finance"
        assert payload["company_kind"] == "PRIVATELY_HELD"
        assert "links" not in payload and "colors" not in payload
        assert "@" not in fake.calls[0]

    def test_profile_blending(self) -> None:
        """LLM description wins; Brandfetch facts win; LLM fills the gaps."""
        service = build_service(
            [brandfetch_plugin(BRANDFETCH_FULL), make_fake("openai")]
        )
        company = service.enrich_domain("acme.com")
        assert company is not None
        info = company.brand_info

        assert info["description"] == VALID_RESULT["description"]
        assert info["year_founded"] == 2014  # Brandfetch over LLM's 2015
        assert info["employee_count"] == "51-200"  # gap filled by LLM
        assert info["headquarters"] == "Berlin, Germany"  # gap filled by LLM
        assert info["industry"] == "Software & SaaS"

    def test_unknown_values_are_not_stored(self) -> None:
        """'Unknown', null and '' never overwrite or fill a field."""
        fake = FakeLLM(
            {
                **VALID_RESULT,
                "year_founded": None,
                "employee_count": "Unknown",
                "headquarters": "",
            }
        )
        info = fake.enrich_domain("acme.com")["brand_info"]
        assert "year_founded" not in info
        assert "employee_count" not in info
        assert "headquarters" not in info

    @pytest.mark.parametrize("year", [1066, 3000, "soon", True])
    def test_implausible_year_is_dropped(self, year: Any) -> None:
        """Founding years outside a plausible range are discarded."""
        fake = FakeLLM({**VALID_RESULT, "year_founded": year})
        assert "year_founded" not in fake.enrich_domain("acme.com")["brand_info"]

    def test_llm_alone_provides_profile(self) -> None:
        """Without Brandfetch data the LLM profile is used as-is."""
        service = build_service([brandfetch_plugin({}), make_fake("anthropic")])
        company = service.enrich_domain("acme.com")
        assert company is not None
        assert company.name == "Acme"
        assert company.brand_info["year_founded"] == 2015


# ============================================================================
# Caching
# ============================================================================


@pytest.mark.django_db
class TestResponseCache:
    """Every provider response is cached."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self, settings: Any) -> Iterator[None]:
        """Use a real in-memory cache, empty at start and end of each test."""
        settings.CACHES = LOCMEM_CACHE
        cache.clear()
        yield
        cache.clear()

    def test_provider_and_llm_responses_are_cached(self) -> None:
        """A second enrichment of the same domain makes no provider calls."""
        bf = brandfetch_plugin(BRANDFETCH_FULL)
        llm = make_fake("openai")
        service = build_service([bf, llm])

        service.enrich_domain("acme.com")
        Company.objects.all().delete()  # force a second enrichment pass
        service.enrich_domain("acme.com")

        assert bf.enrich_domain.call_count == 1
        assert len(llm.calls) == 1

    def test_empty_responses_are_cached_briefly(self) -> None:
        """A domain with no data is not re-fetched on every webhook."""
        bf = brandfetch_plugin({})
        service = build_service([bf])
        with patch.object(cache, "set", wraps=cache.set) as cache_set:
            service.enrich_domain("nothing.com")
            service.enrich_domain("nothing.com")

        assert bf.enrich_domain.call_count == 1
        assert (
            cache_set.call_args.kwargs["timeout"]
            == DomainEnrichmentService.EMPTY_RESPONSE_CACHE_SECONDS
        )

    def test_errors_are_not_cached(self) -> None:
        """A provider exception is retried next time."""
        bf = brandfetch_plugin({})
        bf.enrich_domain.side_effect = [RuntimeError("down"), {"name": "Acme"}]
        service = build_service([bf])

        service.enrich_domain("acme.com")
        company = service.enrich_domain("acme.com")

        assert bf.enrich_domain.call_count == 2
        assert company is not None and company.name == "Acme"

    def test_llm_cache_key_tracks_input(self) -> None:
        """Changed Brandfetch data or model means a new LLM call."""
        fake = FakeLLM()
        base = fake.cache_key("acme.com", BRANDFETCH_FULL)
        assert base == fake.cache_key("acme.com", BRANDFETCH_FULL)
        assert base != fake.cache_key("acme.com", {"name": "Other"})
        fake.model = "other-model"
        assert base != fake.cache_key("acme.com", BRANDFETCH_FULL)
        assert "@" not in base

    def test_refresh_bypasses_cache(self) -> None:
        """refresh_enrichment always calls the providers again."""
        bf = brandfetch_plugin({"name": "Acme"})
        llm = make_fake("openai")
        service = build_service([bf, llm])

        service.enrich_domain("acme.com")
        service.refresh_enrichment("acme.com")

        assert bf.enrich_domain.call_count == 2
        assert len(llm.calls) == 2

    def test_cache_outage_is_tolerated(self) -> None:
        """If the cache backend fails, enrichment still works."""
        service = build_service([brandfetch_plugin({"name": "Acme"})])
        with (
            patch.object(cache, "get", side_effect=ConnectionError),
            patch.object(cache, "set", side_effect=ConnectionError),
        ):
            company = service.enrich_domain("acme.com")
        assert company is not None and company.name == "Acme"

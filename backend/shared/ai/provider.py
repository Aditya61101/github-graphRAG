"""One provider boundary for API, workers and nested ingestion builders."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Protocol, Sequence, Any


class LLM(Protocol):
    deployment: str
    async def ainvoke(self, prompt: str) -> str: ...
    async def extract_structured(self, prompt: str, schema, **kwargs): ...


class Embedder(Protocol):
    dimensions: int
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class ProviderConfig:
    provider: str = "azure"
    project: str = ""
    location: str = ""
    model: str = ""
    embedding_model: str = ""
    dimensions: int = 3072
    thinking_level: str = "LOW"
    isolated_data_id: str = ""
    data_mode: str = "isolated"
    installation_id: str = ""

    def __post_init__(self):
        if self.provider not in {"azure", "google"}:
            raise ValueError("Unsupported AI provider")
        if self.provider == "google" and (
                not all((self.project, self.location, self.model, self.embedding_model))
                or self.data_mode not in {"isolated", "fresh-reset"}
                or (self.data_mode == "isolated" and not self.isolated_data_id)
                or (self.data_mode == "fresh-reset" and (not self.installation_id or self.isolated_data_id))
                or self.dimensions != 3072 or self.thinking_level not in {"LOW", "MEDIUM", "HIGH"}):
            raise ValueError("Invalid Google provider configuration")

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        provider = env.get("AI_PROVIDER", "azure").lower()
        if provider not in {"azure", "google"}:
            raise ValueError("AI_PROVIDER must be azure or google")
        if env.get("AI_GOOGLE_ONLY", "false").lower() == "true" and provider != "google":
            raise ValueError("AI_GOOGLE_ONLY requires AI_PROVIDER=google")
        if provider == "azure":
            return cls(model=env.get("AZURE_OPENAI_DEPLOYMENT_NAME", ""),
                       embedding_model=env.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME", ""))
        if env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY"):
            raise ValueError("Google mode requires ADC; remove Developer API key configuration")
        names = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_AI_MODEL",
                 "GOOGLE_EMBEDDING_MODEL")
        if any(not env.get(n, "").strip() for n in names):
            raise ValueError("Google mode requires explicit Vertex model, project and location")
        mode = env.get("AI_DATA_MODE", "isolated")
        isolated_id = env.get("AI_ISOLATED_DATA_ID", "").strip()
        installation_id = env.get("AI_INSTALLATION_ID", "").strip()
        # These are operator attestations, never permission to reset data. Fresh
        # reuse requires external quiescence/reset/verification in the runbook.
        if mode == "isolated":
            if not isolated_id or env.get("AI_DATA_ISOLATION_CONFIRMED", "false").lower() != "true":
                raise ValueError("Google mode requires a confirmed isolated SQL/Neo4j environment")
            if installation_id or env.get("AI_DATA_RESET_CONFIRMED", "false").lower() == "true":
                raise ValueError("Do not mix isolated and fresh-reset attestations")
        elif mode == "fresh-reset":
            if (not installation_id or isolated_id
                    or env.get("AI_DATA_ISOLATION_CONFIRMED", "false").lower() == "true"
                    or env.get("AI_DATA_RESET_CONFIRMED", "false").lower() != "true"):
                raise ValueError("Fresh-reset mode requires a new installation identity and confirmed reset of BOTH databases; remove isolation attestations")
        else:
            raise ValueError("AI_DATA_MODE must be isolated or fresh-reset")
        level = env.get("GOOGLE_THINKING_LEVEL", "LOW").upper()
        if level not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("Invalid GOOGLE_THINKING_LEVEL")
        dimensions = int(env.get("GOOGLE_EMBEDDING_DIMENSIONS", "3072"))
        if dimensions != 3072:
            raise ValueError("This migration requires 3072-dimensional Google embeddings")
        return cls(provider, env[names[0]], env[names[1]], env[names[2]],
                   env[names[3]], dimensions, level, isolated_id, mode, installation_id)

    @property
    def identity(self):
        values = dict(self.__dict__)
        # Keep existing isolated/Azure fingerprints stable. Reset installations
        # include their unique identity, invalidating durable jobs and caches.
        if self.data_mode == "isolated" and not self.installation_id:
            values.pop("data_mode")
            values.pop("installation_id")
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    @property
    def embedding_space(self):
        return f"vertex:{self.embedding_model}:{self.dimensions}:text-single-v1"


@dataclass
class ProviderServices:
    config: ProviderConfig
    client: Any
    llm: LLM
    embedder: Embedder
    credentials: Any = None
    chat: Any = None

    async def aclose(self):
        if self.config.provider == "google":
            if self.chat is not None:
                await self.chat.client.aio.aclose()
                self.chat.client.close()
            await self.client.aio.aclose()
            self.client.close()
        else:
            await self.client.close()

    def chat_model(self):
        if self.config.provider == "google":
            from .chat import VertexChatModel
            self.chat = VertexChatModel(
                model=self.config.model, vertexai=True, project=self.config.project,
                location=self.config.location, credentials=self.credentials,
                max_retries=0, timeout=120, max_output_tokens=16384,
                thinking_level=self.config.thinking_level.lower(),
            )
            return self.chat
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"], api_version=os.environ["AZURE_OPENAI_API_VERSION"],
            azure_deployment=self.config.model)


def create_provider(config=None):
    config = config or ProviderConfig.from_env()
    if config.provider == "google":
        import google.auth
        from google import genai
        from google.genai import types
        from .vertex import VertexLLM, VertexEmbedder
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        client = genai.Client(vertexai=True, project=config.project, location=config.location,
            credentials=credentials, http_options=types.HttpOptions(
                api_version="v1", timeout=120000,
                retry_options=types.HttpRetryOptions(attempts=1)))
        return ProviderServices(config, client, VertexLLM(client, config), VertexEmbedder(client, config), credentials)
    from openai import AsyncAzureOpenAI
    from shared.utils.llm_utils import AzureOpenAILLM
    from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
    client = AsyncAzureOpenAI(azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"], api_version=os.environ["AZURE_OPENAI_API_VERSION"], max_retries=0)
    return ProviderServices(config, client, AzureOpenAILLM(client, config.model),
        AzureOpenAIEmbedder(client, config.embedding_model,
                           int(os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072"))))


def assert_legacy_allowed():
    if os.environ.get("AI_PROVIDER", "azure").lower() == "google" or os.environ.get("AI_GOOGLE_ONLY", "false").lower() == "true":
        raise ValueError("Non-Google AI transport is disabled in Google mode")


def ingestion_identity():
    config = ProviderConfig.from_env()
    return config.identity if config.provider == "google" else None


def validate_ingestion_identity(run):
    if getattr(run, "provider_identity", None) != ingestion_identity():
        raise ValueError("Ingestion provider changed; create a new run in the configured installation")

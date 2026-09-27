"""
Settings and client factories for the code-graph pipeline.

Everything is read from environment variables (see .env.example), so the
same code runs against Aura, a local Neo4j, Gemini, Azure OpenAI or fully
offline fakes.

    LLM_PROVIDER        gemini | azure | fake            (default: gemini)
    EMBEDDING_PROVIDER  gemini | azure | local | hash    (default: gemini)
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from types import SimpleNamespace


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().strip("'\"")


def required(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"{name} is required. Set it in .env")
    return value


@dataclass
class Settings:
    llm_provider: str = field(default_factory=lambda: env("LLM_PROVIDER", "gemini"))
    embedding_provider: str = field(
        default_factory=lambda: env("EMBEDDING_PROVIDER", "gemini")
    )
    neo4j_database: str | None = field(default_factory=lambda: env("NEO4J_DATABASE"))

    # Tuning knobs (see README "Tuning")
    bridge_top_k: int = field(default_factory=lambda: int(env("BRIDGE_TOP_K", "3")))
    bridge_min_score: float = field(
        default_factory=lambda: float(env("BRIDGE_MIN_SCORE", "0.60"))
    )
    llm_concurrency: int = field(default_factory=lambda: int(env("LLM_CONCURRENCY", "8")))


# --------------------------------------------------------------------------
# Neo4j
# --------------------------------------------------------------------------

def build_driver():
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        required("NEO4J_URI"),
        auth=(required("NEO4J_USERNAME"), required("NEO4J_PASSWORD")),
    )


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------

def build_llm(settings: Settings):
    provider = settings.llm_provider.lower()

    if provider == "gemini":
        from neo4j_graphrag.llm import GeminiLLM

        return GeminiLLM(
            model_name=env("GEMINI_LLM_MODEL", "gemini-2.5-flash"),
            api_key=required("GEMINI_API_KEY"),
        )

    if provider == "azure":
        from neo4j_graphrag.llm import AzureOpenAILLM

        return AzureOpenAILLM(
            model_name=required("AZURE_OPENAI_DEPLOYMENT_NAME"),
            azure_endpoint=required("AZURE_OPENAI_ENDPOINT"),
            api_version=required("OPENAI_API_VERSION"),
            api_key=required("AZURE_OPENAI_API_KEY"),
        )

    if provider == "fake":
        return FakeLLM()

    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


class FakeLLM:
    """Offline stand-in: returns a deterministic 'summary' built from the prompt.

    Lets the whole pipeline run with no API key (tests, demos without network).
    """

    model_name = "fake-llm"

    async def ainvoke(self, prompt: str, *args, **kwargs):
        return SimpleNamespace(content=self._reply(prompt))

    def invoke(self, prompt: str, *args, **kwargs):
        return SimpleNamespace(content=self._reply(prompt))

    @staticmethod
    def _reply(prompt: str) -> str:
        symbol = re.search(r"^Symbol: (.+)$", prompt, re.M)
        module = re.search(r"^Module: (.+)$", prompt, re.M)
        if symbol:
            code = prompt.split("```", 2)[1] if "```" in prompt else ""
            words = sorted(set(re.findall(r"[a-z]{4,}", code.lower())))[:12]
            return (
                f"Implements {symbol.group(1)} using {', '.join(words)}. "
                f"Part of the offline fake summary."
            )
        if module:
            members = re.findall(r"^- ([\w.]+):", prompt, re.M)
            return f"Module {module.group(1)} covers {', '.join(members)}."
        return "Answer (fake LLM): see the context above."


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------

def build_embedder(settings: Settings):
    provider = settings.embedding_provider.lower()

    if provider == "gemini":
        from neo4j_graphrag.embeddings import GeminiEmbedder

        return GeminiEmbedder(
            model=env("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
            embedding_dim=int(env("GEMINI_EMBEDDING_DIMENSIONS", "768")),
            api_key=required("GEMINI_API_KEY"),
        )

    if provider == "azure":
        from neo4j_graphrag.embeddings import AzureOpenAIEmbeddings

        return AzureOpenAIEmbeddings(
            model=required("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"),
            azure_endpoint=required("AZURE_OPENAI_ENDPOINT"),
            api_version=required("OPENAI_API_VERSION"),
            api_key=required("AZURE_OPENAI_API_KEY"),
        )

    if provider == "local":
        from neo4j_graphrag.embeddings import SentenceTransformerEmbeddings

        return SentenceTransformerEmbeddings(
            model=env("LOCAL_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        )

    if provider == "hash":
        return HashEmbedder()

    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {provider}")


class HashEmbedder:
    """Offline bag-of-words embedder (no model download). For tests and demos only."""

    model = "hash-embedder"

    def __init__(self, dims: int = 256):
        self.dims = dims

    def embed_query(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for token in _tokens(text):
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % self.dims] += 1.0 if (h >> 8) % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _tokens(text: str) -> list[str]:
    # split snake_case and camelCase so "verify_password" ~ "verify password"
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    words = re.findall(r"[a-zA-Z]{3,}", text.replace("_", " ").lower())
    stems = [w[:6] for w in words]            # crude stemming: login/logins, token/tokens
    return stems


def model_id(obj) -> str:
    """A stable name for an LLM or embedder, used in cache keys and metadata."""
    for attr in ("model_name", "model", "model_id"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(obj).__name__

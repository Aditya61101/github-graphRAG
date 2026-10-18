"""Bounded adapter over the existing Embedder, with explicit billable approval."""

import asyncio
import math
import time
from dataclasses import dataclass

from ..context import conservative_tokens
from .models import EmbeddingIdentity


@dataclass(frozen=True)
class EmbeddingLimits:
    max_requests: int = 4
    batch_size: int = 8
    max_text_chars: int = 3200
    concurrency: int = 2
    retries: int = 0
    timeout_seconds: float = 15.0
    authorized_billable: bool = False

    def __post_init__(self):
        if (
            any(
                type(v) is not int or v < 1
                for v in (
                    self.max_requests,
                    self.batch_size,
                    self.max_text_chars,
                    self.concurrency,
                )
            )
            or type(self.retries) is not int
            or self.retries < 0
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or type(self.authorized_billable) is not bool
        ):
            raise ValueError("Invalid embedding bounds")


class EmbeddingRunner:
    def __init__(
        self,
        embedder,
        identity: EmbeddingIdentity,
        limits=None,
        token_counter=conservative_tokens,
        request_counter=None,
    ):
        if embedder.dimensions != identity.dimensions:
            raise ValueError("Embedding model/dimension mismatch")
        self.embedder, self.identity = embedder, identity
        self.limits = limits or EmbeddingLimits()
        self.counter = token_counter
        self.request_counter = request_counter
        self.cache = {}
        self.requests = self.estimated_tokens = 0
        self.semaphore = asyncio.Semaphore(self.limits.concurrency)

    async def embed(self, texts, *, deadline=None):
        if (
            self.identity.provenance == "VERIFIED_PROVIDER"
            and not self.limits.authorized_billable
        ):
            raise ValueError("Billable embedding calls require explicit approval")
        if any(len(text) > self.limits.max_text_chars for text in texts):
            raise ValueError("Embedding query length budget")
        missing = list(dict.fromkeys(text for text in texts if text not in self.cache))
        if self.request_counter is not None and self.requests + sum(
                self.request_counter(text) for text in missing) > self.limits.max_requests:
            raise ValueError("Embedding request budget")

        async def batch(items):
            async with self.semaphore:
                for attempt in range(self.limits.retries + 1):
                    request_cost = sum(self.request_counter(text) for text in items) if self.request_counter else 1
                    if self.requests + request_cost > self.limits.max_requests:
                        raise ValueError("Embedding request budget")
                    remaining = (
                        deadline - time.monotonic()
                        if deadline
                        else self.limits.timeout_seconds
                    )
                    if remaining <= 0:
                        raise TimeoutError("Embedding deadline")
                    self.requests += request_cost
                    counts = [self.counter(text) for text in items]
                    if any(type(n) is not int or n < 0 for n in counts):
                        raise ValueError("Invalid embedding token estimator")
                    self.estimated_tokens += sum(counts)
                    try:
                        vectors = await asyncio.wait_for(
                            self.embedder.embed(items),
                            min(remaining, self.limits.timeout_seconds),
                        )
                        if len(vectors) != len(items) or any(
                            len(v) != self.identity.dimensions
                            or not all(math.isfinite(x) for x in v)
                            or not any(v)
                            for v in vectors
                        ):
                            raise ValueError(
                                "Embedding response integrity/dimension mismatch"
                            )
                        for text, vector in zip(items, vectors, strict=True):
                            self.cache[text] = tuple(float(x) for x in vector)
                        return
                    except Exception:
                        if attempt == self.limits.retries:
                            raise

        results = await asyncio.gather(
            *(
                batch(missing[i : i + self.limits.batch_size])
                for i in range(0, len(missing), self.limits.batch_size)
            ),
            return_exceptions=True,
        )
        # Drain all batches before returning; never leave unaccounted billable work.
        error = next((r for r in results if isinstance(r, BaseException)), None)
        if error:
            raise error
        return [self.cache[text] for text in texts]


def azure_runner(client, deployment, dimensions=3072, *, limits=None, revision=None):
    from shared.ai.provider import assert_legacy_allowed
    assert_legacy_allowed()
    from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder

    limits = limits or EmbeddingLimits()
    embedder = AzureOpenAIEmbedder(
        client.with_options(max_retries=0, timeout=limits.timeout_seconds),
        deployment,
        dimensions,
        batch_size=limits.batch_size,
    )
    return EmbeddingRunner(
        embedder,
        EmbeddingIdentity(
            model="text-embedding-3-large",
            dimensions=dimensions,
            revision=revision or "text-embedding-3-large:" + str(dimensions),
        ),
        limits,
    )


def google_runner(embedder, *, limits=None):
    if getattr(embedder, "provider", None) != "google":
        raise ValueError("Google runner requires a Google embedder")
    limits = limits or EmbeddingLimits()
    # One native request per input, counted by the existing request ceiling.
    from dataclasses import replace
    return EmbeddingRunner(embedder, EmbeddingIdentity(provider="google-vertex",
        model=embedder.config.embedding_model, dimensions=embedder.dimensions,
        revision=embedder.identity), replace(limits, batch_size=1),
        request_counter=getattr(embedder, "request_count", None))

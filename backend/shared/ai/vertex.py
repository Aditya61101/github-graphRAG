"""Native Vertex transports. No developer API keys, retries or provider fallback."""
from __future__ import annotations

import asyncio
import math
import time
from google.genai import types
from .embedding_inputs import EMBEDDING_MAX_BYTES, EMBEDDING_INPUT_POLICY, embedding_windows


def vertex_schema(schema):
    """Keep refs/unions/constraints; omit unsupported object closure annotations.

    The ORIGINAL Pydantic model remains the authority after decoding.
    """
    def translate(value):
        if isinstance(value, dict):
            return {k: ({name: translate(child) for name, child in v.items()}
                        if k in {"properties", "$defs", "definitions"} else translate(v))
                    for k, v in value.items() if k not in {"additionalProperties", "$schema", "title"}}
        if isinstance(value, list):
            return [translate(v) for v in value]
        return value
    return translate(schema.model_json_schema())


def response_status(response):
    feedback = getattr(response, "prompt_feedback", None)
    if feedback and getattr(feedback, "block_reason", None):
        return "REFUSAL"
    candidates = getattr(response, "candidates", None) or []
    if len(candidates) != 1:
        return "INVALID"
    reason = getattr(candidates[0].finish_reason, "value", candidates[0].finish_reason)
    if reason in {"MAX_TOKENS", "CONTINUATION"}:
        return "TRUNCATED"
    if reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "MODEL_ARMOR"}:
        return "REFUSAL"
    return "OK" if reason == "STOP" else "INVALID"


class VertexResponseError(ValueError):
    pass


class VertexLLM:
    provider = "google"

    def __init__(self, client, config):
        self.vertex_client, self.config = client, config
        # Deliberately expose no OpenAI-shaped `.client.beta` attribute.
        self.deployment, self.identity = config.model, config.identity

    def generation_config(self, *, schema=None, system=None, max_output_tokens=16384, timeout=120):
        options = dict(system_instruction=system, max_output_tokens=max_output_tokens,
            automatic_continuation=False,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=self.config.thinking_level),
            http_options=types.HttpOptions(timeout=max(1, int(timeout * 1000)),
                retry_options=types.HttpRetryOptions(attempts=1)))
        if schema:
            options.update(response_mime_type="application/json", response_json_schema=vertex_schema(schema))
        return types.GenerateContentConfig(**options)

    async def generate(self, prompt, *, schema=None, system=None, max_output_tokens=16384, timeout=120):
        async with asyncio.timeout(timeout):
            return await self.vertex_client.aio.models.generate_content(model=self.deployment,
                contents=prompt, config=self.generation_config(schema=schema, system=system,
                    max_output_tokens=max_output_tokens, timeout=timeout))

    @staticmethod
    def checked_text(response):
        status = response_status(response)
        if status != "OK":
            raise VertexResponseError(f"Vertex response {status}")
        text = response.text
        if not text or not text.strip():
            raise VertexResponseError("Vertex response EMPTY")
        return text

    async def ainvoke(self, prompt):
        return self.checked_text(await self.generate(prompt))

    async def extract_structured(self, prompt, schema, **kwargs):
        observation = kwargs.pop("observation", None)
        started = time.monotonic()
        response, error = None, None
        try:
            response = await self.generate(prompt, schema=schema, **kwargs)
            return schema.model_validate_json(self.checked_text(response))
        except BaseException as exc:
            error = exc
            raise
        finally:
            if observation is not None:
                from .observability import record_batch
                record_batch(model=self.deployment, observation=observation,
                    prompt=prompt, system=kwargs.get("system"), schema=vertex_schema(schema),
                    elapsed=time.monotonic() - started, response=response, error=error)


class VertexEmbedder:
    provider = "google"
    max_input_bytes = EMBEDDING_MAX_BYTES
    input_policy = EMBEDDING_INPUT_POLICY

    def __init__(self, client, config, concurrency=4):
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 16:
            raise ValueError("Embedding concurrency must be between 1 and 16")
        self.client, self.config = client, config
        self.dimensions, self.identity = config.dimensions, config.embedding_space
        self._semaphore = asyncio.Semaphore(concurrency)

    async def _one(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding input must be nonempty text")
        # Conservative admission bound, without truncation or an Azure tokenizer.
        if len(text.encode("utf-8")) > self.max_input_bytes:
            raise ValueError("Embedding input exceeds conservative 8192 UTF-8-byte bound")
        async with self._semaphore, asyncio.timeout(120):
            response = await self.client.aio.models.embed_content(
                model=self.config.embedding_model, contents=text,
                config=types.EmbedContentConfig(output_dimensionality=self.dimensions,
                    auto_truncate=False))
        items = response.embeddings or []
        if len(items) != 1:
            raise ValueError("Vertex must return exactly one vector per input")
        if getattr(getattr(items[0], "statistics", None), "truncated", False):
            raise ValueError("Vertex truncated embedding input")
        vector = list(items[0].values or [])
        if (len(vector) != self.dimensions or not all(math.isfinite(v) for v in vector)
                or not any(v != 0 for v in vector)):
            raise ValueError("Invalid Vertex embedding dimensions or values")
        return vector

    @staticmethod
    def request_count(text):
        return len(embedding_windows(text))

    async def _windowed(self, windows):
        if len(windows) == 1:
            return await self._one(windows[0])
        vectors = []
        for start in range(0, len(windows), 4):
            batch = await asyncio.gather(*(self._one(t) for t in windows[start:start + 4]),
                return_exceptions=True)
            error = next((v for v in batch if isinstance(v, BaseException)), None)
            if error:
                raise error
            vectors.extend(batch)
        # Each window remains a single native input in the same model space.
        # Preserve all nonblank content; aggregate only the embedding representation.
        weights = [len(text.encode("utf-8")) for text in windows]
        norms = [math.hypot(*vector) for vector in vectors]
        pooled = [math.fsum(vector[j] / norm * weight
            for vector, norm, weight in zip(vectors, norms, weights, strict=True))
            for j in range(self.dimensions)]
        norm = math.hypot(*pooled)
        if not norm or not math.isfinite(norm):
            raise ValueError("Invalid pooled Vertex embedding")
        return [value / norm for value in pooled]

    async def embed(self, texts):
        # Embedding 2 aggregates multiple contents. Never batch contents in one request.
        # Bounded task windows also bound outstanding inputs and preserve order.
        windows = [embedding_windows(text) for text in texts]
        result = []
        for start in range(0, len(windows), 4):
            batch = await asyncio.gather(*(self._windowed(parts) for parts in windows[start:start + 4]), return_exceptions=True)
            error = next((v for v in batch if isinstance(v, BaseException)), None)
            if error:
                raise error
            result.extend(batch)
        return result

    async def embed_query(self, text):
        return (await self.embed([text]))[0]

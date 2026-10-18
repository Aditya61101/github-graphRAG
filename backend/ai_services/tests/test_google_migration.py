"""Offline provider, wire, accounting, isolation and resumable backfill contracts."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google import genai
from google.oauth2.credentials import Credentials
from google.genai import types
from pydantic import BaseModel, ValidationError

from shared.ai.provider import ProviderConfig, ProviderServices, create_provider
from shared.ai.vertex import VertexLLM, VertexEmbedder, vertex_schema
from ai_services.decision_guard.evaluation.vertex_adapter import VertexEvaluationModel
from ai_services.decision_guard.evaluation.models import EvaluationSettings
from ai_services.decision_guard.orchestration.profiles import automatic_evaluation_settings
from ai_services.embeddings.backfill import VectorBackfill, VectorSource
from ai_services.tests.test_response_contract_robustness import data, request_stub


def google_env():
    return dict(AI_PROVIDER="google", AI_GOOGLE_ONLY="true",
        GOOGLE_CLOUD_PROJECT="decisionguard-ai-builder-cup", GOOGLE_CLOUD_LOCATION="global",
        GOOGLE_AI_MODEL="gemini-4-argon", GOOGLE_EMBEDDING_MODEL="gemini-embedding-2",
        GOOGLE_EMBEDDING_DIMENSIONS="3072", AI_ISOLATED_DATA_ID="offline-fixture",
        AI_DATA_ISOLATION_CONFIRMED="true")


def config():
    return ProviderConfig.from_env(google_env())


def response(text=None, finish="STOP", *, candidates=True, usage=True):
    return NS(text=json.dumps(data()) if text is None else text, prompt_feedback=None,
        candidates=[NS(finish_reason=finish)] if candidates else [],
        usage_metadata=NS(prompt_token_count=10, candidates_token_count=5,
            thoughts_token_count=7, cached_content_token_count=2) if usage else None,
        response_id="mock-google-id", model_version="mock-argon-version")


def llm(reply=None):
    client = NS(aio=NS(models=NS(generate_content=AsyncMock(return_value=reply or response()))))
    return VertexLLM(client, config())


@pytest.mark.parametrize("missing", ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_AI_MODEL",
    "GOOGLE_EMBEDDING_MODEL", "AI_ISOLATED_DATA_ID", "AI_DATA_ISOLATION_CONFIRMED"])
def test_missing_configuration_fails_closed(missing):
    env = google_env(); env.pop(missing)
    with pytest.raises(ValueError):
        ProviderConfig.from_env(env)


@pytest.mark.parametrize("changes", [{"AI_PROVIDER": "groq"}, {"AI_PROVIDER": "azure"},
    {"GOOGLE_THINKING_LEVEL": "MINIMAL"},
    {"GOOGLE_API_KEY": "test-key"}, {"GEMINI_API_KEY": "test-key"}, {"GOOGLE_EMBEDDING_DIMENSIONS": "1536"}])
def test_google_only_rejects_non_vertex_configuration(changes):
    with pytest.raises(ValueError):
        ProviderConfig.from_env({**google_env(), **changes})


def test_google_factory_uses_adc_and_never_constructs_azure(monkeypatch):
    adc = MagicMock(return_value=(Credentials(token="offline-test"), "ignored"))
    monkeypatch.setattr("google.auth.default", adc)
    azure = MagicMock(side_effect=AssertionError("Azure cannot execute"))
    monkeypatch.setattr("openai.AsyncAzureOpenAI", azure)
    factory = MagicMock()
    monkeypatch.setattr(genai, "Client", factory)
    services = create_provider(config())
    assert services.llm.provider == services.embedder.provider == "google"
    assert factory.call_args.kwargs["vertexai"] is True
    assert factory.call_args.kwargs["http_options"].retry_options.attempts == 1
    azure.assert_not_called(); adc.assert_called_once()


def test_missing_adc_propagates_without_fallback(monkeypatch):
    monkeypatch.setattr("google.auth.default", MagicMock(side_effect=RuntimeError("ADC unavailable")))
    with pytest.raises(RuntimeError, match="ADC unavailable"):
        create_provider(config())


def test_legacy_adapters_cannot_execute_in_google_mode(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "google")
    from shared.utils.llm_utils import AzureOpenAILLM
    from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
    from ai_services.ingestion.discovery.planner import create_ingestion_plan
    for call in [lambda: AzureOpenAILLM(None, "old"),
                 lambda: AzureOpenAIEmbedder(None, "old", 3072),
                 lambda: create_ingestion_plan(None, "objective", {})]:
        with pytest.raises(ValueError, match="disabled"):
            call()


@pytest.mark.parametrize("finish,expected", [("STOP", "OK"), ("MAX_TOKENS", "TRUNCATED"),
    ("CONTINUATION", "TRUNCATED"),
    ("SAFETY", "REFUSAL"), (None, "INVALID"), ("OTHER", "INVALID")])
async def test_gemini_evaluation_envelopes_and_thinking(finish, expected):
    model = VertexEvaluationModel(llm(response(finish=finish)), deployment_revision="test-v1")
    reply = await model.assess(request_stub(), max_output_tokens=100, timeout=2)
    assert reply.status == expected
    assert reply.input_tokens == 10 and reply.output_tokens == 12 and reply.thinking_tokens == 7
    assert reply.provider == "google" and reply.provider_identity == config().identity
    assert reply.output_json is not None if expected == "OK" else reply.output_json is None


@pytest.mark.parametrize("text,candidates", [("", True), ("{}", True), ("bad JSON", True), ("{}", False)])
async def test_empty_invalid_and_missing_candidate_are_not_success(text, candidates):
    reply = await VertexEvaluationModel(llm(response(text, candidates=candidates)), deployment_revision="v1").assess(
        request_stub(), max_output_tokens=100, timeout=2)
    assert reply.status == "INVALID" and reply.output_json is None


async def test_unknown_role_bound_sources_still_fail_original_validator():
    value = data()
    value["quotes"][0].update(quote_id="A1", source_ref="ADR_DOCUMENT:1")
    value["units"][0]["support"] = ["A1"]
    reply = await VertexEvaluationModel(llm(response(json.dumps(value))), deployment_revision="v1").assess(
        request_stub(), max_output_tokens=100, timeout=2)
    from ai_services.decision_guard.evaluation.models import AssessmentBatchOutput
    expanded = AssessmentBatchOutput.model_validate_json(reply.output_json)
    # Existing validator rejects this unit's support while retaining valid siblings.
    assert not expanded.units[0].support
    assert expanded.units[0].rejections[0].reason == "UNKNOWN_SOURCE_REF"


async def test_unknown_usage_preserves_reservations():
    reply = await VertexEvaluationModel(llm(response(usage=False)), deployment_revision="v1").assess(
        request_stub(), max_output_tokens=100, timeout=2)
    assert reply.input_tokens is None and reply.output_tokens is None


async def test_cancellation_reaches_transport_without_retry():
    model = llm(); started = asyncio.Event(); cancelled = asyncio.Event()
    async def pending(**kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    model.vertex_client.aio.models.generate_content.side_effect = pending
    task = asyncio.create_task(model.ainvoke("source")); await started.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and model.vertex_client.aio.models.generate_content.await_count == 1


def test_schema_translation_preserves_named_fields_and_original_domain():
    class Original(BaseModel):
        title: str
        value: str | int | bool | list[str | int | bool]
    translated = vertex_schema(Original)
    assert "title" in translated["properties"] and "anyOf" in translated["properties"]["value"]
    with pytest.raises(ValidationError):
        Original.model_validate({"value": "x"})


async def test_pinned_native_sdk_serializes_vertex_generation_without_network():
    sent = []
    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
            "content": {"role": "model", "parts": [{"text": json.dumps(data())}]}}]})
    client = genai.Client(vertexai=True, project=config().project, location="global",
        credentials=Credentials(token="offline-test"), http_options=types.HttpOptions(api_version="v1",
            async_client_args={"transport": httpx.MockTransport(handle)}))
    try:
        from ai_services.decision_guard.evaluation.wire import WireAssessment
        result = await VertexLLM(client, config()).extract_structured("source", WireAssessment)
        assert result.batch_id == "batch"
        generation = sent[0]["generationConfig"]
        assert generation["responseMimeType"] == "application/json"
        assert "responseJsonSchema" in generation
        assert generation["thinkingConfig"].get("thinkingLevel", generation["thinkingConfig"].get("thinking_level")) == "LOW"
    finally:
        await client.aio.aclose(); client.close()


async def test_embeddings_are_one_request_per_text_and_ordered():
    seen = []
    async def embed(**kwargs):
        seen.append(kwargs["contents"])
        return NS(embeddings=[NS(values=[float(len(kwargs["contents"]))] * 3072)])
    adapter = VertexEmbedder(NS(aio=NS(models=NS(embed_content=embed))), config())
    vectors = await adapter.embed(["a", "bbb", "cc"])
    assert sorted(seen) == ["a", "bbb", "cc"]
    assert [v[0] for v in vectors] == [1, 3, 2]
    assert await adapter.embed_query("zz") == [2.0] * 3072


async def test_pinned_sdk_embedding_two_uses_single_input_without_task_type():
    sent = []
    def handle(request):
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"embedding": {"values": [1.0]*3072}})
    client = genai.Client(vertexai=True, project=config().project, location="global",
        credentials=Credentials(token="offline-test"), http_options=types.HttpOptions(api_version="v1",
            async_client_args={"transport": httpx.MockTransport(handle)}))
    try:
        assert len(await VertexEmbedder(client, config()).embed(["one", "two"])) == 2
        assert len(sent) == 2
        assert all("taskType" not in json.dumps(body) for _, body in sent)
        assert all("gemini-embedding-2" in url for url, _ in sent)
    finally:
        await client.aio.aclose(); client.close()


def test_google_conversation_adapter_binds_tools_with_vertex_adc():
    services = ProviderServices(config(), None, None, None, Credentials(token="offline-test"))
    chat = services.chat_model()
    assert chat.vertexai is True and chat.thinking_level == "low"
    def search_repository(query: str) -> str:
        """Search the authorized repository."""
        return query
    assert chat.bind_tools([search_repository]) is not None


async def test_google_conversation_request_owns_tools_retries_and_timeout(monkeypatch):
    services = ProviderServices(config(), None, None, None, Credentials(token="offline-test"))
    chat = services.chat_model()
    def search_repository(query: str) -> str:
        """Search the authorized repository."""
        return query
    first_response = types.GenerateContentResponse(candidates=[types.Candidate(
        finish_reason="STOP", content=types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(id="call-1", name="search_repository", args={"query": "architecture"}),
            thought_signature=b"offline-signature")]))])
    final_response = types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="STOP",
        content=types.Content(role="model", parts=[types.Part(text="Grounded answer")]))])
    fake = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(chat.client.aio.models, "generate_content", fake)
    try:
        result = await chat.bind_tools([search_repository]).ainvoke("Find architecture")
        request = fake.call_args.kwargs["config"]
        assert result.tool_calls[0]["name"] == "search_repository"
        assert request.automatic_continuation is False
        assert request.automatic_function_calling.disable is True
        assert request.http_options.retry_options.attempts == 1
        assert request.http_options.timeout == 120000
        assert "candidate_count" not in request.model_dump(exclude_none=True)
        from langchain_core.messages import HumanMessage, ToolMessage
        assert result.tool_calls[0]["id"] == "call-1"
        answer = await chat.bind_tools([search_repository]).ainvoke([
            HumanMessage(content="Find architecture"), result,
            ToolMessage(content="Repository facts", tool_call_id="call-1")])
        assert answer.content == "Grounded answer"
        sent = fake.call_args.kwargs["contents"]
        calls = [part.function_call for content in sent for part in content.parts if part.function_call]
        replies = [part.function_response for content in sent for part in content.parts if part.function_response]
        assert calls[0].id == replies[0].id == "call-1"
        assert next(part for content in sent for part in content.parts if part.function_call).thought_signature == b"offline-signature"
    finally:
        await chat.client.aio.aclose(); chat.client.close()


@pytest.mark.parametrize("vectors", [[], [[1.0]*3072, [2.0]*3072], [[1.0]*1536],
    [[0.0]*3072], [[float("nan")]*3072], [[float("inf")]*3072]])
async def test_embedding_cardinality_dimensions_and_finiteness(vectors):
    call = AsyncMock(return_value=NS(embeddings=[NS(values=v) for v in vectors]))
    adapter = VertexEmbedder(NS(aio=NS(models=NS(embed_content=call))), config())
    with pytest.raises(ValueError):
        await adapter.embed(["source"])
    assert call.await_count == 1


def test_google_profile_identity_is_explicit_and_historical_settings_still_parse():
    settings = automatic_evaluation_settings(google_env())
    assert settings.provider_identity == config().identity and settings.token_encoding == "gemini-utf8-bound-v1"
    assert EvaluationSettings.model_validate_json('{}').provider == "azure"
    with pytest.raises(ValueError):
        EvaluationSettings(provider="google", token_encoding="o200k_base")


async def test_google_tool_response_order_follows_original_calls():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    services = ProviderServices(config(), None, None, None, Credentials(token="offline-test"))
    chat = services.chat_model()
    try:
        request = chat._prepare_request([HumanMessage(content="Find architecture"), AIMessage(content="", tool_calls=[
            {"id": "first", "name": "search_repository", "args": {"query": "first"}},
            {"id": "second", "name": "search_repository", "args": {"query": "second"}}]),
            ToolMessage(content="second facts", tool_call_id="second"),
            ToolMessage(content="first facts", tool_call_id="first")])
        replies = [part.function_response for content in request["contents"] for part in content.parts if part.function_response]
        assert [r.id for r in replies] == ["first", "second"]
        assert [r.response["output"] for r in replies] == ["first facts", "second facts"]
    finally:
        await chat.client.aio.aclose(); chat.client.close()


async def test_backfill_resumes_verified_content_and_never_publishes_partial():
    from dataclasses import replace
    class Store:
        def __init__(self):
            self.items = [VectorSource("a", "original", "h1", False), VectorSource("b", "other", "h2", False)]
            self.ready = False
        def sources(self): return self.items
        def begin(self, *args): self.ready = False
        def save(self, source, vector):
            self.items = [replace(s, completed=True) if s.key == source.key else s for s in self.items]
        def progress(self, *args): pass
        def finish(self, *args): self.ready = True
    store = Store(); calls = 0
    async def embed(texts):
        nonlocal calls
        calls += 1
        if calls == 2: raise TimeoutError("interrupted")
        return [[1.0]*3072]
    embedder = NS(provider="google", embed=embed)
    with pytest.raises(TimeoutError): await VectorBackfill(store, embedder).run()
    assert not store.ready and store.items[0].completed
    await VectorBackfill(store, embedder).run()
    assert store.ready and calls == 3


def test_equal_dimension_azure_vectors_fail_google_readiness(monkeypatch):
    for k, v in google_env().items(): monkeypatch.setenv(k, v)
    from shared.ai.space import assert_vector_space
    driver = NS(execute_query=MagicMock(return_value=NS(records=[{"expected": 10, "completed": 9}])))
    with pytest.raises(ValueError, match="incompatible"):
        assert_vector_space(driver, "isolated", "repo")


def test_global_indexes_cannot_search_azure_vectors_from_another_repository(monkeypatch):
    for k, v in google_env().items(): monkeypatch.setenv(k, v)
    from shared.ai.space import assert_vector_space
    def execute(query, **kwargs):
        if "repository:$repo" in query:
            return NS(records=[{"expected": 1, "completed": 1, "communities": 0}])
        if "SHOW INDEXES" in query:
            return NS(records=[{"name": name, "type": "VECTOR", "state": "ONLINE",
                "labelsOrTypes": [label], "properties": ["embedding"],
                "options": {"indexConfig": {"vector.dimensions": 3072, "vector.similarity_function": "cosine"}}}
                for name, label in [("chunk_vector_index", "Chunk"), ("entity_vector_index", "Entity")]])
        return NS(records=[{"expected": 2, "completed": 1, "communities": 0}])
    driver = NS(execute_query=execute)
    assert_vector_space(driver, "isolated", "repo")
    with pytest.raises(ValueError, match="incompatible"):
        assert_vector_space(driver, "isolated", "repo", global_indexes=True)


@pytest.mark.parametrize("metric", ["COSINE", "cosine", "EUCLIDEAN"])
@pytest.mark.parametrize("index_name,label", [
    ("chunk_vector_index", "Chunk"),
    ("entity_vector_index", "Entity"),
    ("community_vector_index", "Community"),
])
def test_google_vector_index_similarity_case(monkeypatch, metric, index_name, label):
    for key, value in google_env().items():
        monkeypatch.setenv(key, value)
    from shared.ai.space import assert_vector_space
    indexes = [{"name": name, "type": "VECTOR", "state": "ONLINE",
        "labelsOrTypes": [kind], "properties": ["embedding"],
        "options": {"indexConfig": {"vector.dimensions": 3072,
            "vector.similarity_function": metric if name == index_name else "cosine"}}}
        for name, kind in [("chunk_vector_index", "Chunk"),
            ("entity_vector_index", "Entity"), ("community_vector_index", "Community")]]
    def execute(query, **kwargs):
        return NS(records=indexes if "SHOW INDEXES" in query else [
            {"expected": 3, "completed": 3, "communities": 1}])
    driver = NS(execute_query=execute)
    if metric == "EUCLIDEAN":
        with pytest.raises(ValueError, match=index_name):
            assert_vector_space(driver, "isolated", "repo", global_indexes=True)
    else:
        assert_vector_space(driver, "isolated", "repo", global_indexes=True)


@pytest.mark.parametrize("field,value", [
    ("name", "wrong_index"), ("type", "RANGE"), ("state", "POPULATING"),
    ("labelsOrTypes", ["WrongLabel"]), ("properties", ["wrong_property"]),
    ("vector.dimensions", 1536), ("vector.similarity_function", None),
])
def test_google_vector_index_case_fix_preserves_other_checks(monkeypatch, field, value):
    for key, setting in google_env().items():
        monkeypatch.setenv(key, setting)
    from shared.ai.space import assert_vector_space
    indexes = [{"name": name, "type": "VECTOR", "state": "ONLINE",
        "labelsOrTypes": [label], "properties": ["embedding"],
        "options": {"indexConfig": {"vector.dimensions": 3072,
            "vector.similarity_function": "COSINE"}}}
        for name, label in [("chunk_vector_index", "Chunk"), ("entity_vector_index", "Entity")]]
    target = indexes[0]["options"]["indexConfig"] if field.startswith("vector.") else indexes[0]
    target[field] = value
    def execute(query, **kwargs):
        return NS(records=indexes if "SHOW INDEXES" in query else [
            {"expected": 0, "completed": 0, "communities": 0}])
    with pytest.raises(ValueError, match="chunk_vector_index"):
        assert_vector_space(NS(execute_query=execute), "isolated", "repo", global_indexes=True)


def test_packet_keys_preserve_azure_compatibility_and_separate_google():
    from ai_services.decision_guard.orchestration.packets import DurableEvaluationModel
    from ai_services.decision_guard.evaluation.model_adapter import wire_text
    from ai_services.decision_guard.models import digest
    model = NS(deployment_revision="stable")
    azure = EvaluationSettings()
    request = request_stub()
    historical = digest(json.dumps([request.model_dump(mode="json"), wire_text(request, "ASSESS"),
        "stable", azure.max_output_tokens], sort_keys=True))
    azure_key = DurableEvaluationModel(model, None, "job", "lease", azure).key(request, "ASSESS")
    google = automatic_evaluation_settings(google_env())
    google_key = DurableEvaluationModel(model, None, "job", "lease", google).key(request, "ASSESS")
    assert azure_key == historical and google_key != azure_key
    different = google.model_copy(update={"provider_identity": "different-model-config"})
    assert DurableEvaluationModel(model, None, "job", "lease", different).key(request, "ASSESS") != google_key


def test_completed_azure_jobs_cannot_resume_or_deduplicate_as_google():
    from ai_services.decision_guard.orchestration.runtime import ProductionRuntime
    from ai_services.decision_guard.orchestration.store import EvaluationStore
    google = automatic_evaluation_settings(google_env())
    job = NS(settings_json=EvaluationSettings().model_dump_json(), result_json="archived Azure result", stage="EVALUATED")
    runtime = NS(model=NS(provider="google", provider_identity=google.provider_identity))
    with pytest.raises(ValueError, match="provider identity mismatch"):
        ProductionRuntime.validate_job(runtime, job)
    with pytest.raises(ValueError, match="different provider"):
        EvaluationStore._same_provider(job, google)


def test_ingestion_run_identity_is_bound_to_provider_configuration(monkeypatch):
    for k, v in google_env().items(): monkeypatch.setenv(k, v)
    from shared.ai.provider import validate_ingestion_identity
    with pytest.raises(ValueError, match="provider changed"):
        validate_ingestion_identity(NS(provider_identity=None))
    validate_ingestion_identity(NS(provider_identity=config().identity))


def test_community_embedding_reuse_changes_with_provider():
    from ai_services.ingestion.community.embedder import compute_embedding_version_hash
    assert compute_embedding_version_hash("summary", True) != compute_embedding_version_hash(
        "summary", True, config().embedding_space)


async def test_native_sdk_retry_is_disabled_on_rate_limit():
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "offline"}})
    client = genai.Client(vertexai=True, project=config().project, location="global",
        credentials=Credentials(token="offline-test"), http_options=types.HttpOptions(api_version="v1",
            async_client_args={"transport": httpx.MockTransport(handle)}))
    try:
        from google.genai.errors import ClientError
        with pytest.raises(ClientError):
            await VertexLLM(client, config()).ainvoke("source")
        assert len(sent) == 1
    finally:
        await client.aio.aclose(); client.close()


async def test_native_sdk_does_not_follow_continuation_tokens():
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"candidates": [{"finishReason": "CONTINUATION",
            "continuationToken": "b2ZmbGluZQ==", "content": {"role": "model", "parts": [{"text": "partial"}]}}]})
    client = genai.Client(vertexai=True, project=config().project, location="global",
        credentials=Credentials(token="offline-test"), http_options=types.HttpOptions(api_version="v1",
            async_client_args={"transport": httpx.MockTransport(handle)}))
    try:
        from shared.ai.vertex import response_status
        reply = await VertexLLM(client, config()).generate("source")
        assert response_status(reply) == "TRUNCATED" and len(sent) == 1
    finally:
        await client.aio.aclose(); client.close()


async def test_stale_backfill_sources_never_publish_ready():
    class Store:
        ready = False
        def __init__(self): self.item = VectorSource("a", "source", "old", False)
        def sources(self): return [self.item]
        def begin(self, *args): pass
        def save(self, *args): self.item = VectorSource("a", "changed", "new", True)
        def progress(self, *args): pass
        def finish(self, *args): self.ready = True
    store = Store()
    with pytest.raises(ValueError, match="sources changed"):
        await VectorBackfill(store, NS(provider="google", embed=AsyncMock(return_value=[[1.0]*3072]))).run()
    assert not store.ready


def test_backfill_checkpoint_hashes_exclude_vector_metadata():
    from ai_services.embeddings.backfill import Neo4jVectorBackfillStore
    row = {"key": "chunk", "labels": ["Chunk"], "props": {"text": "original", "repository": "repo"}}
    first = Neo4jVectorBackfillStore._source(row, config().embedding_space)
    row["props"].update(embedding=[1.0]*3072, embeddingSpace=config().embedding_space,
        embeddingSourceHash=first.source_hash, embeddingTemplate="backfill-text-v1",
        embedding_provider="google-vertex", embedding_text_hash="text-hash")
    complete = Neo4jVectorBackfillStore._source(row, config().embedding_space)
    assert complete.completed and complete.source_hash == first.source_hash
    row["props"]["text"] = "changed"
    assert not Neo4jVectorBackfillStore._source(row, config().embedding_space).completed

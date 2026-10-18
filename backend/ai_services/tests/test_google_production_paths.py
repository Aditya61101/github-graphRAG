"""Actual ingestion adapter paths with frozen native Vertex responses only."""
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace as NS

import pytest

from ai_services.tests.test_google_migration import config, llm, response


async def test_nested_rkg_builder_uses_supplied_google_services(tmp_path):
    from ai_services.ingestion.rkg.builders.knowledge_pipeline import build_knowledge_pipeline
    from ai_services.ingestion.rkg.models import EvidenceChunk, ChunkStrategy
    transport = llm(response(json.dumps({"entities": [{"label": "Service", "name": "API",
        "properties": [], "source_chunk_refs": ["C1"]}], "relationships": []})))
    embedder = NS(provider="google", dimensions=3072, identity=config().embedding_space)
    pipeline = build_knowledge_pipeline(tmp_path / "candidates.jsonl", llm=transport, embedder=embedder)
    assert pipeline.embedder is embedder and pipeline.extractor.llm is transport
    chunk = EvidenceChunk.create("repo", "commit", "api.py", 0, "class API: pass", ChunkStrategy.WHOLE_FILE)
    extracted = await pipeline.extractor.extract([chunk])
    assert extracted[0].entities[0].source_chunk_ids == [chunk.chunk_id]
    call = transport.vertex_client.aio.models.generate_content.call_args.kwargs
    assert call["model"] == config().model and call["config"].response_mime_type == "application/json"


async def test_google_planner_preserves_manifest_schema():
    from ai_services.ingestion.discovery.planner import create_vertex_ingestion_plan
    from ai_services.ingestion.sources.classifier import classify_file_for_ingestion
    from ai_services.models.ingestion_plan import IngestionPlan
    output = IngestionPlan(files=[classify_file_for_ingestion("service.py")])
    transport = llm(response(output.model_dump_json()))
    result = await create_vertex_ingestion_plan(transport, "architecture", {"files": [{"path": "service.py"}]})
    assert result == output
    assert transport.vertex_client.aio.models.generate_content.call_args.kwargs["model"] == config().model


async def test_google_adr_extraction_separates_system_instructions():
    from ai_services.ingestion.adr.extractor import ADRArchitecturalExtractor
    from ai_services.ingestion.adr.models import ADRChunk
    transport = llm(response('{"decisions":[],"constraints":[],"entities":[],"relationships":[]}'))
    chunk = ADRChunk.create("a", "repo", "Decision", 0, "Use events")
    result = await ADRArchitecturalExtractor(transport).extract_chunk(chunk)
    assert not result.decisions
    call = transport.vertex_client.aio.models.generate_content.call_args.kwargs
    assert call["model"] == config().model
    assert "UNTRUSTED" in call["config"].system_instruction


def test_google_conversation_text_blocks_preserve_answer_contract():
    from ai_services.agents.rag_agent.agent import RAGQueryAgent
    result = RAGQueryAgent._extract_result({"messages": [NS(type="ai", content=[
        {"type": "thinking", "thinking": "private"}, {"type": "text", "text": "Grounded answer"}, " text"])]})
    assert result.answer == "Grounded answer text"


async def test_truncated_embedding_is_explicitly_rejected():
    from shared.ai.vertex import VertexEmbedder
    client = NS(aio=NS(models=NS(embed_content=AsyncMock(return_value=NS(embeddings=[
        NS(values=[1.0]*3072, statistics=NS(truncated=True))])))))
    with pytest.raises(ValueError, match="truncated"):
        await VertexEmbedder(client, config()).embed(["text"])


async def test_google_community_summaries_use_native_structured_output():
    from ai_services.ingestion.community.summarizer import LLMCommunitySummarizer
    from ai_services.ingestion.community.models import CommunityContext, CommunityMember
    transport = llm(response(json.dumps({"architectural_role": "API boundary", "summary": "API handles requests",
        "key_entities": ["API"]})))
    context = CommunityContext(community_id="c", members=[CommunityMember(entity_id="e", entity_name="API")])
    summarizer = LLMCommunitySummarizer(transport)
    summary = await summarizer.summarize(context)
    assert summary.key_entities == ["API"] and summary.context_hash != context.context_hash()
    assert transport.vertex_client.aio.models.generate_content.call_args.kwargs["config"].response_mime_type == "application/json"

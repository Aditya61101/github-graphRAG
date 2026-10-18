"""Offline regressions for source preservation and bounded native embeddings."""
import asyncio
import hashlib
import math
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from shared.ai.embedding_inputs import embedding_windows, text_windows, EMBEDDING_INPUT_POLICY
from shared.ai.vertex import VertexEmbedder
from ai_services.ingestion.rkg.models import ChunkStrategy, EvidenceChunk
from ai_services.ingestion.rkg.splitters import RepositoryTextSplitter
from ai_services.ingestion.rkg.pipeline import RepositoryKnowledgePipeline, PipelineConfig
from ai_services.tests.test_google_migration import config


def adapter():
    seen = []
    async def embed(**kwargs):
        text = kwargs["contents"]
        assert text.strip() and len(text.encode("utf-8")) <= 8192
        assert kwargs["config"].auto_truncate is False
        seen.append(text)
        await asyncio.sleep(0)
        first = 1.0 if text[0] in "a界" else 0.0
        return NS(embeddings=[NS(values=[first, 1.0 - first] + [0.0] * 3070)])
    return VertexEmbedder(NS(aio=NS(models=NS(embed_content=embed))), config()), seen


@pytest.mark.parametrize("strategy", list(ChunkStrategy))
@pytest.mark.parametrize("text", ["", " \n\t"])
def test_empty_source_files_emit_no_chunks_for_any_planner_strategy(strategy, text):
    assert RepositoryTextSplitter().split("repo", "sha", "users.py", text, strategy, "python") == []


@pytest.mark.parametrize("text", ["a" * 25001, "界" * 10000, "😀" * 5000,
    "header\n" + "x" * 20000 + "\ntail\n"], ids=["ascii", "cjk", "emoji", "long-line"])
def test_oversized_source_chunks_are_lossless_bounded_and_deterministic(text):
    splitter = RepositoryTextSplitter(max_chunk_bytes=8192)
    chunks = splitter.split("repo", "sha", "source.txt", text, ChunkStrategy.WHOLE_FILE)
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(0 < len(c.text.encode("utf-8")) <= 8192 for c in chunks)
    assert chunks == splitter.split("repo", "sha", "source.txt", text, ChunkStrategy.WHOLE_FILE)
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        meta = chunk.metadata
        assert chunk.text == text[meta["source_char_start"]:meta["source_char_end"]]
        assert chunk.content_hash == hashlib.sha256(chunk.text.encode()).hexdigest()
        assert (chunk.repository, chunk.commit, chunk.file_path) == ("repo", "sha", "source.txt")


def test_overflow_source_line_provenance_is_exact():
    text = "alpha\nbeta\ngamma\n"
    source = EvidenceChunk.create("repo", "sha", "x.py", 0, text, ChunkStrategy.SYMBOL,
        {"start_line": 10, "end_line": 12, "language": "python"})
    chunks = RepositoryTextSplitter(max_chunk_chars=8)._cap_chunks([source])
    assert "".join(c.text for c in chunks) == text
    assert [(c.metadata["start_line"], c.metadata["end_line"]) for c in chunks] == [(10, 10), (11, 11), (12, 12)]
    assert all(c.metadata["overflow_of_chunk"] == source.chunk_id for c in chunks)


@pytest.mark.parametrize("text", ["界" * 4000, "😀" * 3000, "a" * 8192 + "b" * 500], ids=["cjk", "emoji", "ascii"])
def test_embedding_windows_cover_every_unicode_character(text):
    windows = embedding_windows(text)
    assert "".join(windows) == text
    assert all(0 < len(w.encode()) <= 8192 for w in windows)


@pytest.mark.parametrize("bad", ["", " \n\t", None, "a" * (8192 * 32 + 1)], ids=["empty", "blank", "nontext", "overbudget"])
async def test_invalid_inputs_fail_before_any_native_request(bad):
    embedder, seen = adapter()
    with pytest.raises(ValueError):
        await embedder.embed(["valid", bad])
    assert seen == []


async def test_windowing_preserves_logical_order_cardinality_and_short_vectors():
    embedder, seen = adapter()
    long = "a" * 8192 + "b" * 4096
    vectors = await embedder.embed(["b", long, "a", "界" * 4000])
    assert len(vectors) == 4 and all(len(v) == 3072 for v in vectors)
    assert vectors[0] == [0.0, 1.0] + [0.0] * 3070
    assert vectors[2] == [1.0, 0.0] + [0.0] * 3070
    assert vectors[1][0] / vectors[1][1] == pytest.approx(2.0)
    assert math.hypot(*vectors[1]) == pytest.approx(1.0)
    assert len(seen) == sum(embedder.request_count(t) for t in ["b", long, "a", "界" * 4000])
    assert embedder.input_policy == EMBEDDING_INPUT_POLICY


async def test_native_request_still_rejects_oversized_text():
    embedder, seen = adapter()
    with pytest.raises(ValueError, match="8192"):
        await embedder._one("a" * 8193)
    assert not seen


async def test_google_query_budget_counts_each_native_window():
    from ai_services.decision_guard.hybrid.embedding import google_runner, EmbeddingLimits
    embedder, seen = adapter()
    limits = EmbeddingLimits(max_requests=1, authorized_billable=True)
    runner = google_runner(embedder, limits=limits)
    with pytest.raises(ValueError, match="request budget"):
        await runner.embed(["😀" * 2500])
    assert not seen and runner.requests == 0
    runner = google_runner(embedder, limits=EmbeddingLimits(max_requests=2, authorized_billable=True))
    result = await runner.embed(["😀" * 2500])
    assert len(result) == 1 and runner.requests == 2


async def test_unicode_adr_authority_and_reconstructed_chunk_ids_unchanged():
    from ai_services.models.adr_document import ADRDocument
    from ai_services.ingestion.adr.chunker import ADRChunker
    doc = ADRDocument(adr_id="adr", repository_id="repo", title="Unicode",
        content="😀" * 2500, source_name="decision.md", file_path="decision.md", content_hash="h")
    chunks = ADRChunker().chunk(doc)
    original = [(c.chunk_id, c.text, c.content_hash) for c in chunks]
    embedder, _ = adapter()
    vectors = await embedder.embed([c.text for c in chunks])
    assert len(vectors) == len(chunks)
    assert [(c.chunk_id, c.text, c.content_hash) for c in ADRChunker().chunk(doc)] == original
    assert doc.content == "😀" * 2500


async def test_large_entity_and_community_descriptions_are_not_truncated():
    from ai_services.ingestion.community.embedder import build_embedding_text
    entity = NS(name="Component", label="Service", aliases=[], properties={"description": "界" * 4000})
    summary = NS(architectural_role="API", summary="😀" * 2000, key_entities=["Component"])
    texts = [RepositoryKnowledgePipeline._entity_embedding_text(entity), build_embedding_text(summary)]
    embedder, seen = adapter()
    assert len(await embedder.embed(texts)) == 2
    assert "".join(embedding_windows(texts[0])) == texts[0]
    assert "".join(embedding_windows(texts[1])) == texts[1]
    assert all(len(t.encode()) <= 8192 for t in seen)


async def test_source_embedding_cardinality_mismatch_fails_explicitly():
    chunks = [EvidenceChunk.create("repo", "sha", "x", 0, "text", ChunkStrategy.WHOLE_FILE)]
    pipeline = NS(embedder=NS(embed=AsyncMock(return_value=[])), config=PipelineConfig())
    with pytest.raises(ValueError, match="number of vectors"):
        await RepositoryKnowledgePipeline._embed_chunks(pipeline, chunks)


async def test_empty_chunk_defense_reports_only_identity_not_source():
    chunk = EvidenceChunk.create("repo", "sha", "empty.txt", 0, " \t", ChunkStrategy.WHOLE_FILE)
    pipeline = NS(embedder=NS(embed=AsyncMock()), config=PipelineConfig())
    with pytest.raises(ValueError, match=chunk.chunk_id):
        await RepositoryKnowledgePipeline._embed_chunks(pipeline, [chunk])
    pipeline.embedder.embed.assert_not_called()


def test_empty_query_is_rejected_at_request_boundary(monkeypatch):
    from api_services.app.models.query import QueryRequest
    monkeypatch.setenv("AI_PROVIDER", "google")
    with pytest.raises(ValueError, match="nonempty"):
        QueryRequest(query=" \t\n", conversation_id="c")
    with pytest.raises(ValueError, match="window budget"):
        QueryRequest(query="x" * (8192 * 32 + 1), conversation_id="c")
    text = "😀" * 2500
    assert QueryRequest(query=text, conversation_id="c").query == text


def test_google_repository_builder_applies_byte_cap(tmp_path):
    from ai_services.ingestion.rkg.builders.repo_ingestion_pipeline import build_repository_ingestion_pipeline
    pipeline = build_repository_ingestion_pipeline(tmp_path, "repo", "name", "sha", NS(),
        tmp_path / "candidates.jsonl", llm=NS(provider="google", identity="fixture"),
        embedder=NS(provider="google", dimensions=3072))
    assert pipeline.splitter.max_chunk_bytes == 6000
    assert pipeline.knowledge_pipeline.batcher.config.max_tokens == 6000
    assert pipeline.knowledge_pipeline.batcher.config.max_chunks == 8


def test_overflow_then_short_symbol_retains_unambiguous_source_order():
    source = [EvidenceChunk.create("repo", "sha", "x.py", i, text, ChunkStrategy.SYMBOL)
        for i, text in enumerate(["a" * 9000, "following symbol"])]
    chunks = RepositoryTextSplitter(max_chunk_bytes=8192)._cap_chunks(source)
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    assert "".join(c.text for c in sorted(chunks, key=lambda c: c.chunk_index)) == "".join(c.text for c in source)
    assert chunks[-1].metadata["reindexed_from_chunk"] == source[-1].chunk_id


@pytest.mark.parametrize("values", [[], [0.0] * 3072, [float("nan")] * 3072],
    ids=["missing", "zero", "nonfinite"])
async def test_windowing_still_rejects_invalid_native_vectors(values):
    client = NS(aio=NS(models=NS(embed_content=AsyncMock(return_value=NS(
        embeddings=[NS(values=values)])))))
    with pytest.raises(ValueError, match="Invalid Vertex embedding"):
        await VertexEmbedder(client, config()).embed(["a" * 9000])

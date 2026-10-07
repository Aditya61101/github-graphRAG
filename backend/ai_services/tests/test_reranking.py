import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_services.retrievers.evidence import (
    DiscoverySignal, EvidenceCandidate, RerankedEvidence, budget_candidates,
    deduplicate_candidates, select_evidence,
)
from ai_services.retrievers.hybrid_retrievers import hybrid_retrieve
from ai_services.retrievers.settings import RetrievalSettings
from ai_services.agents.rag_agent.tools.query_tool import create_query_graph_rag_tool
from ai_services.graph import edge_graph_id, entity_graph_id
from ai_services.graph.service import RepositoryGraphService
from ai_services.agents.rag_agent.dataclasses.result import RAGAgentResult
from api_services.app.utils.query_response_mapper import build_api_response


def candidate(cid, *, repository="repo", channel="direct_chunk", rank=1):
    return EvidenceCandidate(
        cid, f"private evidence {cid}", f"{cid}.py", repository,
        discovery_signals=(DiscoverySignal(channel, rank, 0.8),),
    )


class FakeReranker:
    def __init__(self, scores=None):
        self.scores = scores if scores is not None else {"B": 0.95, "A": 0.1, "C": 0.2}
        self.calls = []

    def rerank(self, query, candidates):
        self.calls.append((query, candidates))
        return [
            RerankedEvidence(c, self.scores.get(c.chunk_id, 0.0), i)
            for i, c in enumerate(reversed(candidates), 1)
        ]


def search(items):
    return SimpleNamespace(search=MagicMock(return_value=SimpleNamespace(items=items)))


def hit(content, **metadata):
    return SimpleNamespace(content=content, metadata=metadata)


class EvidenceDriver:
    def __init__(self):
        self.calls = []
        self.discovered = [
            {
                "chunk_id": "B", "repository": "repo", "file_path": "B.py", "text": "private evidence B",
                "signals": [
                    {"channel": "entity", "rank": 1, "score": 0.8, "id": "E1"},
                    {"channel": "community", "rank": 1, "score": 0.9, "id": "comm"},
                ],
            }
        ]
        self.associations = {
            "B": {
                "chunk_id": "B", "repository": "repo", "entity_ids": ["E1", "E2"],
                "raw_assertion_records": [{
                    "source_id": "E1", "target_id": "E2", "relationship": "CALLS",
                    "assertion_id": "X",
                }],
                "raw_direct_records": [],
            },
            "A": {
                "chunk_id": "A", "repository": "repo", "entity_ids": ["E3"],
                "raw_assertion_records": [{
                    "source_id": "E1", "target_id": "E3", "relationship": "USES",
                    "assertion_id": "Y",
                }],
                "raw_direct_records": [],
            },
        }

    def execute_query(self, query, **params):
        self.calls.append((query, params))
        if "entity_hits" in params:
            return self.discovered, None, None
        return [
            self.associations[cid] for cid in params.get("chunk_ids", [])
            if cid in self.associations
        ], None, None


def retrieval_kwargs(driver=None, reranker=None):
    return dict(
        query="effective standalone middleware question",
        driver=driver or EvidenceDriver(), database="neo4j",
        embedder=SimpleNamespace(embed=AsyncMock(return_value=[[0.1, 0.2]])),
        entity_retriever=search([hit("unselected entity description", canonical_id="E1", repository="repo", score=0.9)]),
        community_retriever=search([hit("unselected whole community", community_id="comm", repository="repo", score=0.9)]),
        chunk_retriever=search([
            hit(f"private evidence {cid}", chunk_id=cid, repository="repo", file_path=f"{cid}.py", score=0.8)
            for cid in ["A", "B", "C"]
        ]),
        repository_id="repo", reranker=reranker or FakeReranker(),
        settings=RetrievalSettings(rerank_min_score=0.5),
    )


@pytest.mark.asyncio
async def test_selected_evidence_controls_context_sources_and_graph():
    kwargs = retrieval_kwargs()
    result = await hybrid_retrieve(**kwargs)
    assert [r.candidate.chunk_id for r in result.selected_evidence] == ["B"]
    assert "private evidence B" in result.context
    for rejected in ["private evidence A", "private evidence C", "unselected entity", "whole community"]:
        assert rejected not in result.context
    assert [s["chunk_id"] for s in result.sources["chunks"]] == ["B"]
    assert result.sources["chunks"][0]["score"] == 0.95
    assert result.sources["chunks"][0]["score_type"] == "reranker_relevance"
    assert result.graph_context.node_ids == ["entity:E1", "entity:E2"]
    assert result.graph_context.edge_ids == ["edge:E1:CALLS:E2"]
    assert result.graph_context.assertion_ids == ["assertion:X"]
    assert all("E3" not in value for value in result.graph_context.node_ids)
    # Exactly one discovery query and one selected-only association query.
    assert len(kwargs["driver"].calls) == 2
    assert kwargs["driver"].calls[1][1]["chunk_ids"] == ["B"]
    kwargs["embedder"].embed.assert_awaited_once()
    response = build_api_response(RAGAgentResult("answer", result.sources, result.graph_context), "thread")
    assert [s.file_path for s in response.sources] == ["B.py"]
    assert response.sources[0].score_type == "reranker_relevance"


@pytest.mark.asyncio
async def test_reranker_gets_one_chunk_for_three_discovery_channels():
    kwargs = retrieval_kwargs()
    await hybrid_retrieve(**kwargs)
    query, candidates = kwargs["reranker"].calls[0]
    assert query == kwargs["query"]
    assert len(candidates) == len({c.chunk_id for c in candidates}) == 3
    b = next(c for c in candidates if c.chunk_id == "B")
    assert {s.channel for s in b.discovery_signals} == {"entity", "community", "direct_chunk"}


@pytest.mark.parametrize("threshold,cap,expected", [
    (0.5, 5, ["B"]), (0.0, 2, ["B", "C"]), (0.99, 5, []), (0.1, 1, ["B"]),
])
def test_threshold_order_and_cap(threshold, cap, expected):
    candidates = [candidate(cid) for cid in ["A", "B", "C"]]
    ranked = FakeReranker().rerank("question", candidates)
    selected = select_evidence(ranked, candidates, min_score=threshold, max_evidence=cap)
    assert [item.candidate.chunk_id for item in selected] == expected
    assert [item.rank for item in selected] == list(range(1, len(expected) + 1))


def test_budget_deduplication_and_order_are_deterministic():
    inputs = [candidate("C", rank=3), candidate("B", rank=2), candidate("B", channel="entity", rank=1), candidate("A")]
    unique = deduplicate_candidates(inputs, "repo")
    assert [c.chunk_id for c in budget_candidates(unique, 2)] == ["B", "A"]
    assert budget_candidates(unique, 2) == budget_candidates(list(reversed(unique)), 2)
    tied = [RerankedEvidence(c, 0.8, 1) for c in unique]
    assert [r.candidate.chunk_id for r in select_evidence(tied, unique, min_score=0, max_evidence=5)] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_no_evidence_does_not_force_a_source_or_highlight():
    kwargs = retrieval_kwargs(reranker=FakeReranker({}))
    result = await hybrid_retrieve(**kwargs)
    assert "insufficient" in result.context
    assert result.sources == {"chunks": []}
    assert result.graph_context.to_dict() == {"node_ids": [], "edge_ids": [], "assertion_ids": []}
    assert len(kwargs["driver"].calls) == 1


@pytest.mark.asyncio
async def test_repository_isolation_rejects_missing_metadata_in_all_channels():
    kwargs = retrieval_kwargs()
    for key, id_key in [
        ("chunk_retriever", "chunk_id"), ("entity_retriever", "canonical_id"), ("community_retriever", "community_id"),
    ]:
        kwargs[key].search.return_value.items.extend([
            hit("foreign", **{id_key: "foreign", "repository": "other"}),
            hit("legacy", **{id_key: "legacy"}),
        ])
    kwargs["driver"].discovered.extend([
        {"chunk_id": "foreign", "repository": "other", "text": "foreign"},
        {"chunk_id": "legacy", "text": "legacy"},
    ])
    result = await hybrid_retrieve(**kwargs)
    assert {c.chunk_id for c in kwargs["reranker"].calls[0][1]} == {"A", "B", "C"}
    assert [s["repository"] for s in result.sources["chunks"]] == ["repo"]
    discovery_params = kwargs["driver"].calls[0][1]
    assert [h["id"] for h in discovery_params["entity_hits"]] == ["E1"]
    assert [h["id"] for h in discovery_params["community_hits"]] == ["comm"]
    assert all(params["repo_id"] == "repo" for _, params in kwargs["driver"].calls)


@pytest.mark.asyncio
async def test_tool_keeps_metadata_out_of_prompt_and_uses_effective_query():
    kwargs = retrieval_kwargs()
    tool = create_query_graph_rag_tool(
        **{key: kwargs[key] for key in ["driver", "database", "embedder", "entity_retriever", "community_retriever", "chunk_retriever", "reranker"]},
        retrieval_settings=kwargs["settings"],
    )
    command = await tool.coroutine(
        query=kwargs["query"], runtime=SimpleNamespace(state={"repository_id": "repo"}, tool_call_id="call"),
    )
    text = command.update["messages"][0].content
    assert "private evidence B" in text
    assert all(key not in text for key in ["node_ids", "edge_ids", "assertion_ids", "graph_context", "private evidence A"])
    assert command.update["retrieval_graph_context"]["assertion_ids"] == ["assertion:X"]
    assert kwargs["reranker"].calls[0][0] == kwargs["query"]
    with pytest.raises(ValueError, match="authorized"):
        await tool.coroutine(query="q", repository_id="other", runtime=SimpleNamespace(state={"repository_id": "repo"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion_backed", [False, True])
async def test_query_graph_ids_match_real_graph_service_mapping(assertion_backed):
    relationship = {"source_id": "E1", "target_id": "E2", "rel_type": "CALLS", "assertion_id": "X"}
    graph_repo = SimpleNamespace(
        fetch_entities=lambda repos: [{"id": "E1"}, {"id": "E2"}],
        fetch_direct_relationships=lambda repos: [] if assertion_backed else [relationship],
        fetch_assertion_relationships=lambda repos: [relationship] if assertion_backed else [],
    )
    repo = SimpleNamespace(id="repo", github_repository_id=None, user_id="user", github_connection=None)
    service = RepositoryGraphService(SimpleNamespace(get_repository=lambda rid: repo), graph_repo)
    graph = await service.get_repository_graph("repo", "user")
    result = await hybrid_retrieve(**retrieval_kwargs())
    assert result.graph_context.node_ids == [node.id for node in graph.nodes]
    assert result.graph_context.edge_ids == [edge.id for edge in graph.edges]


@pytest.mark.asyncio
async def test_direct_edge_only_highlighted_by_its_selected_chunk_provenance():
    kwargs = retrieval_kwargs()
    association = kwargs["driver"].associations["B"]
    association["raw_assertion_records"] = []
    association["raw_direct_records"] = [{"source_id": "E1", "target_id": "E2", "relationship": "CALLS"}]
    result = await hybrid_retrieve(**kwargs)
    assert result.graph_context.edge_ids == [edge_graph_id("E1", "CALLS", "E2")]
    assert result.graph_context.assertion_ids == []
    query = kwargs["driver"].calls[1][0]
    assert "c.id IN coalesce(r.evidence_chunk_ids, [])" in query
    assert "[:SUPPORTS]->(a:GraphAssertion)" in query


@pytest.mark.asyncio
async def test_reranker_failure_propagates():
    kwargs = retrieval_kwargs()
    kwargs["reranker"] = SimpleNamespace(rerank=MagicMock(side_effect=RuntimeError("inference failure")))
    with pytest.raises(RuntimeError, match="inference failure"):
        await hybrid_retrieve(**kwargs)


def test_settings_environment_and_validation(monkeypatch):
    monkeypatch.setenv("RERANK_MIN_SCORE", "0.7")
    monkeypatch.setenv("DIRECT_CHUNK_TOP_K", "40")
    settings = RetrievalSettings.from_env()
    assert settings.rerank_min_score == 0.7
    assert settings.direct_chunk_top_k == 40
    with pytest.raises(ValueError):
        RetrievalSettings(rerank_max_candidates=0)
    with pytest.raises(ValueError):
        RetrievalSettings(rerank_min_score=float("nan"))


def test_evaluation_hook_accepts_swappable_reranker():
    from ai_services.retrievers.evaluation import evaluate_reranker
    result = evaluate_reranker(
        "q", [candidate("A"), candidate("B")], {"B"}, FakeReranker(), RetrievalSettings(),
    )
    assert result.selected_ids == ("B",)
    assert result.candidate_recall == result.evidence_recall == result.selected_precision == result.ndcg == 1.0
    assert result.reranking_duration_ms >= 0


def test_reranker_cannot_inject_unknown_evidence_or_invalid_scores():
    known = candidate("B")
    with pytest.raises(ValueError, match="unknown"):
        select_evidence([RerankedEvidence(candidate("Z"), 1, 1)], [known], min_score=0, max_evidence=5)
    with pytest.raises(ValueError, match="non-finite"):
        select_evidence([RerankedEvidence(known, float("nan"), 1)], [known], min_score=0, max_evidence=5)
    with pytest.raises(ValueError, match="every"):
        select_evidence([], [known], min_score=0, max_evidence=5)


@pytest.mark.asyncio
async def test_middleware_query_returns_only_setup_source():
    kwargs = retrieval_kwargs()
    paths = {"A": "backend/app/api.rest", "B": "backend/app/setup.py", "C": "backend/alembic/README"}
    for item in kwargs["chunk_retriever"].search.return_value.items:
        item.metadata["file_path"] = paths[item.metadata["chunk_id"]]
    kwargs["driver"].discovered[0]["file_path"] = paths["B"]
    result = await hybrid_retrieve(**kwargs)
    assert [source["file_path"] for source in result.sources["chunks"]] == [paths["B"]]
    assert paths["B"] in result.context
    assert paths["A"] not in result.context and paths["C"] not in result.context


@pytest.mark.asyncio
async def test_entity_hit_without_chunk_evidence_cannot_highlight_a_node():
    kwargs = retrieval_kwargs()
    kwargs["driver"].discovered = []
    kwargs["chunk_retriever"] = None
    result = await hybrid_retrieve(**kwargs)
    assert result.graph_context.node_ids == []
    assert result.sources["chunks"] == []
    assert kwargs["reranker"].calls == []


@pytest.mark.asyncio
async def test_inference_runs_off_event_loop_thread():
    import threading
    event_thread = threading.get_ident()
    kwargs = retrieval_kwargs()
    original = kwargs["reranker"].rerank
    def rerank(query, candidates):
        assert threading.get_ident() != event_thread
        return original(query, candidates)
    kwargs["reranker"].rerank = rerank
    await hybrid_retrieve(**kwargs)


@pytest.mark.asyncio
async def test_legacy_vector_filter_fallback_still_requires_matching_repository():
    from neo4j_graphrag.exceptions import SearchValidationError
    kwargs = retrieval_kwargs()
    direct = kwargs["chunk_retriever"]
    original = direct.search.return_value
    original.items.extend([
        hit("legacy", chunk_id="legacy"),
        hit("foreign", chunk_id="foreign", repository="other"),
    ])
    direct.search.side_effect = [SearchValidationError("unsupported filters"), original]
    result = await hybrid_retrieve(**kwargs)
    assert direct.search.call_count == 2
    assert direct.search.call_args_list[0].kwargs["filters"] == {"repository": "repo"}
    assert "filters" not in direct.search.call_args_list[1].kwargs
    assert {c.chunk_id for c in kwargs["reranker"].calls[0][1]} == {"A", "B", "C"}
    assert [s["chunk_id"] for s in result.sources["chunks"]] == ["B"]

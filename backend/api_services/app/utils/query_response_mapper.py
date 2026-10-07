from __future__ import annotations

from ai_services.agents.rag_agent.dataclasses.result import RAGAgentResult
from api_services.app.models.query import APIResponse, QueryGraphContext, Source


def build_api_response(
    result: RAGAgentResult,
    conversation_id: str,
) -> APIResponse:
    chunks = result.sources.get("chunks", [])

    sources = [
        Source(
            file_path=chunk["file_path"],
            excerpt=chunk["excerpt"],
            score=chunk["score"],
            score_type=chunk.get("score_type"),
        )
        for chunk in chunks
    ]

    gc = getattr(result, "graph_context", None)
    if gc is not None:
        graph_context = QueryGraphContext(
            node_ids=list(getattr(gc, "node_ids", [])),
            edge_ids=list(getattr(gc, "edge_ids", [])),
            assertion_ids=list(getattr(gc, "assertion_ids", [])),
        )
    else:
        graph_context = QueryGraphContext()

    return APIResponse(
        answer=result.answer,
        sources=sources,
        conversation_id=conversation_id,
        graph_context=graph_context,
    )

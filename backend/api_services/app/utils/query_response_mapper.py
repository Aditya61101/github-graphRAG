from api_services.app.models.query import APIResponse, Source
from ai_services.agents.rag_agent.dataclasses.result import RAGAgentResult


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
        )
        for chunk in chunks
    ]

    return APIResponse(
        answer=result.answer,
        sources=sources,
        conversation_id=conversation_id,
    )
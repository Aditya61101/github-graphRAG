from __future__ import annotations
from fastapi import APIRouter

from api_services.app.utils.query_response_mapper import build_api_response

from api_services.app.models.query import APIResponse, QueryPreparationResult, QueryRequest
from ai_services.agents.rag_agent.agent import RAGQueryAgent

router = APIRouter()

@router.post("/query", response_model=APIResponse)
def prepare_query(request: QueryRequest) -> QueryPreparationResult:
    rag_agent = RAGQueryAgent()
    result = rag_agent.prepare(
        conversation_id=request.conversation_id,
        query=request.query,
    )

    return build_api_response(
        result=result,
        conversation_id=request.conversation_id,
    )


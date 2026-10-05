from __future__ import annotations

from fastapi import APIRouter, Request

from api_services.app.utils.query_response_mapper import build_api_response
from api_services.app.models.query import APIResponse, QueryRequest
from ai_services.agents.rag_agent.agent import RAGQueryAgent

router = APIRouter()


def get_rag_agent(request: Request) -> RAGQueryAgent:
    return request.app.state.rag_agent


@router.post("", response_model=APIResponse)
async def query(
    request: QueryRequest,
    http_request: Request,
) -> APIResponse:
    rag_agent = get_rag_agent(http_request)

    result = await rag_agent.query(
        conversation_id=request.conversation_id,
        query=request.query,
    )

    return build_api_response(
        result=result,
        conversation_id=request.conversation_id,
    )

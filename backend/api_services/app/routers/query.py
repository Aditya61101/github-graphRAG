from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ai_services.agents.rag_agent.agent import RAGQueryAgent
from ai_services.ingestion.persistence.models import UserModel
from api_services.app.models.query import APIResponse, QueryRequest
from api_services.app.utils.jwt_utils import get_current_user
from api_services.app.utils.query_response_mapper import build_api_response

router = APIRouter()


def get_rag_agent(request: Request) -> RAGQueryAgent:
    return request.app.state.rag_agent


@router.post("", response_model=APIResponse)
async def query(
    request: QueryRequest,
    http_request: Request,
    current_user: UserModel = Depends(get_current_user),
) -> APIResponse:
    """Execute conversational GraphRAG query authorized against the authenticated user's repositories."""
    authorized_repo_id: str | None = None

    if request.repository_id:
        sqlite_store = getattr(http_request.app.state, "sqlite_store", None)
        if not sqlite_store:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Application store is not initialized.",
            )

        repo = sqlite_store.get_repository(request.repository_id, user_id=current_user.id)
        if not repo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{request.repository_id}' not found or access denied for the current user.",
            )
        authorized_repo_id = repo.id

    rag_agent = get_rag_agent(http_request)

    result = await rag_agent.query(
        conversation_id=request.conversation_id,
        query=request.query,
        repository_id=authorized_repo_id,
    )

    return build_api_response(
        result=result,
        conversation_id=request.conversation_id,
    )

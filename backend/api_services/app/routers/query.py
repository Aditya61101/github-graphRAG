from fastapi import APIRouter, Depends, HTTPException, Request
from ai_services.ingestion.persistence.models import UserModel
from api_services.app.models.query import APIResponse, QueryRequest
from api_services.app.utils.jwt_utils import get_current_user
from api_services.app.utils.github_app_access import query_authorized
from api_services.app.utils.query_response_mapper import build_api_response

router = APIRouter()


@router.post('', response_model=APIResponse)
async def query(request: QueryRequest, http_request: Request,
                current_user: UserModel = Depends(get_current_user)):
    if not request.repository_id:
        raise HTTPException(422, 'repository_id is required')
    result = await query_authorized(http_request, current_user, request.repository_id, request)
    return build_api_response(result=result, conversation_id=request.conversation_id)

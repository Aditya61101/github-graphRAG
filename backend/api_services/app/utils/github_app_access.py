from fastapi import HTTPException, Request
from ai_services.github_app.service import GitHubAppError


def get_github_app(request: Request):
    service = getattr(request.app.state, 'github_app', None)
    if service is None:
        raise HTTPException(503, 'GitHub App service is not initialized')
    return service


async def authorize_repository(request: Request, user_id: str, identifier: str):
    store = getattr(request.app.state, 'sqlite_store', None)
    if store is None:
        raise HTTPException(503, 'Application store is not initialized')
    repo = store.get_repository(identifier)
    if not repo:
        raise HTTPException(404, 'Repository not found')
    if repo.user_id != user_id:
        raise HTTPException(403, 'Access denied: You do not have permission to access this repository')
    try:
        return await get_github_app(request).authorize_tracked(user_id, repo.id)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


async def query_authorized(request, user, repository_id, payload):
    repo = await authorize_repository(request, user.id, repository_id)
    try:
        get_github_app(request).bind_conversation(user.id, repo.id, payload.conversation_id)
    except GitHubAppError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    agent = getattr(request.app.state, 'rag_agent', None)
    if agent is None:
        raise HTTPException(503, 'RAG query agent is not initialized')
    return await agent.query(conversation_id=payload.conversation_id, query=payload.query,
                             repository_id=repo.id, user_id=user.id)

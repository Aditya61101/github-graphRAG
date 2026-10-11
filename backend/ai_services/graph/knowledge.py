import asyncio
from api_services.app.models.knowledge import KnowledgeDetail, KnowledgePage


class RepositoryKnowledgeService:
    """Read existing ADR-derived knowledge through the shared access verifier."""
    def __init__(self, graph_service):
        self.graph_service = graph_service

    async def _scope(self, repository_id, user_id):
        repo = await self.graph_service.validate_repository_read_access(repository_id, user_id)
        return list(dict.fromkeys(filter(None, [repo.id, repo.github_repository_id])))

    async def list(self, repository_id, user_id, search, kind, offset, limit):
        scope = await self._scope(repository_id, user_id)
        result = await asyncio.to_thread(self.graph_service.graph_repo.list_architectural_knowledge,
                                        scope, search, kind, offset, limit)
        return KnowledgePage(**result)

    async def detail(self, repository_id, user_id, item_id):
        scope = await self._scope(repository_id, user_id)
        result = await asyncio.to_thread(self.graph_service.graph_repo.get_architectural_knowledge,
                                        scope, item_id)
        return KnowledgeDetail(**result) if result else None

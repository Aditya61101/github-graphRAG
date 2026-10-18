import asyncio
from ai_services.ingestion.readiness import guarded_read
from api_services.app.models.knowledge import KnowledgeDetail, KnowledgePage, KnowledgeItem


class RepositoryKnowledgeService:
    """Read existing ADR-derived knowledge through the shared access verifier."""
    def __init__(self, graph_service):
        self.graph_service = graph_service
        self.barrier = getattr(graph_service, "barrier", None)

    async def _scope(self, repository_id, user_id):
        repo = await self.graph_service.validate_repository_read_access(repository_id, user_id)
        return list(dict.fromkeys(filter(None, [repo.id, repo.github_repository_id])))

    @guarded_read
    async def list(self, repository_id, user_id, search, kind, offset, limit):
        scope = await self._scope(repository_id, user_id)
        result = await asyncio.to_thread(self.graph_service.graph_repo.list_architectural_knowledge,
                                        scope, search, kind, offset, limit)
        return KnowledgePage(**result)

    @guarded_read
    async def detail(self, repository_id, user_id, item_id):
        scope = await self._scope(repository_id, user_id)
        result = await asyncio.to_thread(self.graph_service.graph_repo.get_architectural_knowledge,
                                        scope, item_id)
        return KnowledgeDetail(**result) if result else None

    @guarded_read
    async def enrich_risks(self, repository_id, user_id, risks):
        scope = await self._scope(repository_id, user_id)
        records = await asyncio.to_thread(self.graph_service.graph_repo.get_architectural_knowledge_batch,
                                          scope, [risk['identity'] for risk in risks])
        by_id = {}
        for record in records:
            by_id.setdefault(record['id'], []).append(record)
        enriched = []
        for risk in risks:
            matches = by_id.get(risk['identity'], [])
            expected = 'decision' if risk['kind'] == 'DECISION' else 'constraint'
            record = matches[0] if len(matches) == 1 and matches[0]['kind'] == expected else None
            enriched.append({**risk, 'knowledge': KnowledgeItem(**record).model_dump() if record else None,
                             'knowledge_resolution': 'RESOLVED' if record else 'NOT_FOUND'})
        return enriched

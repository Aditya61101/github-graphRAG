"""Validate the contracts consumed by Explorer without changing retrieval."""
import pytest
from sqlalchemy import select
from ai_services.tests.test_github_app import env
from ai_services.tests.test_shared_knowledge import shared, bind
from ai_services.agents.rag_agent.dataclasses.result import RAGAgentResult
from ai_services.graph.context import GraphContext
from ai_services.ingestion.persistence.models import ConversationScopeModel

@pytest.mark.asyncio
async def test_explorer_contract_and_user_specific_conversation_continuity(shared):
    repository = await bind(shared)
    agent = shared.env.app.state.rag_agent.query
    context = GraphContext(node_ids=['entity:orders', 'entity:db'],
                           edge_ids=['edge:orders:WRITES_TO:db'],
                           assertion_ids=['assertion:adr:storage'])
    agent.return_value = RAGAgentResult(answer='**Transactional storage**', sources={'chunks': [
        {'file_path': 'docs/storage.md', 'excerpt': 'Use transactions.', 'score': .94,
         'score_type': 'reranker_relevance'}]}, graph_context=context)
    for headers in (shared.headers, shared.env.headers):
        for question in ('Explain storage', 'Explain that further'):
            response = shared.env.client.post(f'/repositories/{repository.id}/query', headers=headers,
                json={'query': question, 'conversation_id': 'explorer-continuity'})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data['conversation_id'] == 'explorer-continuity'
            assert data['answer'] == '**Transactional storage**'
            assert data['sources'][0]['excerpt'] == 'Use transactions.'
            assert data['sources'][0]['score_type'] == 'reranker_relevance'
            assert data['graph_context'] == context.to_dict()
    calls = agent.await_args_list
    assert all(call.kwargs['repository_id'] == repository.id for call in calls)
    assert [call.kwargs['user_id'] for call in calls] == [shared.user.id] * 2 + [shared.env.user.id] * 2
    with shared.env.store.session_factory() as session:
        scopes = list(session.scalars(select(ConversationScopeModel)))
        assert {(scope.user_id, scope.conversation_id, scope.repository_id) for scope in scopes} == {
            (shared.user.id, 'explorer-continuity', repository.id),
            (shared.env.user.id, 'explorer-continuity', repository.id)}
    assert shared.env.client.get(f'/repositories/{repository.id}/graph', headers=shared.headers).status_code == 200
    assert shared.env.client.get(f'/repositories/{repository.id}/adrs', headers=shared.headers).status_code == 403

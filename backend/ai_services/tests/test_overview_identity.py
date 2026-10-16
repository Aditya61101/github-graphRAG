"""Exact, repository-scoped read enrichment. No evaluator/provider clients."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_services.graph.knowledge import RepositoryKnowledgeService
from ai_services.graph.repository import Neo4jGraphRepository, GraphRepositoryError
from ai_services.github_app.service import GitHubAppError
from ai_services.tests.test_repository_overview import overview_env  # noqa: F401
from ai_services.tests.test_repository_graph import graph_env  # noqa: F401


@pytest.fixture
def actual():
    return json.loads((Path(__file__).resolve().parents[3] / 'frontend/tests/fixtures/stage-4-5-3-identities.json').read_text())


@pytest.mark.asyncio
async def test_real_case_a_exact_identities_one_scoped_query(actual):
    calls = []
    def execute(query, **params):
        calls.append(params)
        assert 'd.id IN $item_ids' in query
        assert 'd.repository IN $repo_ids' in query and 'a.repository IN $repo_ids' in query
        assert 'CONTAINS' not in query
        return actual['knowledge'], None, None
    graph = SimpleNamespace(validate_repository_read_access=AsyncMock(return_value=SimpleNamespace(id=actual['repository_id'], github_repository_id='1120610241')),
                            graph_repo=Neo4jGraphRepository(SimpleNamespace(execute_query=execute)))
    result = await RepositoryKnowledgeService(graph).enrich_risks(actual['repository_id'], 'reader', actual['attention'])
    assert len(calls) == 1 and calls[0]['repo_ids'] == [actual['repository_id'], '1120610241']
    assert len(calls[0]['item_ids']) == 4
    assert all(item['knowledge_resolution'] == 'RESOLVED' for item in result)
    requirements = {r['requirement_id']: r for r in actual['requirements']}
    for item in result:
        record = item['knowledge']
        assert record['id'] == item['identity']
        assert record['title'] == requirements[item['identity']]['title']
        assert record['description'] == requirements[item['identity']]['statement']
        assert record['sources'][0]['title'].startswith('ADR-001: Two-Phase')
    unmapped = [item for item in result if item['kind'] == 'REQUIREMENT']
    assert len(unmapped) == 2 and len({item['identity'] for item in unmapped}) == 2
    assert unmapped[0]['knowledge']['description'] != unmapped[1]['knowledge']['description']
    assert sorted(item['classification'] for item in result if item['kind'] == 'DECISION') == ['CONFIRMED', 'POTENTIAL']
    assert [{k:v for k,v in item.items() if not k.startswith('knowledge')} for item in result] == actual['attention']


@pytest.mark.asyncio
async def test_missing_wrong_kind_ambiguous_and_denied_never_guess(actual):
    batch = SimpleNamespace(get_architectural_knowledge_batch=lambda *args: [])
    graph = SimpleNamespace(validate_repository_read_access=AsyncMock(return_value=SimpleNamespace(id='repo_a', github_repository_id='1')), graph_repo=batch)
    service = RepositoryKnowledgeService(graph)
    risk = actual['attention'][0]
    for records in ([], [{**actual['knowledge'][0], 'id': risk['identity'], 'kind': 'constraint'}], [actual['knowledge'][0], actual['knowledge'][0]]):
        batch.get_architectural_knowledge_batch = lambda *args: records
        result = await service.enrich_risks('repo_a', 'reader', [risk])
        assert result[0]['knowledge'] is None
    graph.validate_repository_read_access.side_effect = GitHubAppError(403, 'denied')
    batch.get_architectural_knowledge_batch = lambda *args: pytest.fail('Graph read after denied authorization')
    with pytest.raises(GitHubAppError):
        await service.enrich_risks('repo_b', 'reader', [risk])


def test_overview_enrichment_preserves_counts_and_fails_honestly(overview_env, actual, monkeypatch):
    env = overview_env
    metrics = dict(attention=actual['attention'], attention_total=4, recent=[], decisions_at_risk=2, prs_analyzed=1, architectural_warnings=7,
                   confirmed_decisions=1, potential_decisions=1, unmapped_requirements=2, confirmed_warnings=3, potential_warnings=4, uncertain_units=6)
    monkeypatch.setattr('ai_services.decision_guard.orchestration.projections.RepositoryReadModel.overview', lambda *args: dict(metrics))
    env['client'].app.state.evaluation_orchestrator = SimpleNamespace(store=SimpleNamespace(clock=lambda: 0))
    enrichment = AsyncMock(return_value=[{**risk, 'knowledge_resolution': 'NOT_FOUND', 'knowledge': None} for risk in actual['attention']])
    env['client'].app.state.knowledge_service = SimpleNamespace(enrich_risks=enrichment)
    response = env['client'].get('/repositories/repo_a/overview', headers=env['headers'])
    assert response.status_code == 200
    data = response.json()
    assert [data['kpis'][key]['value'] for key in ('decisions_at_risk','prs_analyzed','architectural_warnings')] == [2,1,7]
    assert data['risk_breakdown']['unmapped_requirements'] == 2
    enrichment.assert_awaited_once()
    enrichment.side_effect = GraphRepositoryError('offline')
    response = env['client'].get('/repositories/repo_a/overview', headers=env['headers'])
    assert response.status_code == 200
    assert all(item['knowledge_resolution'] == 'UNAVAILABLE' for item in response.json()['decisions_needing_attention']['items'])
    enrichment.reset_mock()
    assert env['client'].get('/repositories/repo_b/overview', headers=env['headers']).status_code == 403
    enrichment.assert_not_awaited()

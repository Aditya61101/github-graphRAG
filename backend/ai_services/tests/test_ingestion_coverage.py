import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chunker import LanguageDetectorImpl

from ai_services.ingestion.discovery.git_tree import GitFile
from ai_services.ingestion.discovery.plan_validation import (
    PlannerCoverageError, resolve_planned_file, validate_ingestion_plan,
)
from ai_services.ingestion.discovery.planner import create_ingestion_plan
from ai_services.ingestion.discovery.repo_manifest import build_repository_manifest
from ai_services.ingestion.rkg.batching import TokenBudgetBatcher
from ai_services.ingestion.rkg.canonicalization import EntityCanonicalizer
from ai_services.ingestion.rkg.candidate_generation import RelationshipCandidateGenerator
from ai_services.ingestion.rkg.cross_chunk import CrossChunkReasoner
from ai_services.ingestion.rkg.ingestion_pipeline import RepositoryIngestionPipeline
from ai_services.ingestion.rkg.models import (
    CandidateKnowledge, ChunkStrategy, EvidenceChunk,
    ExtractedEntity, ExtractedRelationship, ValidatedRelationship,
)
from ai_services.ingestion.rkg.pipeline import PipelineConfig, RepositoryKnowledgePipeline
from ai_services.ingestion.rkg.splitters import RepositoryTextSplitter
from ai_services.ingestion.rkg.store import JsonlCandidateKnowledgeStore
from ai_services.ingestion.run_audit import (
    IngestionRunAudit, PersistedCounts, measure_persisted_counts,
)
from ai_services.ingestion.service import RepositoryIngestionService
from ai_services.ingestion.sources.classifier import classify_file_for_ingestion
from ai_services.ingestion.sources.credentials import GitHubCredential
from ai_services.ingestion.sources.interface import RepositoryMetadata, RepositoryRef, RepositorySnapshot
from ai_services.ingestion.rkg.incremental import ChangeKind
from ai_services.models.ingestion_plan import IngestionPlan, IngestionAction
from api_services.app.models.repository import IngestionJobResponse


def manifest(paths):
    return build_repository_manifest('example', 'sha', [
        GitFile(path, '100644', 'blob', 'id', 100) for path in paths
    ]).to_dict()


def plan(paths):
    return IngestionPlan(files=[classify_file_for_ingestion(path) for path in paths])


def planner_client(output):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=MagicMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)), finish_reason='stop')],
            model='test-model', id='response',
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        ),
    ))))


@pytest.mark.parametrize('paths,field', [
    (['a.py'], 'missing'), (['a.py', 'b.py', 'a.py'], 'duplicates'),
    (['a.py', 'b.py', 'unknown.py'], 'unknown'),
])
def test_coverage_validator_requires_exact_manifest_coverage(paths, field):
    with pytest.raises(PlannerCoverageError) as error:
        validate_ingestion_plan(plan(paths), manifest(['a.py', 'b.py']))
    assert error.value.details[field]


def test_planner_leaves_coverage_validation_to_caller():
    result = create_ingestion_plan(
        planner_client(plan(['a.py']).model_dump(mode='json')),
        'unchanged', manifest(['a.py', 'b.py']),
    )
    assert [file.path for file in result.files] == ['a.py']
    with pytest.raises(PlannerCoverageError) as error:
        validate_ingestion_plan(result, manifest(['a.py', 'b.py']))
    assert error.value.details['missing'] == ['b.py']


@pytest.mark.parametrize('path', [
    '/tmp/a.py', 'C:/repo/a.py', 'C:a.py', r'C:\repo\a.py', r'\\server\repo\a.py',
    '../a.py', 'backend/../a.py', './a.py', 'backend//a.py', r'backend\a.py',
    '~/a.py', 'https://example/a.py', 'a.py/', '', 'a\x00.py',
])
def test_non_repository_relative_paths_are_rejected(path):
    with pytest.raises(PlannerCoverageError) as error:
        validate_ingestion_plan(plan([path]), manifest(['a.py']))
    assert path in error.value.details['invalid']


def test_explicit_exclusion_is_valid_and_raw_metadata_is_captured():
    output = plan(['a.py', 'b.py'])
    output.files[1].action = IngestionAction.EXCLUDE
    captured = []
    result = create_ingestion_plan(
        planner_client(output.model_dump(mode='json')), 'unchanged', json.dumps(manifest(['a.py', 'b.py'])),
        on_response=lambda content, metadata: captured.append((content, metadata)),
    )
    assert result.files[1].action == IngestionAction.EXCLUDE
    assert captured[0][1]['finish_reason'] == 'stop'
    assert captured[0][1]['usage']['total_tokens'] == 150


def pipeline(root, **overrides):
    args = dict(
        repository_root=root, repository_id='repo', repository_name='example', commit='sha',
        splitter=RepositoryTextSplitter(), knowledge_pipeline=MagicMock(),
        cross_chunk_reasoner=MagicMock(), neo4j_writer=MagicMock(),
        language_detector=LanguageDetectorImpl(), graph_neighborhood_loader=AsyncMock(return_value=''),
    )
    args.update(overrides)
    return RepositoryIngestionPipeline(**args)


@pytest.mark.parametrize('path', ['missing.py', '../outside.py', '/tmp/a.py', 'parent/a.py'])
def test_chunking_does_not_silently_skip_invalid_or_nonexistent_paths(tmp_path, path):
    with pytest.raises((ValueError, FileNotFoundError)):
        pipeline(tmp_path)._create_chunks(plan([path]).files)


def test_chunking_exclusion_and_duplicate_paths(tmp_path):
    (tmp_path / 'a.py').write_text('def login():\n    return True\n', encoding='utf-8')
    excluded = plan(['a.py'])
    excluded.files[0].action = IngestionAction.EXCLUDE
    assert pipeline(tmp_path)._create_chunks(excluded.files) == []
    with pytest.raises(ValueError, match='Duplicate'):
        pipeline(tmp_path)._create_chunks(plan(['a.py', 'a.py']).files)


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    outside = tmp_path / 'outside.py'
    outside.write_text('secret', encoding='utf-8')
    try:
        (root / 'link.py').symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation is unavailable on this host')
    with pytest.raises(ValueError, match='outside'):
        resolve_planned_file(root, 'link.py')


@pytest.mark.asyncio
async def test_incremental_preflight_precedes_deletion(tmp_path):
    instance = pipeline(tmp_path)
    with pytest.raises(FileNotFoundError):
        await instance.ingest_incremental(plan(['missing.py']).files, ['old.py'])
    instance.neo4j_writer.delete_files.assert_not_called()


def test_local_and_cloned_roots_preserve_auth_evidence(tmp_path):
    auth = {
        'backend/app/api/auth.py': 3, 'backend/app/core/jwt.py': 1,
        'backend/app/core/security.py': 3, 'backend/app/core/security_utils.py': 2,
    }
    originals, clones = [], []
    for name, output in [('local', originals), ('clone', clones)]:
        root = tmp_path / name
        for path, count in auth.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('\n'.join(f'def symbol_{i}():\n    return {i}\n' for i in range(count)), encoding='utf-8')
        output.extend(pipeline(root)._create_chunks(plan(list(auth)).files))
    assert [c.chunk_id for c in originals] == [c.chunk_id for c in clones]
    assert len(originals) == 9
    assert {p: sum(c.file_path == p for c in clones) for p in auth} == auth
    scoped = [EvidenceChunk.create('other', c.commit, c.file_path, c.chunk_index, c.text, c.strategy) for c in originals]
    assert not {c.chunk_id for c in scoped} & {c.chunk_id for c in originals}


@pytest.mark.asyncio
async def test_cache_identity_and_full_resume_are_explicit(tmp_path):
    store = JsonlCandidateKnowledgeStore(tmp_path / 'candidates.jsonl')
    chunk = EvidenceChunk.create('repo', 'sha', 'auth.py', 0, 'code', ChunkStrategy.WHOLE_FILE)
    empty = CandidateKnowledge(chunk_id=chunk.chunk_id)
    await store.put(empty)
    assert await store.get_for_chunk(chunk.chunk_id) == empty
    resumed = JsonlCandidateKnowledgeStore(store.path)
    assert await resumed.get_for_chunk(chunk.chunk_id) == empty
    for repository, commit in [('other', 'sha'), ('repo', 'new-sha')]:
        changed = EvidenceChunk.create(repository, commit, 'auth.py', 0, 'code', ChunkStrategy.WHOLE_FILE)
        assert await resumed.get_for_chunk(changed.chunk_id) is None


@pytest.fixture
def full_service(tmp_path, monkeypatch):
    root = tmp_path / 'clone'
    root.mkdir()
    (root / 'auth.py').write_text('def login():\n    return True\n', encoding='utf-8')
    (root / 'README.md').write_text('# Architecture\nAuthentication service.\n', encoding='utf-8')
    files = [GitFile('auth.py', '100644', 'blob', 'a', 30), GitFile('README.md', '100644', 'blob', 'b', 40)]
    monkeypatch.setattr('ai_services.ingestion.service.get_repository_files', lambda root: files)
    ref = RepositoryRef('owner', 'example', branch='main')
    metadata = RepositoryMetadata('1', 'owner', 'example', 'owner/example', 'main', 'main', 'https://example')
    snapshot = RepositorySnapshot(ref, root, 'sha', 'main')
    source = MagicMock()
    source.resolve_ref.return_value = ref
    source.get_metadata = AsyncMock(return_value=metadata)
    source.prepare_snapshot = AsyncMock(return_value=snapshot)
    source.update_snapshot = AsyncMock(return_value=snapshot)
    store = MagicMock()
    store.save_repository.return_value = SimpleNamespace(id='repo', indexed_commit_sha=None)
    store.record_ingestion_start.side_effect = [SimpleNamespace(id=f'run_{i}') for i in range(10)]

    async def extract(chunks, **kwargs):
        return [CandidateKnowledge(
            chunk_id=c.chunk_id,
            entities=[ExtractedEntity(label='Service', name=name, source_chunk_ids=[c.chunk_id]) for name in ['Auth', 'Tokens']],
            relationships=[ExtractedRelationship(
                source_label='Service', source_name='Auth', target_label='Service', target_name='Tokens',
                relationship_type='USES', source_chunk_ids=[c.chunk_id],
            )],
        ) for c in chunks]

    async def validate(candidate, **kwargs):
        return ValidatedRelationship(
            **{k: getattr(candidate, k) for k in ['source_id', 'target_id', 'relationship_type', 'evidence_chunk_ids']},
            confidence=0.9, rationale='test evidence',
        )

    extractor = SimpleNamespace(extract=AsyncMock(side_effect=extract))
    embedder = SimpleNamespace(embed=AsyncMock(side_effect=lambda texts: [[1.0, 0.0, 0.0] for _ in texts]))
    writer = MagicMock()
    def build(**kwargs):
        reasoner = CrossChunkReasoner(SimpleNamespace(validate=AsyncMock(side_effect=validate)))
        knowledge = RepositoryKnowledgePipeline(
            embedder, extractor, JsonlCandidateKnowledgeStore(kwargs['candidates_path']),
            EntityCanonicalizer(), RelationshipCandidateGenerator(), reasoner,
            TokenBudgetBatcher(lambda text: len(text)), PipelineConfig(embedding_dimensions=3),
        )
        return pipeline(root, knowledge_pipeline=knowledge, cross_chunk_reasoner=reasoner,
                        neo4j_writer=writer, progress_callback=kwargs['progress_callback'])
    builder = MagicMock(side_effect=build)
    monkeypatch.setattr('ai_services.ingestion.service.build_repository_ingestion_pipeline', builder)
    def persisted(driver, database, repo, result):
        assert repo == 'repo'
        return PersistedCounts(len(result.chunks), len(result.entities.entities) if result.entities else 0,
                               len(result.validated_relationships), len(result.validated_relationships))
    monkeypatch.setattr('ai_services.ingestion.service.measure_persisted_counts', persisted)
    service = RepositoryIngestionService(source, store, MagicMock(), database='neo4j', state_dir=tmp_path / 'state')
    service._run_community_pipeline = AsyncMock(return_value=SimpleNamespace(community_count=1))
    return SimpleNamespace(service=service, root=root, source=source, store=store, ref=ref,
                           metadata=metadata, builder=builder, writer=writer, extractor=extractor, tmp=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'unknown', 'absolute', 'malformed'])
async def test_invalid_planner_uses_classifier_and_saves_original_response(full_service, failure):
    fx = full_service
    output = plan(['auth.py', 'README.md']).model_dump(mode='json')
    if failure == 'missing': output['files'] = output['files'][:1]
    if failure == 'duplicate': output['files'].append(output['files'][0])
    if failure == 'unknown': output['files'].append(plan(['unknown.py']).model_dump(mode='json')['files'][0])
    if failure == 'absolute': output['files'][0]['path'] = '/tmp/auth.py'
    if failure == 'malformed': output = {'files': 'invalid'}
    fx.service.groq_client = planner_client(output)
    if failure == 'missing':
        fx.service.groq_client.chat.completions.create.return_value.choices[0].finish_reason = 'length'
    result = await fx.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    path = fx.service.state_dir / 'ingestion_runs' / 'repo' / result.run_id
    assert json.loads((path / 'manifest.json').read_text())['file_count'] == 2
    metadata = json.loads((path / 'planner_metadata.json').read_text())
    assert metadata['source'] == 'classifier_fallback'
    assert metadata['finish_reason'] == ('length' if failure == 'missing' else 'stop')
    assert json.loads(json.loads((path / 'planner_response.json').read_text())['content']) == output
    assert len(json.loads((path / 'planner_output.json').read_text())['files']) == 2
    assert result.stage_counts.chunked == result.added_or_modified_count == 2
    assert result.stage_counts.extracted == 4
    assert result.stage_counts.canonicalized == 2
    assert result.stage_counts.candidates == result.stage_counts.validated == 1
    assert result.stage_counts.persisted == PersistedCounts(2, 2, 1, 1)
    assert result.stage_counts.communities == 1
    assert '2/2 included files' in result.message
    fx.service._run_community_pipeline.assert_awaited_once_with(repository_id='repo')
    fx.writer.write_entities.assert_called_once()
    assert fx.writer.write_entities.call_args.kwargs['repository'] == 'repo'
    assert fx.writer.write_relationships.call_args.kwargs['repository'] == 'repo'
    assert json.loads((path / 'stage_counts.json').read_text()) == result.stage_counts.to_dict()


@pytest.mark.asyncio
async def test_valid_exclusions_do_not_trigger_fallback_and_counts_are_actual(full_service):
    fx = full_service
    output = plan(['auth.py', 'README.md'])
    output.files[1].action = IngestionAction.EXCLUDE
    fx.service.groq_client = planner_client(output.model_dump(mode='json'))
    result = await fx.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    assert result.stage_counts.discovered == result.stage_counts.planned == 2
    assert result.stage_counts.included == result.stage_counts.excluded == 1
    assert result.stage_counts.chunked == result.stage_counts.chunks == 1
    assert result.added_or_modified_count == 1
    assert '1/1 included files' in result.message
    metadata = json.loads((fx.service.state_dir / 'ingestion_runs/repo' / result.run_id / 'planner_metadata.json').read_text())
    assert metadata['source'] == 'groq' and 'fallback_reason' not in metadata
    response = IngestionJobResponse(status='completed', repository='owner/example', stage_counts=result.stage_counts)
    assert response.model_dump()['stage_counts']['persisted']['entities'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('indexed,force,expected', [(None, False, 'full'), ('sha', False, 'incremental'), ('sha', True, 'full')])
async def test_initial_full_and_forced_full_routing(full_service, indexed, force, expected):
    fx = full_service
    fx.store.save_repository.return_value.indexed_commit_sha = indexed
    fx.service._run_full_ingestion = AsyncMock()
    fx.service._run_incremental_ingestion = AsyncMock()
    await fx.service.ingest_repository('owner/example', force_full=force, credential=GitHubCredential('test'))
    target = fx.service._run_full_ingestion if expected == 'full' else fx.service._run_incremental_ingestion
    target.assert_awaited_once()
    if expected == 'incremental': assert target.call_args.kwargs['indexed_sha'] == indexed


@pytest.mark.asyncio
async def test_same_commit_sync_does_not_ingest_or_create_new_audit(full_service):
    fx = full_service
    fx.store.save_repository.return_value.indexed_commit_sha = 'sha'
    result = await fx.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    assert result.status == 'already_indexed'
    fx.builder.assert_not_called()
    fx.store.record_ingestion_start.assert_not_called()


@pytest.mark.asyncio
async def test_failed_chunking_is_recorded_without_marking_success(full_service):
    fx = full_service
    (fx.root / 'auth.py').unlink()
    with pytest.raises(FileNotFoundError):
        await fx.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    metadata = json.loads((fx.service.state_dir / 'ingestion_runs/repo/run_0/run_metadata.json').read_text())
    assert metadata['status'] == 'failed'
    assert (fx.service.state_dir / 'ingestion_runs/repo/run_0/planner_output.json').exists()
    fx.store.record_ingestion_failure.assert_called_once()
    fx.store.record_ingestion_success.assert_not_called()


def test_persistence_measurement_is_batched_and_repository_scoped():
    driver = MagicMock()
    driver.execute_query.return_value = ([dict(chunks=1, entities=1, relationships=0, assertions=0)], None, None)
    result = SimpleNamespace(chunks=[SimpleNamespace(chunk_id='chunk')],
                             entities=SimpleNamespace(entities={'entity': object()}), validated_relationships=[])
    assert measure_persisted_counts(driver, 'neo4j', 'repo', result) == PersistedCounts(1, 1, 0, 0)
    assert driver.execute_query.call_count == 1
    call = driver.execute_query.call_args
    assert call.kwargs['repo'] == 'repo' and call.kwargs['routing_'] == 'r'
    assert call.kwargs['chunk_ids'] == ['chunk']
    assert call.kwargs['entity_ids'] == ['entity']
    assert 'repository: $repo' in call.args[0]


def test_audit_run_directories_are_isolated_and_unsafe_ids_are_rejected(tmp_path):
    first = IngestionRunAudit(tmp_path, 'repo', 'run_1')
    second = IngestionRunAudit(tmp_path, 'repo', 'run_2')
    first.counts.discovered = 2
    first.save_counts()
    assert json.loads((second.path / 'stage_counts.json').read_text())['discovered'] == 0
    assert json.loads((second.path / 'stage_counts.json').read_text())['persisted'] is None
    with pytest.raises(ValueError): IngestionRunAudit(tmp_path, '../repo', 'run')


@pytest.mark.asyncio
async def test_incremental_plan_covers_changed_manifest_and_uses_indexed_base(full_service):
    fx = full_service
    fx.store.save_repository.return_value.indexed_commit_sha = 'old-sha'
    fx.source.update_snapshot.return_value = RepositorySnapshot(fx.ref, fx.root, 'new-sha', 'main')
    fx.source.compute_diff.return_value = [SimpleNamespace(kind=ChangeKind.MODIFIED, path='auth.py')]
    result = await fx.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    fx.source.compute_diff.assert_called_once_with(
        snapshot=fx.source.update_snapshot.return_value, base_commit='old-sha', target_commit='new-sha',
    )
    path = fx.service.state_dir / 'ingestion_runs/repo' / result.run_id
    assert json.loads((path / 'manifest.json').read_text())['file_count'] == 2
    assert json.loads((path / 'planning_manifest.json').read_text())['file_count'] == 1
    assert json.loads((path / 'planner_metadata.json').read_text())['scope'] == 'changed_files'
    assert result.stage_counts.discovered == 2 and result.stage_counts.planned == 1
    assert result.stage_counts.chunked == 1 and result.is_incremental
    fx.writer.delete_files.assert_called_once_with('repo', ['auth.py'])


@pytest.mark.asyncio
async def test_forced_full_retains_existing_cache_semantics_and_separate_audits(full_service):
    fx = full_service
    first = await fx.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    fx.store.save_repository.return_value.indexed_commit_sha = 'sha'
    second = await fx.service.ingest_repository('owner/example', force_full=True, credential=GitHubCredential('test'))
    assert first.run_id != second.run_id
    assert first.stage_counts.to_dict() == second.stage_counts.to_dict()
    fx.extractor.extract.assert_awaited_once()
    assert len(list((fx.service.state_dir / 'ingestion_runs/repo').iterdir())) == 2


@pytest.mark.asyncio
async def test_all_four_auth_files_reach_extraction_after_incomplete_plan_fallback(full_service, monkeypatch):
    fx = full_service
    paths = {'backend/app/api/auth.py': 3, 'backend/app/core/jwt.py': 1,
             'backend/app/core/security.py': 3, 'backend/app/core/security_utils.py': 2}
    for path, count in paths.items():
        target = fx.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('\n'.join(f'def symbol_{i}():\n    return {i}\n' for i in range(count)), encoding='utf-8')
    files = [GitFile(p, '100644', 'blob', 'id', (fx.root / p).stat().st_size) for p in paths]
    monkeypatch.setattr('ai_services.ingestion.service.get_repository_files', lambda root: files)
    fx.service.groq_client = planner_client({'files': []})
    result = await fx.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    extracted = fx.extractor.extract.call_args.args[0]
    assert len(extracted) == result.stage_counts.extraction_chunks == 9
    assert {p: sum(c.file_path == p for c in extracted) for p in paths} == paths
    assert result.stage_counts.canonicalized == 2
    assert result.stage_counts.validated == result.stage_counts.persisted.relationships == 1


@pytest.mark.asyncio
async def test_unavailable_community_count_is_not_reported_as_zero(full_service):
    full_service.service._run_community_pipeline.return_value = None
    result = await full_service.service.ingest_repository('owner/example', credential=GitHubCredential('test'))
    assert result.stage_counts.communities is None

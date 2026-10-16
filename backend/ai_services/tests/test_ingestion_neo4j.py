"""Opt-in actual Neo4j/GDS tests, restricted to the isolated localhost instance."""
import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from neo4j import GraphDatabase
from ai_services.ingestion.graph_control import GraphControl, FencedDriver
from ai_services.ingestion.persistence.ingestion_jobs import LostIngestionLease
from ai_services.ingestion.rkg.neo4j_writer import Neo4jRepositoryWriter
from ai_services.ingestion.rkg.models import EvidenceChunk, ChunkStrategy, CanonicalEntity, ValidatedRelationship


@pytest.fixture
def graph():
    uri = os.getenv("DECISIONGUARD_INGESTION_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Requires isolated localhost Neo4j/GDS on port 57687")
    assert uri == "bolt://127.0.0.1:57687", "Refuse non-test Neo4j"
    driver = GraphDatabase.driver(uri, auth=None)
    driver.verify_connectivity()
    control = GraphControl(driver)
    control.initialize()
    yield driver, control
    driver.close()


def writer_for(graph, *, token=1, repo=None):
    driver, control = graph
    repo = repo or "stage_b_" + uuid.uuid4().hex
    run = SimpleNamespace(id="run_"+uuid.uuid4().hex, owner=uuid.uuid4().hex,
        repository_id=repo, token=token, lease_until=int(time.time())+120,
        target_commit_sha="a"*40)
    control.arm(run)
    writer = Neo4jRepositoryWriter(FencedDriver(control,run))
    writer.initialize_constraints(embedding_dimensions=3)
    return repo, run, writer


def chunk(repo, path):
    c = EvidenceChunk.create(repo,"a"*40,path,0,"source "+path,ChunkStrategy.WHOLE_FILE)
    return EvidenceChunk(**{**c.__dict__,"embedding":[1.,0.,0.]})


def seed(graph):
    repo, run, writer = writer_for(graph)
    a,b = chunk(repo,"a.py"),chunk(repo,"b.py")
    writer.write_source(repository=repo,commit="a"*40,chunks=[a,b])
    entities = [CanonicalEntity(canonical_id=repo+name,label="Service",name=name,
        properties={"description":"original"}, evidence_chunk_ids=[a.chunk_id,b.chunk_id],
        embedding=[1.,0.,0.]) for name in ("S","T")]
    writer.write_entities(entities,repository=repo)
    relationship = ValidatedRelationship(source_id=entities[0].canonical_id,
        target_id=entities[1].canonical_id,relationship_type="USES", confidence=.9,
        rationale="supported",evidence_chunk_ids=[a.chunk_id])
    writer.write_relationships([relationship],repository=repo)
    return repo,run,writer,a,b,entities


def test_real_deletion_removes_unsupported_direct_edge_but_shared_entities_survive(graph):
    driver, control = graph
    repo,run,writer,a,b,entities = seed(graph)
    writer.delete_files(repo,["a.py"])
    assert driver.execute_query("MATCH (a:GraphAssertion {repository:$repo}) RETURN count(a) AS n",
        repo=repo).records[0]["n"] == 0
    assert driver.execute_query("MATCH (:Entity {repository:$repo})-[r:USES]->() RETURN count(r) AS n",
        repo=repo).records[0]["n"] == 0
    assert driver.execute_query("MATCH (e:Entity {repository:$repo}) RETURN count(e) AS n",
        repo=repo).records[0]["n"] == 2
    writer.delete_files(repo,["b.py"])  # Empty assertion set must still clean orphan entities.
    assert driver.execute_query("MATCH (e:Entity {repository:$repo}) RETURN count(e) AS n",
        repo=repo).records[0]["n"] == 0


def test_real_full_reconciliation_preserves_adr_support_and_applicability(graph):
    driver, control = graph
    repo,run,writer,a,b,entities = seed(graph)
    driver.execute_query("""
        MATCH (s:Entity {id:$source}), (t:Entity {id:$target})
        CREATE (adr:ADR {id:$adr,repository:$repo})
        CREATE (ch:Chunk:ADRChunk {id:$chunk,adr_id:$adr,repository:$repo})
        CREATE (d:ArchitecturalConstraint {id:$constraint,repository:$repo,scope:'preserved'})
        CREATE (adr)-[:HAS_CHUNK]->(ch), (adr)-[:DEFINES]->(d),
               (ch)-[:MENTIONS]->(s), (d)-[:CONSTRAINS]->(s)
        CREATE (a:GraphAssertion {id:$assertion,repository:$repo,source_type:'ADR',relationshipType:'REQUIRES'})
        CREATE (ch)-[:SUPPORTS]->(a), (s)-[:ASSERTS]->(a), (a)-[:TARGETS]->(t)
        """,repo=repo,source=entities[0].canonical_id,target=entities[1].canonical_id,
        adr=repo+"adr",chunk=repo+"adrchunk",constraint=repo+"constraint",assertion=repo+"assertion")
    writer.reconcile_code(repo)
    writer.reconcile_code(repo)
    assert driver.execute_query("MATCH (c:Chunk {repository:$repo}) RETURN count(c) AS n",
        repo=repo).records[0]["n"] == 1
    assert driver.execute_query("MATCH (f:File {repository:$repo}) RETURN count(f) AS n",
        repo=repo).records[0]["n"] == 0
    assert driver.execute_query("MATCH (a:GraphAssertion {repository:$repo}) RETURN a.source_type AS source",
        repo=repo).records[0]["source"] == "ADR"
    assert driver.execute_query("MATCH (d:ArchitecturalConstraint {repository:$repo})-[:CONSTRAINS]->() RETURN d.scope AS scope",
        repo=repo).records[0]["scope"] == "preserved"
    assert driver.execute_query("MATCH (e:Entity {repository:$repo}) RETURN count(e) AS n",
        repo=repo).records[0]["n"] == 2


def test_real_fence_blocks_stale_worker_and_reconciles_partial_write(graph):
    driver,control = graph
    repo,old,writer = writer_for(graph)
    writer.write_source(repository=repo,commit=old.target_commit_sha,chunks=[chunk(repo,"partial.py")])
    _,current,replacement = writer_for(graph,repo=repo,token=2)
    with pytest.raises(LostIngestionLease):
        writer.reconcile_code(repo)
    replacement.reconcile_code(repo)
    c = chunk(repo,"complete.py")
    replacement.write_source(repository=repo,commit=current.target_commit_sha,chunks=[c])
    replacement.validate(repo)
    control.seal(current)
    assert control.snapshot(repo)["state"] == "COMPLETE"
    with pytest.raises(LostIngestionLease):
        replacement.reconcile_code(repo)
    paths = driver.execute_query("MATCH (f:File {repository:$repo}) RETURN f.path AS path",repo=repo).records
    assert [r["path"] for r in paths] == ["complete.py"]


def test_real_neo4j_rejects_expired_owner_before_mutation(graph):
    _,control = graph
    repo = "stage_b_" + uuid.uuid4().hex
    run = SimpleNamespace(id="run_"+uuid.uuid4().hex,owner="expired",repository_id=repo,
        token=1,lease_until=int(time.time())-1,target_commit_sha="a"*40)
    control.arm(run)
    with pytest.raises(LostIngestionLease):
        FencedDriver(control,run).execute_query(
            "CREATE (:Entity {id:$id,repository:$repo})",id=repo+"forbidden",repo=repo)
    assert graph[0].execute_query("MATCH (e:Entity {repository:$repo}) RETURN count(e) AS n",
        repo=repo).records[0]["n"] == 0


def test_real_incremental_preserves_properties_supported_by_unchanged_file(graph):
    driver,_ = graph
    repo,run,writer,a,b,entities = seed(graph)
    writer.delete_files(repo,["a.py"])
    e = entities[0].model_copy(update={"properties":{"description":"partial-overwrite"},
        "evidence_chunk_ids":[b.chunk_id]})
    writer.write_entities([e],repository=repo)
    assert driver.execute_query("MATCH (e:Entity {id:$id}) RETURN e.description AS description",
        id=e.canonical_id).records[0]["description"] == "original"


def test_real_relationship_with_surviving_evidence_is_preserved(graph):
    driver,_ = graph
    repo,run,writer,a,b,entities = seed(graph)
    writer.write_relationships([ValidatedRelationship(
        source_id=entities[0].canonical_id,target_id=entities[1].canonical_id,
        relationship_type="USES",confidence=.9,rationale="both files support this",
        evidence_chunk_ids=[a.chunk_id,b.chunk_id])],repository=repo)
    writer.delete_files(repo,["a.py"])
    assert driver.execute_query("MATCH (a:GraphAssertion {repository:$repo}) RETURN count(a) AS n",
        repo=repo).records[0]["n"] == 1
    assert driver.execute_query("MATCH (:Entity {repository:$repo})-[r:USES]->() RETURN count(r) AS n",
        repo=repo).records[0]["n"] == 1


@pytest.mark.asyncio
async def test_real_gds_repositories_are_isolated_and_summaries_reused(graph):
    from ai_services.ingestion.community.pipeline import build_community_pipeline
    from ai_services.ingestion.community.config import CommunityConfig
    class Embedder:
        dimensions=3
        async def embed(self,texts): return [[1.,0.,0.] for _ in texts]
    def pipeline(seed_value):
        repo,run,writer,*_ = seed_value
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=json.dumps({
            "architectural_role":"Service Collaboration",
            "summary":"S uses T to implement the architectural service collaboration.",
            "key_entities":["S","T"]})))
        p = build_community_pipeline(writer.driver,llm,Embedder(),
            CommunityConfig(repository_id=repo,graph_name="gds_"+repo,
                            vector_index_await_timeout=10))
        return p,llm
    left,right = seed(graph),seed(graph)
    p,llm = pipeline(left)
    q,_ = pipeline(right)
    first,second = await asyncio.gather(p.run(),q.run())
    assert first.community_count == second.community_count == 1
    assert first.summaries_generated == 1
    reused = await p.run()
    assert reused.summaries_generated == 0 and reused.summaries_skipped_unchanged == 1
    assert reused.embeddings_generated == 0
    assert llm.ainvoke.await_count == 1
    records = graph[0].execute_query("""
        MATCH (e:Entity)-[:MEMBER_OF]->(c:Community)
        WHERE e.repository IN $repos AND e.repository <> c.repository
        RETURN count(e) AS n""",repos=[left[0],right[0]]).records
    assert records[0]["n"] == 0


@pytest.mark.asyncio
async def test_real_worker_full_then_incremental_preserves_unchanged_evidence(graph,tmp_path,monkeypatch):
    """Real Git + SQLite lifecycle + Neo4j/GDS; extraction and embeddings are fakes."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from ai_services.ingestion.persistence.models import Base, RepositoryModel
    from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
    from ai_services.ingestion.persistence.ingestion_jobs import IngestionJobs
    from ai_services.ingestion.worker import IngestionWorker
    from ai_services.ingestion.sources.github import GitHubRepositorySource
    from ai_services.ingestion.sources.interface import RepositoryMetadata
    from ai_services.ingestion.sources.credentials import GitHubCredential
    from ai_services.ingestion.rkg.pipeline import RepositoryKnowledgePipeline,PipelineConfig
    from ai_services.ingestion.rkg.store import JsonlCandidateKnowledgeStore
    from ai_services.ingestion.rkg.canonicalization import EntityCanonicalizer
    from ai_services.ingestion.rkg.candidate_generation import RelationshipCandidateGenerator
    from ai_services.ingestion.rkg.cross_chunk import CrossChunkReasoner
    from ai_services.ingestion.rkg.batching import TokenBudgetBatcher
    from ai_services.ingestion.rkg.models import CandidateKnowledge,ExtractedEntity,ExtractedRelationship
    from ai_services.tests.test_pr_ingestion import git
    repo = "stage_b_"+uuid.uuid4().hex
    origin=tmp_path/"origin"
    origin.mkdir()
    git(origin,"init")
    git(origin,"config","user.email","test@example.com")
    git(origin,"config","user.name","Test")
    for name in ("a.py","b.py"):
        (origin/name).write_text("def service():\n    return True\n")
    git(origin,"add",".")
    git(origin,"commit","-m","initial")
    target=[git(origin,"rev-parse","HEAD").strip()]
    engine=create_engine(f"sqlite:///{tmp_path/'worker.db'}")
    Base.metadata.create_all(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    with sessions.begin() as s:
        s.add(RepositoryModel(id=repo,github_repository_id="101",owner="owner",name="repo",
            full_name="owner/repo",repository_url="https://github.com/owner/repo",
            tracked_branch="main",installation_id="10"))
    store=SqliteApplicationStore(sessions)
    jobs=IngestionJobs(sessions)
    source=GitHubRepositorySource(tmp_path/"unused")
    source.get_metadata=AsyncMock(return_value=RepositoryMetadata(
        "101","owner","repo","owner/repo","main","main","https://github.com/owner/repo"))
    source.resolve_target=AsyncMock(side_effect=lambda *_:target[0])
    original=source._run_git
    def local(args,**kwargs):
        return original([str(origin) if str(a).startswith("https://github.com/") else a for a in args],**kwargs)
    source._run_git=local
    class Embedder:
        dimensions=3
        async def embed(self,texts): return [[1.,0.,0.] for _ in texts]
    extracted=[]
    async def extract(chunks,**kwargs):
        extracted.extend(c.file_path for c in chunks)
        return [CandidateKnowledge(chunk_id=c.chunk_id,entities=[
            ExtractedEntity(label="Service",name=name,source_chunk_ids=[c.chunk_id]) for name in ("S","T")],
            relationships=[ExtractedRelationship(source_label="Service",source_name="S",target_label="Service",
                target_name="T",relationship_type="USES",source_chunk_ids=[c.chunk_id])]) for c in chunks]
    async def validate(candidate,**kwargs):
        return ValidatedRelationship(source_id=candidate.source_id,target_id=candidate.target_id,
            relationship_type=candidate.relationship_type,confidence=.9,rationale="fixture",
            evidence_chunk_ids=candidate.evidence_chunk_ids)
    def knowledge(candidates_path,**kwargs):
        reasoner=CrossChunkReasoner(SimpleNamespace(validate=validate))
        return RepositoryKnowledgePipeline(Embedder(),SimpleNamespace(extract=extract),
            JsonlCandidateKnowledgeStore(candidates_path),EntityCanonicalizer(),
            RelationshipCandidateGenerator(),reasoner,TokenBudgetBatcher(len),
            PipelineConfig(embedding_dimensions=3))
    monkeypatch.setattr("ai_services.ingestion.rkg.builders.repo_ingestion_pipeline.build_knowledge_pipeline",knowledge)
    monkeypatch.setenv("ENABLE_BACKGROUND_PROCESSING","true")
    github=SimpleNamespace(indexing_credential=AsyncMock(return_value=GitHubCredential("synthetic")))
    llm=SimpleNamespace(ainvoke=AsyncMock(return_value=json.dumps({
        "architectural_role":"Service Collaboration",
        "summary":"S uses T to implement service collaboration.","key_entities":["S","T"]})))
    worker=IngestionWorker(jobs,store,source,github,graph[1],llm=llm,embedder=Embedder())
    first=jobs.enqueue(repo,target[0],branch="main",installation_id="10")
    finish = jobs.finish
    def publication_outage(*args,**kwargs):
        raise RuntimeError("Injected SQL publication failure after real Neo4j seal")
    jobs.finish = publication_outage
    await worker.run(first)
    assert jobs.get(first).status == "INDEXING"
    assert graph[1].snapshot(repo)["state"] == "COMPLETE"
    completed_extraction_count = len(extracted)
    jobs.finish = finish
    await worker.run(first)
    assert len(extracted) == completed_extraction_count
    assert jobs.get(first).status=="COMPLETED",jobs.get(first).error
    unchanged=graph[0].execute_query("MATCH (c:Chunk {repository:$repo,filePath:'b.py'}) RETURN c.id AS id",
        repo=repo).records[0]["id"]
    assert set(extracted)=={"a.py","b.py"}
    extracted.clear()
    # Keep a valid source file while changing only a.py.
    (origin/"a.py").write_text("def service():\n    return False\n")
    git(origin,"commit","-am","change a")
    target[0]=git(origin,"rev-parse","HEAD").strip()
    second=jobs.enqueue(repo,target[0],branch="main",installation_id="10",trigger="webhook_push",
                        delivery_id=repo+"delivery")
    await worker.run(second)
    assert jobs.get(second).status=="COMPLETED",jobs.get(second).error
    assert extracted and set(extracted)=={"a.py"}
    assert graph[0].execute_query("MATCH (c:Chunk {id:$id}) RETURN count(c) AS n",
        id=unchanged).records[0]["n"]==1
    assert jobs.enqueue(repo,target[0],branch="main",installation_id="10",trigger="webhook_push",
        delivery_id=repo+"delivery")==second
    from ai_services.ingestion.readiness import GraphReadBarrier
    assert GraphReadBarrier(sessions,graph[1]).snapshot(repo)[3]==target[0]
    engine.dispose()

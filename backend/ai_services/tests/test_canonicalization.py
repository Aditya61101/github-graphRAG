import asyncio

import pytest
from pydantic import ValidationError

from ai_services.ingestion.rkg.candidate_generation import (
    RelationshipCandidateGenerator,
)
from ai_services.ingestion.rkg.canonicalization import (
    AliasRegistry,
    EntityCanonicalizer,
)
from ai_services.ingestion.rkg.models import (
    CandidateKnowledge,
    CanonicalEntity,
    ChunkStrategy,
    EvidenceChunk,
    ExtractedEntity,
    ExtractedRelationship,
)
from ai_services.ingestion.rkg.neo4j_writer import Neo4jRepositoryWriter
from ai_services.ingestion.rkg.pipeline import (
    PipelineConfig,
    RepositoryKnowledgePipeline,
)


def extracted(label, name, chunk_id, **properties):
    return ExtractedEntity(
        label=label,
        name=name,
        properties=properties,
        source_chunk_ids=[chunk_id],
    )


def test_semantic_merge_removes_source_entity_and_redirects_all_mappings():
    def semantic_candidates(entity, candidates):
        if entity.name == "AccountSvc":
            return [candidate for candidate in candidates if candidate.name == "Account Service"]
        return []

    canonicalizer = EntityCanonicalizer(
        semantic_candidates=semantic_candidates,
        verifier=lambda entity, candidate: True,
    )
    result = canonicalizer.resolve(
        [
            extracted("Service", "Account Service", "chunk-a", owner="platform"),
            extracted("Service", "AccountSvc", "chunk-b", port=443),
        ],
        scope="repo:commit",
    )

    assert len(result.entities) == 1
    entity = next(iter(result.entities.values()))
    assert entity.name == "Account Service"
    assert entity.aliases == ["AccountSvc"]
    assert set(entity.evidence_chunk_ids) == {"chunk-a", "chunk-b"}
    assert entity.properties == {"owner": "platform", "port": 443}
    assert set(result.extracted_to_canonical.values()) == {entity.canonical_id}


def test_exact_identity_is_normalized_and_aliases_are_label_scoped():
    registry = AliasRegistry()
    registry.add("Service", "Config", "service-config")

    assert registry.resolve("service", " config ") == "service-config"
    assert registry.resolve("Database", "Config") is None

    canonicalizer = EntityCanonicalizer()
    result = canonicalizer.resolve(
        [
            extracted(" Service ", "AccountService", "chunk-a"),
            extracted("service", " accountservice ", "chunk-b"),
        ],
        scope="repo:commit",
    )

    assert len(result.entities) == 1
    entity = next(iter(result.entities.values()))
    assert set(entity.evidence_chunk_ids) == {"chunk-a", "chunk-b"}


def test_relationship_mapping_uses_structured_normalized_keys_not_raw_colon_keys():
    canonicalizer = EntityCanonicalizer()
    resolution = canonicalizer.resolve(
        [
            extracted("A:B", "C", "chunk-a"),
            extracted("A", "B:C", "chunk-a"),
        ],
        scope="repo:commit",
    )
    knowledge = CandidateKnowledge(
        chunk_id="chunk-a",
        relationships=[
            ExtractedRelationship(
                source_label=" a:b ",
                source_name=" c ",
                relationship_type="USES",
                target_label="a",
                target_name="b:c",
                source_chunk_ids=["chunk-a"],
            )
        ],
    )

    relationships = RelationshipCandidateGenerator().generate(
        candidates=[knowledge],
        canonical_entities=resolution.entities,
        extracted_to_canonical=resolution.extracted_to_canonical,
        chunks_by_id={},
    )

    assert len(relationships) == 1
    assert relationships[0].source_id != relationships[0].target_id


def test_invalid_entity_names_are_rejected_and_writer_preserves_reserved_identity_fields():
    with pytest.raises(ValidationError):
        ExtractedEntity(label="Service", name=None)
    with pytest.raises(ValidationError):
        ExtractedEntity(label="Service", name="  ")

    class RecordingDriver:
        def __init__(self):
            self.calls = []

        def execute_query(self, query, **kwargs):
            self.calls.append((query, kwargs))

    driver = RecordingDriver()
    writer = Neo4jRepositoryWriter(driver)
    valid = CanonicalEntity(
        canonical_id="valid-id",
        label="Service",
        name="Account Service",
        properties={"name": "wrong", "id": "wrong", "label": "wrong", "port": 443},
        evidence_chunk_ids=["chunk-a"],
    )
    invalid = CanonicalEntity.model_construct(
        canonical_id="invalid-id",
        label="Service",
        name=None,
        properties={},
        aliases=[],
        evidence_chunk_ids=[],
    )

    writer.write_entities([valid, invalid])

    payload = driver.calls[0][1]["entities"]
    assert len(payload) == 1
    assert payload[0]["properties"] == {
        "port": 443,
        "id": "valid-id",
        "name": "Account Service",
        "label": "Service",
        "aliases": [],
    }


def test_process_chunks_uses_repository_scope_and_rejects_mixed_repositories():
    class CachedStore:
        async def get_for_chunk(self, chunk_id):
            return CandidateKnowledge(chunk_id=chunk_id)

    class NeverUsedBatcher:
        def batches(self, chunks):
            assert not chunks
            return []

    class ScopeCapturingCanonicalizer(EntityCanonicalizer):
        def resolve(self, entities, *, scope):
            self.scope = scope
            return super().resolve(entities, scope=scope)

    canonicalizer = ScopeCapturingCanonicalizer()
    pipeline = RepositoryKnowledgePipeline(
        embedder=None,
        extractor=None,
        store=CachedStore(),
        canonicalizer=canonicalizer,
        candidate_generator=RelationshipCandidateGenerator(),
        cross_chunk_reasoner=None,
        batcher=NeverUsedBatcher(),
        config=PipelineConfig(),
    )
    mixed_repository_chunks = [
        EvidenceChunk.create("repo-a", "commit-1", "a.py", 0, "a", ChunkStrategy.WHOLE_FILE),
        EvidenceChunk.create("repo-b", "commit-1", "b.py", 0, "b", ChunkStrategy.WHOLE_FILE),
    ]

    with pytest.raises(ValueError, match="exactly one repository"):
        asyncio.run(pipeline.process_chunks(mixed_repository_chunks))

    same_repository_different_commits = [
        EvidenceChunk.create("repo-a", "commit-1", "a.py", 0, "a", ChunkStrategy.WHOLE_FILE),
        EvidenceChunk.create("repo-a", "commit-2", "a.py", 0, "a", ChunkStrategy.WHOLE_FILE),
    ]
    asyncio.run(pipeline.process_chunks(same_repository_different_commits))
    assert canonicalizer.scope == "repo-a"

"""Per-run planning artifacts and explicitly measured evidence coverage."""
from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from ai_services.ingestion.rkg.neo4j_writer import sanitize_relationship_type


@dataclass
class PersistedCounts:
    chunks: int = 0
    entities: int = 0
    relationships: int = 0
    assertions: int = 0


@dataclass
class IngestionStageCounts:
    discovered: int = 0
    planned: int = 0
    included: int = 0  # INCLUDE and LOW_PRIORITY, both ingested
    excluded: int = 0
    chunked: int = 0  # distinct files producing evidence
    chunks: int = 0
    extraction_chunks: int = 0
    extracted: int = 0  # extracted entity records before canonicalization
    extracted_relationships: int = 0
    canonicalized: int = 0
    candidates: int = 0
    validated: int = 0
    persisted: PersistedCounts | None = None  # unavailable until database verification
    communities: int | None = None  # None = not completed / unavailable

    def to_dict(self) -> dict:
        return asdict(self)


class IngestionRunAudit:
    def __init__(self, state_dir: Path, repository_id: str, run_id: str, **metadata):
        for value in (repository_id, run_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise ValueError("Unsafe ingestion artifact identifier")
        self.path = state_dir / "ingestion_runs" / repository_id / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.counts = IngestionStageCounts()
        self.metadata = {"repository_id": repository_id, "run_id": run_id, **metadata}
        self.write("run_metadata.json", self.metadata)
        self.save_counts()

    def write(self, name: str, data) -> None:
        target = self.path / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(target)

    def save_counts(self) -> None:
        self.write("stage_counts.json", self.counts.to_dict())

    def record_planner_response(self, content: str | None, metadata: dict) -> None:
        # Preserve even malformed JSON and coverage-invalid responses for diagnosis.
        self.write("planner_response.json", {"content": content, "metadata": metadata})

    def record_plan(self, manifest, plan, metadata: dict) -> None:
        self.write("planning_manifest.json", manifest.to_dict())
        self.write("planner_output.json", plan.model_dump(mode="json"))
        self.write("planner_metadata.json", metadata)
        self.counts.planned = len(plan.files)
        self.counts.excluded = sum(p.action.value == "exclude" for p in plan.files)
        self.counts.included = self.counts.planned - self.counts.excluded
        self.save_counts()

    def record_result(self, result) -> None:
        self.counts.chunked = len({c.file_path for c in result.chunks})
        self.counts.chunks = len(result.chunks)
        self.counts.extraction_chunks = len(result.candidate_knowledge)
        self.counts.extracted = sum(len(k.entities) for k in result.candidate_knowledge)
        self.counts.extracted_relationships = sum(len(k.relationships) for k in result.candidate_knowledge)
        self.counts.canonicalized = len(result.entities.entities) if result.entities else 0
        self.counts.candidates = len(result.relationship_candidates)
        self.counts.validated = len(result.validated_relationships)
        self.write("chunk_coverage.json", dict(sorted(Counter(c.file_path for c in result.chunks).items())))
        self.save_counts()

    def finish(self, status: str, **metadata) -> None:
        self.metadata.update(status=status, **metadata)
        self.write("run_metadata.json", self.metadata)
        self.save_counts()


def measure_persisted_counts(driver, database, repository_id, result) -> PersistedCounts:
    """One read query verifies objects from this run, not historical repo totals."""
    relationships = [{
        "source": r.source_id, "target": r.target_id,
        "type": sanitize_relationship_type(r.relationship_type),
    } for r in result.validated_relationships]
    assertion_ids = [f"{r['source']}|{r['type']}|{r['target']}" for r in relationships]
    records, _, _ = driver.execute_query(
        """
        CALL () {
            MATCH (c:Chunk {repository: $repo}) WHERE c.id IN $chunk_ids
            RETURN count(c) AS chunks
        }
        CALL () {
            MATCH (e:Entity {repository: $repo}) WHERE e.id IN $entity_ids
            RETURN count(e) AS entities
        }
        CALL () {
            UNWIND $relationships AS item
            MATCH (s:Entity {id: item.source, repository: $repo})-[r]->
                  (t:Entity {id: item.target, repository: $repo})
            WHERE type(r) = item.type
            RETURN count(DISTINCT r) AS relationships
        }
        CALL () {
            MATCH (a:GraphAssertion {repository: $repo}) WHERE a.id IN $assertion_ids
            RETURN count(a) AS assertions
        }
        RETURN chunks, entities, relationships, assertions
        """,
        repo=repository_id, chunk_ids=[c.chunk_id for c in result.chunks],
        entity_ids=list(result.entities.entities) if result.entities else [],
        relationships=relationships, assertion_ids=assertion_ids,
        database_=database, routing_="r",
    )
    if len(records) != 1:
        raise RuntimeError("Could not measure ingestion persistence coverage")
    return PersistedCounts(**dict(records[0]))

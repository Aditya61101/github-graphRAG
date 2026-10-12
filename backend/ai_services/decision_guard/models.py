from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_services.ingestion.adr.applicability import ApplicabilityMetadata

SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Identifier = Annotated[str, Field(min_length=1)]
Line = Annotated[int, Field(gt=0)]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(*parts: str) -> str:
    return digest(json.dumps(parts, ensure_ascii=False, separators=(",", ":")))


class Contract(BaseModel):
    # Tuples and nested frozen contracts provide deep immutability, unlike frozen lists.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RevisionIdentity(Contract):
    repository_id: Identifier
    github_repository_id: Identifier
    pull_request_number: int = Field(gt=0)
    base_sha: SHA
    head_sha: SHA
    merge_base_sha: SHA
    revision_key: Identifier


class BaselineSnapshot(Contract):
    repository_id: Identifier
    github_repository_id: Identifier
    tracked_branch: Identifier
    indexed_rkg_sha: SHA
    repository_status: Literal["COMPLETED", "INDEXING", "FAILED", "IDLE"]
    readiness: Literal["READY", "UPDATING", "DIRTY", "UNKNOWN"]
    graph_generation: int = Field(ge=0)
    ingestion_run_id: Identifier
    unfinished_ingestion: bool


class SourceLocation(Contract):
    path: Identifier
    start_line: Line
    end_line: Line

    @model_validator(mode="after")
    def ordered(self):
        if self.end_line < self.start_line:
            raise ValueError("Source range is reversed")
        return self


class Limitation(Contract):
    code: Identifier
    message: str
    paths: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()


class EvidenceSnapshot(Contract):
    evidence_id: Identifier
    repository_id: Identifier
    kind: Literal["CODE", "ADR", "GRAPH_ASSERTION"]
    origin: Literal[
        "MERGE_BASE", "TARGET_BASE", "PR_HEAD", "INDEXED_CODE", "ADR_DOCUMENT"
    ]
    path: str | None = None
    commit_sha: SHA | None = None
    chunk_id: str | None = None
    assertion_id: str | None = None
    adr_id: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    document_hash: str | None = None
    content_hash: str | None = None
    section: str | None = None
    location: SourceLocation | None = None
    excerpt: str
    excerpt_hash: str
    completeness: Literal["COMPLETE", "EXCERPT", "UNAVAILABLE"]
    placement: Literal["EXACT", "AMBIGUOUS", "UNLOCATED", "NOT_APPLICABLE"]

    @model_validator(mode="after")
    def validate_evidence(self):
        if digest(self.excerpt) != self.excerpt_hash:
            raise ValueError("Evidence excerpt hash mismatch")
        if self.location and (
            self.path != self.location.path or self.placement != "EXACT"
        ):
            raise ValueError("Located evidence requires an exact path placement")
        if self.kind == "ADR" and (
            not self.adr_id or not self.document_hash or not self.chunk_id
        ):
            raise ValueError("ADR evidence requires document and chunk provenance")
        return self


class ArchitecturalDelta(Contract):
    delta_id: Identifier
    kind: Literal[
        "API",
        "DEPENDENCY",
        "SCHEMA",
        "DATA_ACCESS",
        "WORKFLOW",
        "CONFIGURATION",
        "COMPONENT",
        "OTHER",
    ]
    path: Identifier
    previous_path: str | None = None
    change_status: Literal[
        "added", "modified", "removed", "renamed", "copied", "type_changed"
    ]
    before: str | None = None
    after: str | None = None
    symbols: tuple[str, ...] = ()
    old_location: SourceLocation | None = None
    head_location: SourceLocation | None = None
    evidence_ids: tuple[str, ...]
    target_comparison: Literal["UNCHANGED_BASE", "DIVERGED", "UNAVAILABLE"]
    analysis: Literal["TEXT", "METADATA_ONLY", "UNSUPPORTED"]


class AffectedEntityCandidate(Contract):
    entity_id: Identifier
    name: Identifier
    entity_type: Identifier
    aliases: tuple[str, ...] = ()
    origin: Literal["CODE", "ADR_ONLY", "NEW_CANDIDATE", "UNKNOWN"]
    mapping: Literal[
        "CHANGED_SYMBOL",
        "CHANGED_CHUNK",
        "FILE_CANDIDATE",
        "AMBIGUOUS",
        "NEIGHBOR",
        "NEW_CANDIDATE",
    ]
    paths: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    delta_ids: tuple[str, ...] = ()
    distance: int = Field(ge=0)
    reason: str


class ArchitecturalRelationship(Contract):
    relationship_id: Identifier
    source_id: Identifier
    relationship_type: Identifier
    target_id: Identifier
    provenance: Literal["OBSERVED_CODE", "ADR_ASSERTION", "UNKNOWN"]
    assertion_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1)
    rationale: str = ""


class RequirementEntityLink(Contract):
    entity_id: Identifier
    name: Identifier
    entity_type: Identifier
    relationship: Literal["AFFECTS", "CONSTRAINS"]
    origin: Literal["CODE", "ADR_ONLY", "UNKNOWN"]


class RequirementCandidate(Contract):
    requirement_id: Identifier
    kind: Literal["DECISION", "CONSTRAINT"]
    title: str | None = None
    statement: str
    requirement_type: str | None = None
    adr_ids: tuple[str, ...]
    target_entity_ids: tuple[str, ...] = ()
    target_links: tuple[RequirementEntityLink, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    discovery_reasons: tuple[str, ...]
    relevance_score: float = Field(ge=0)
    # Reuse the persisted enrichment envelope without mutable lists entering an
    # otherwise immutable context. The accessor returns a validated defensive copy.
    declared_applicability_json: str | None = None
    scope_metadata_status: Literal["VALID", "LEGACY", "INVALID"] = "LEGACY"
    enrichment_fingerprint: str = ""
    enrichment_status: str | None = None
    enrichment_model: str | None = None
    supported_community_ids: tuple[str, ...] = ()
    scope_evidence_ids: tuple[str, ...] = ()
    applicability: Literal["UNASSESSED"] = "UNASSESSED"
    policy_status: Literal["UNKNOWN"] = "UNKNOWN"

    @property
    def declared_applicability(self) -> ApplicabilityMetadata:
        if self.scope_metadata_status != "VALID":
            return ApplicabilityMetadata()
        return ApplicabilityMetadata.from_json(self.declared_applicability_json)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.scope_metadata_status == "VALID":
            if not self.declared_applicability_json:
                raise ValueError("Valid scope metadata requires the persisted envelope")
            ApplicabilityMetadata.from_json(self.declared_applicability_json)
        if not set(self.scope_evidence_ids) <= set(self.evidence_ids):
            raise ValueError("Scope evidence must be retained in requirement evidence")
        return self


class ADRIdentity(Contract):
    adr_id: Identifier
    content_hash: Identifier
    applicability_fingerprint: str = ""


class FileCoverage(Contract):
    path: Identifier
    status: Literal["TEXT", "METADATA_ONLY", "UNSUPPORTED", "BUDGET_OMITTED"]
    mapped: bool
    reasons: tuple[str, ...] = ()


class ContextCoverage(Contract):
    files: tuple[FileCoverage, ...]
    adr_retrieval: Literal["ABSENT", "AVAILABLE", "DEGRADED"]
    adr_catalog_complete: bool
    limitations: tuple[Limitation, ...]


class ContextBudget(Contract):
    token_limit: int = Field(gt=0)
    estimated_tokens: int = Field(ge=0)
    estimator: str


class ArchitectureContext(Contract):
    schema_version: Literal["4.1"] = "4.1"
    context_builder_version: Literal["1", "2"] = "2"
    revision: RevisionIdentity
    baseline: BaselineSnapshot
    coverage_mode: Literal["RKG_ONLY", "RKG_ADR", "RKG_ADR_DEGRADED"]
    adr_snapshot: tuple[ADRIdentity, ...]
    adr_catalog_hash: str
    deltas: tuple[ArchitecturalDelta, ...]
    entities: tuple[AffectedEntityCandidate, ...]
    relationships: tuple[ArchitecturalRelationship, ...]
    requirements: tuple[RequirementCandidate, ...]
    evidence: tuple[EvidenceSnapshot, ...]
    coverage: ContextCoverage
    budget: ContextBudget
    context_hash: str

    @model_validator(mode="after")
    def consistent(self):
        repo = self.revision.repository_id
        if (
            self.baseline.repository_id != repo
            or self.baseline.github_repository_id != self.revision.github_repository_id
            or self.baseline.indexed_rkg_sha != self.revision.base_sha
            or self.baseline.readiness != "READY"
            or self.baseline.repository_status != "COMPLETED"
            or self.baseline.unfinished_ingestion
        ):
            raise ValueError("Context requires a safe matching baseline")
        evidence = {e.evidence_id for e in self.evidence}
        if len(evidence) != len(self.evidence) or any(
            e.repository_id != repo for e in self.evidence
        ):
            raise ValueError("Evidence must be unique and repository-scoped")
        entities = {e.entity_id for e in self.entities}
        deltas = {d.delta_id for d in self.deltas}
        if len(entities) != len(self.entities) or len(deltas) != len(self.deltas):
            raise ValueError("Duplicate entity or delta identity")
        for item in (
            *self.entities,
            *self.relationships,
            *self.requirements,
            *self.deltas,
        ):
            if not set(item.evidence_ids) <= evidence:
                raise ValueError("Dangling evidence reference")
        for item in self.entities:
            if not set(item.delta_ids) <= deltas:
                raise ValueError("Dangling delta reference")
        for edge in self.relationships:
            if edge.source_id not in entities or edge.target_id not in entities:
                raise ValueError("Dangling relationship endpoint")
        if self.coverage_mode == "RKG_ONLY" and self.coverage.adr_retrieval != "ABSENT":
            raise ValueError("RKG_ONLY requires confirmed ADR absence")
        if self.budget.estimated_tokens > self.budget.token_limit:
            raise ValueError("Context exceeds token budget")
        return self

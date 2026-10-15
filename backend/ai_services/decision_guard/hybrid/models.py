"""Frozen retrieval metadata; no applicability, findings or proof verdicts."""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from ..budget import coverage_summary
from ..models import (
    ADRIdentity,
    ArchitecturalRelationship,
    BaselineSnapshot,
    Contract,
    EvidenceSnapshot,
    Limitation,
    RequirementCandidate,
    RevisionIdentity,
    digest,
)

Category = Literal["CODE", "ADR"]
Channel = Literal[
    "CODE_RKG", "CODE_BM25", "ADR_ASSOCIATION", "ADR_BM25", "ADR_SCOPE", "ADR_SEMANTIC"
]


def fingerprint(value):
    return digest(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


def vector_key(chunk, identity):
    return fingerprint(
        {
            "format": "dg-adr-chunk-vector-v1",
            "repository": chunk.repository_id,
            "adr": chunk.adr_id,
            "document_hash": chunk.document_hash,
            "chunk": chunk.chunk_id,
            "content_hash": chunk.content_hash,
            "identity": identity.model_dump(mode="json"),
        }
    )


class EmbeddingIdentity(Contract):
    provider: str = "azure-openai"
    model: str = "text-embedding-3-large"
    dimensions: int = 3072
    revision: str = "text-embedding-3-large:3072"
    provenance: Literal["VERIFIED_PROVIDER", "MOCK"] = "VERIFIED_PROVIDER"

    @model_validator(mode="after")
    def valid(self):
        if (
            not self.provider
            or not self.model
            or not self.revision
            or self.dimensions < 1
        ):
            raise ValueError("Invalid embedding identity")
        return self


class VectorCompatibilityManifest(Contract):
    schema_version: Literal["dg-vector-compatibility-v1"] = "dg-vector-compatibility-v1"
    configuration_id: str
    identity: EmbeddingIdentity
    confirmation: Literal[
        "USER_CONFIRMED_LEGACY", "CONFIGURATION_CONFIRMED", "MOCK_FIXTURE"
    ]
    confirmation_source: str
    embedded_text: Literal["ADRChunk.text"] = "ADRChunk.text"
    applies_to: tuple[Literal["RKG", "ADR"], ...] = ("RKG", "ADR")
    neo4j_model_metadata_claimed: Literal[False] = False

    @model_validator(mode="after")
    def valid(self):
        if not self.configuration_id or not self.confirmation_source:
            raise ValueError("Embedding configuration confirmation missing")
        if (self.confirmation == "MOCK_FIXTURE") != (
            self.identity.provenance == "MOCK"
        ):
            raise ValueError("Mock/real vector provenance mismatch")
        if self.confirmation == "USER_CONFIRMED_LEGACY" and (
            self.identity.provider,
            self.identity.model,
            self.identity.dimensions,
        ) != ("azure-openai", "text-embedding-3-large", 3072):
            raise ValueError(
                "Legacy confirmation applies only to the confirmed Azure configuration"
            )
        return self

    @property
    def manifest_hash(self):
        return fingerprint(self.model_dump(mode="json"))


def confirmed_legacy_manifest():
    return VectorCompatibilityManifest.model_validate_json(
        Path(__file__)
        .with_name("legacy_azure_3072_v1.json")
        .read_text(encoding="utf-8")
    )


class StoredVector(Contract):
    identity: EmbeddingIdentity
    key: str
    values: tuple[float, ...]

    @model_validator(mode="after")
    def valid(self):
        if (
            len(self.values) != self.identity.dimensions
            or not all(math.isfinite(v) for v in self.values)
            or not any(self.values)
        ):
            raise ValueError("Invalid/nonfinite/zero embedding vector")
        return self


class SearchUnit(Contract):
    canonical_id: str
    category: Category
    original_id: str
    source_hash: str
    evidence_ids: tuple[str, ...]
    entity_ids: tuple[str, ...] = ()
    entity_names: tuple[str, ...] = ()
    requirement: RequirementCandidate | None = None
    lexical_tokens: tuple[str, ...] = ()

    @model_validator(mode="after")
    def integrity(self):
        data = self.model_dump(mode="json", exclude={"source_hash"})
        if fingerprint(data) != self.source_hash:
            raise ValueError("Stale search-unit source hash")
        if self.category == "CODE" and self.requirement:
            raise ValueError("Code semantic retrieval is forbidden")
        if self.category == "ADR" and self.requirement is None:
            raise ValueError("ADR retrieval requires an original requirement")
        return self


class ADRChunkVector(Contract):
    repository_id: str
    adr_id: str
    document_hash: str
    chunk_id: str
    content_hash: str
    evidence_id: str
    requirement_ids: tuple[str, ...]
    vector: StoredVector
    metadata_basis: Literal[
        "LEGACY_CONFIRMED", "NEO4J_METADATA_VERIFIED", "MOCK_FIXTURE"
    ]
    observed_metadata_json: str = "{}"
    verified_metadata_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self):
        if self.vector.key != vector_key(self, self.vector.identity):
            raise ValueError("Stale/incompatible semantic source identity")
        observed = json.loads(self.observed_metadata_json)
        if set(observed) != set(self.verified_metadata_fields):
            raise ValueError("Unverified Neo4j embedding metadata claim")
        expected = {
            "embedding_model": self.vector.identity.model,
            "embedding_provider": self.vector.identity.provider,
            "embedding_dimensions": self.vector.identity.dimensions,
            "embedding_revision": self.vector.identity.revision,
            "embedding_text_hash": self.content_hash,
        }
        if any(observed.get(k) != expected[k] for k in observed):
            raise ValueError("Conflicting Neo4j embedding metadata")
        complete = set(expected) <= observed.keys()
        if self.metadata_basis == "NEO4J_METADATA_VERIFIED" and not complete:
            raise ValueError("Incomplete independently verified model metadata")
        if (self.metadata_basis == "MOCK_FIXTURE") != (
            self.vector.identity.provenance == "MOCK"
        ):
            raise ValueError("Chunk mock provenance mismatch")
        return self


class CommunityIdentity(Contract):
    repository_id: str
    community_id: str
    name: str | None = None
    title: str | None = None
    architectural_role: str | None = None
    aliases: tuple[str, ...] = ()


class SearchIndex(Contract):
    schema_version: Literal["dg-search-v2"] = "dg-search-v2"
    index_id: str
    baseline: BaselineSnapshot
    catalog: tuple[ADRIdentity, ...] = ()
    catalog_hash: str
    adr_status: Literal["ABSENT", "AVAILABLE", "DEGRADED"]
    catalog_complete: bool
    units: tuple[SearchUnit, ...]
    evidence: tuple[EvidenceSnapshot, ...]
    vector_manifest: VectorCompatibilityManifest
    adr_chunks: tuple[ADRChunkVector, ...] = ()
    community_links: tuple[tuple[str, str], ...] = ()
    communities: tuple[CommunityIdentity, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    mode: Literal["ARCHIVED", "LIVE"] = "LIVE"
    embedding_requests: int = 0
    embeddings_reused: int = 0

    @model_validator(mode="after")
    def integrity(self):
        data = self.model_dump(
            mode="json", exclude={"index_id", "embedding_requests", "embeddings_reused"}
        )
        if fingerprint(data) != self.index_id:
            raise ValueError("Search-index manifest hash mismatch")
        if (
            fingerprint([a.model_dump(mode="json") for a in self.catalog])
            != self.catalog_hash
        ):
            raise ValueError("Search-index ADR catalog mismatch")
        ev = {e.evidence_id: e for e in self.evidence}
        if len(ev) != len(self.evidence) or len(
            {u.canonical_id for u in self.units}
        ) != len(self.units):
            raise ValueError("Duplicate index source identity")
        catalog = {a.adr_id: a.content_hash for a in self.catalog}
        if any(
            c.repository_id != self.baseline.repository_id for c in self.communities
        ) or len({c.community_id for c in self.communities}) != len(self.communities):
            raise ValueError("Community identity escaped repository or duplicated")
        if not {cid for _, cid in self.community_links} <= {
            c.community_id for c in self.communities
        }:
            raise ValueError("Community membership missing original identity")
        for e in self.evidence:
            if e.repository_id != self.baseline.repository_id:
                raise ValueError("Index evidence escaped repository")
            if e.kind == "CODE" and (
                e.origin != "INDEXED_CODE" or e.placement != "EXACT"
            ):
                raise ValueError("Index code must be verified baseline evidence")
            if e.kind == "ADR" and catalog.get(e.adr_id) != e.document_hash:
                raise ValueError("Index ADR source escaped catalog")
        for unit in self.units:
            if not unit.evidence_ids or not set(unit.evidence_ids) <= ev.keys():
                raise ValueError("Index unit missing original source")
            if any(ev[eid].kind != unit.category for eid in unit.evidence_ids):
                raise ValueError("Index unit mixes evidence categories")
            if unit.requirement and not set(unit.requirement.evidence_ids) <= set(
                unit.evidence_ids
            ):
                raise ValueError("Index requirement missing policy support")
        requirements = {
            u.original_id: u.requirement for u in self.units if u.requirement
        }
        if len({c.chunk_id for c in self.adr_chunks}) != len(self.adr_chunks):
            raise ValueError("Duplicate ADR chunk vector identity")
        for c in self.adr_chunks:
            e = ev.get(c.evidence_id)
            if (
                c.repository_id != self.baseline.repository_id
                or catalog.get(c.adr_id) != c.document_hash
                or c.vector.identity != self.vector_manifest.identity
            ):
                raise ValueError(
                    "Chunk vector escaped repository/catalog/confirmed model"
                )
            if e is None or (
                e.kind,
                e.adr_id,
                e.chunk_id,
                e.content_hash,
                e.document_hash,
            ) != ("ADR", c.adr_id, c.chunk_id, c.content_hash, c.document_hash):
                raise ValueError("Chunk vector missing original evidence")
            if any(
                rid not in requirements
                or c.adr_id not in requirements[rid].adr_ids
                or c.evidence_id not in requirements[rid].evidence_ids
                for rid in c.requirement_ids
            ):
                raise ValueError("Chunk SUPPORTS mapping missing coherent policy group")
        if self.adr_status == "ABSENT" and (self.catalog or not self.catalog_complete):
            raise ValueError("Unverified ADR absence")
        return self


@dataclass(frozen=True)
class IndexSettings:
    max_code_units: int = 4096
    max_adr_units: int = 2048
    max_documents: int = 256
    max_source_reads: int = 1024
    max_source_bytes: int = 64 * 1024 * 1024
    max_index_text_chars: int = 8000
    max_adr_chunks: int = 2048
    deadline_seconds: int = 120

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in self.__dict__.values()):
            raise ValueError("Index bounds must be positive integers")


@dataclass(frozen=True)
class RetrievalSettings:
    structural: bool = True
    lexical: bool = True
    scope: bool = True
    semantic: bool = True
    results_per_query: int = 64
    semantic_chunks_per_query: int = 64
    semantic_requirements_per_query: int = 192
    results_per_seed: int = 192
    results_per_file: int = 192
    max_code_candidates: int = 96
    max_adr_candidates: int = 96
    max_candidates: int = 192
    max_queries: int = 96
    max_index_units: int = 6144
    max_semantic_chunks: int = 2048
    max_index_bytes: int = 32 * 1024 * 1024
    deadline_seconds: float = 30.0
    rrf_k: int = 60
    max_redundant: int = 3
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    # Channel weights are ranks-only; no raw BM25/cosine score mixing.
    weights: tuple[tuple[str, float], ...] = ()

    def __post_init__(self):
        if any(
            type(getattr(self, k)) is not bool
            for k in ("structural", "lexical", "scope", "semantic")
        ):
            raise ValueError("Retrieval channel flags must be booleans")
        for key, value in self.__dict__.items():
            if (
                key.startswith(("max_", "results_", "semantic_"))
                and key != "semantic"
                or key == "rrf_k"
            ) and (type(value) is not int or value < 1):
                raise ValueError("Retrieval limits must be positive integers")
        if (
            not math.isfinite(self.deadline_seconds)
            or self.deadline_seconds <= 0
            or not math.isfinite(self.bm25_k1)
            or self.bm25_k1 <= 0
            or not 0 <= self.bm25_b <= 1
        ):
            raise ValueError("Invalid retrieval/scoring configuration")
        if len(dict(self.weights)) != len(self.weights) or any(
            not math.isfinite(w) or w <= 0 for _, w in self.weights
        ):
            raise ValueError("Invalid RRF channel weights")


class ChannelRank(Contract):
    channel: Channel
    position: int
    seed_ids: tuple[str, ...]
    query_id: str | None = None
    raw_score: float | None = None
    hints: tuple[str, ...] = ()
    source_chunk_id: str | None = None
    source_evidence_id: str | None = None
    source_vector_key: str | None = None


class SemanticChunkHit(Contract):
    chunk_id: str
    evidence_id: str
    vector_key: str
    position: int
    cosine: float
    query_id: str
    seed_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    metadata_basis: Literal[
        "LEGACY_CONFIRMED", "NEO4J_METADATA_VERIFIED", "MOCK_FIXTURE"
    ]


class RankedCandidate(Contract):
    canonical_id: str
    category: Category
    original_id: str
    source_hash: str
    evidence_ids: tuple[str, ...]
    seed_ids: tuple[str, ...]
    channels: tuple[ChannelRank, ...]
    rrf_score: float
    rank: int
    relationship_ids: tuple[str, ...] = ()
    hydration_required: Literal[True] = True


class ChannelCoverage(Contract):
    channel: Channel
    status: Literal[
        "READY", "EMPTY", "UNAVAILABLE", "FAILED", "STALE", "TRUNCATED", "DISABLED"
    ]
    queries: int = 0
    distinct_candidates: int = 0
    detail: str = ""


class RetrievalUsage(Contract):
    embedding_requests: int = 0
    embedding_estimated_tokens: int = 0
    query_count: int = 0
    index_units_read: int = 0
    latency_seconds: float = 0.0
    semantic_provenance: Literal["UNMEASURED", "MOCK", "VERIFIED_PROVIDER"] = (
        "UNMEASURED"
    )


class EvidencePlan(Contract):
    schema_version: Literal["dg-evidence-plan-v2"] = "dg-evidence-plan-v2"
    revision: RevisionIdentity
    baseline: BaselineSnapshot
    index_id: str | None
    adr_status: Literal["ABSENT", "AVAILABLE", "DEGRADED"]
    ranked_code: tuple[RankedCandidate, ...]
    ranked_adr: tuple[RankedCandidate, ...]
    evidence: tuple[EvidenceSnapshot, ...]
    requirements: tuple[RequirementCandidate, ...]
    relationships: tuple[ArchitecturalRelationship, ...]
    channels: tuple[ChannelCoverage, ...]
    limitations: tuple[Limitation, ...]
    usage: RetrievalUsage
    source_freshness: Literal["SNAPSHOT_ONLY"] = "SNAPSHOT_ONLY"
    vector_manifest: VectorCompatibilityManifest | None = None
    semantic_chunks: tuple[SemanticChunkHit, ...] = ()

    @model_validator(mode="after")
    def integrity(self):
        if (
            self.baseline.repository_id != self.revision.repository_id
            or self.baseline.indexed_rkg_sha != self.revision.base_sha
        ):
            raise ValueError("Plan escaped discovery baseline")
        evidence = {e.evidence_id: e for e in self.evidence}
        if len(evidence) != len(self.evidence) or any(
            e.repository_id != self.revision.repository_id for e in self.evidence
        ):
            raise ValueError("Plan source identity/repository mismatch")
        candidates = (*self.ranked_code, *self.ranked_adr)
        if len({c.canonical_id for c in candidates}) != len(candidates):
            raise ValueError("Duplicate ranked candidate identity")
        for category, rows in (("CODE", self.ranked_code), ("ADR", self.ranked_adr)):
            for rank, candidate in enumerate(rows, 1):
                if (
                    candidate.category != category
                    or candidate.rank != rank
                    or not set(candidate.evidence_ids) <= evidence.keys()
                ):
                    raise ValueError("Plan category/rank/source mismatch")
        for r in (*self.requirements, *self.relationships):
            if not set(r.evidence_ids) <= evidence.keys():
                raise ValueError("Plan omitted a coherent support group")
        chunk_hits = {(h.query_id, h.chunk_id): h for h in self.semantic_chunks}
        for h in self.semantic_chunks:
            if (
                h.evidence_id not in evidence
                or evidence[h.evidence_id].chunk_id != h.chunk_id
                or evidence[h.evidence_id].kind != "ADR"
                or not math.isfinite(h.cosine)
            ):
                raise ValueError("Semantic hit missing original chunk source")
        for c in candidates:
            for h in c.channels:
                if h.channel != "ADR_SEMANTIC":
                    continue
                chunk = chunk_hits.get((h.query_id, h.source_chunk_id))
                if (
                    chunk is None
                    or c.original_id not in chunk.requirement_ids
                    or h.position != chunk.position
                    or h.source_vector_key != chunk.vector_key
                    or h.source_evidence_id != chunk.evidence_id
                ):
                    raise ValueError(
                        "Semantic projection misrepresents chunk rank/source/mapping"
                    )
        return self

    @property
    def compact_coverage(self):
        result = coverage_summary(self.limitations, self.revision.repository_id)
        result.update(
            adr_status=self.adr_status,
            source_freshness=self.source_freshness,
            channels=[c.model_dump(mode="json") for c in self.channels],
        )
        return result

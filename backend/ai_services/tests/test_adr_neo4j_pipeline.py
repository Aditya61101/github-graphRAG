from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_services.embeddings.base import Embedder
from ai_services.ingestion.adr.chunker import ADRChunker
from ai_services.ingestion.adr.extractor import ADRArchitecturalExtractor
from ai_services.ingestion.adr.models import (
    ADRChunk,
    ADRConstraintOutput,
    ADRDecisionOutput,
    ADREntityOutput,
    ADRExtractionResult,
    ADRRelationshipOutput,
    ArchitecturalConstraintNode,
    ArchitecturalDecisionNode,
)
from ai_services.ingestion.adr.neo4j_writer import ADRNeo4jWriter
from ai_services.ingestion.adr.processor import (
    ADRProcessingError,
    ADRProcessingService,
)
from ai_services.ingestion.adr.resolver import ADREntityResolver
from ai_services.ingestion.adr.service import ADRService
from ai_services.ingestion.persistence.models import (
    ADRModel,
    Base,
    GitHubConnectionModel,
    RepositoryModel,
    UserModel,
)
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.ingestion.rkg.canonicalization import EntityCanonicalizer
from ai_services.ingestion.rkg.models import CanonicalEntity
from ai_services.models.adr_document import ADRDocument, ADRSection


# =========================================================================
# Mock Graph Driver for Testing Neo4j Interactions
# =========================================================================
class MockTransaction:
    """Mock Neo4j transaction tracking mutations with rollback support."""

    def __init__(self, driver: MockNeo4jDriver) -> None:
        self.driver = driver
        self.committed = False
        self._snapshot_nodes = {k: dict(v) for k, v in driver.nodes.items()}
        self._snapshot_rels = [dict(r) for r in driver.relationships]

    def __enter__(self) -> MockTransaction:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None or not self.committed:
            self.rollback()

    def run(self, query: str, parameters: dict[str, Any] | None = None, **kwparams: Any) -> tuple[list[Any], Any, Any]:
        params = dict(parameters or {})
        params.update(kwparams)
        for pattern in self.driver.fail_queries_matching:
            if pattern in query:
                raise RuntimeError(f"Simulated Neo4j transaction failure on query matching: '{pattern}'")
        return self.driver._execute_cypher(query, params)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.driver.nodes = {k: dict(v) for k, v in self._snapshot_nodes.items()}
        self.driver.relationships = [dict(r) for r in self._snapshot_rels]


class MockNeo4jSession:
    """Mock Neo4j session offering transaction management."""

    def __init__(self, driver: MockNeo4jDriver) -> None:
        self.driver = driver

    def __enter__(self) -> MockNeo4jSession:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def begin_transaction(self) -> MockTransaction:
        return MockTransaction(self.driver)


class MockNeo4jDriver:
    """In-memory mock of Neo4j driver tracking nodes, relationships, and queries."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.nodes: dict[str, dict[str, Any]] = {}  # key -> properties
        self.relationships: list[dict[str, Any]] = []
        self.fail_queries_matching: list[str] = []

    def session(self, database: str | None = None) -> MockNeo4jSession:
        return MockNeo4jSession(self)

    def _add_relationship(self, rel: dict[str, Any]) -> None:
        """Idempotently add a relationship mimicking Neo4j MERGE semantics."""
        for existing in self.relationships:
            if (
                existing.get("source") == rel.get("source")
                and existing.get("type") == rel.get("type")
                and existing.get("target") == rel.get("target")
            ):
                return
        self.relationships.append(rel)

    def execute_query(self, query: str, database_: str = "neo4j", **params: Any) -> tuple[list[Any], Any, Any]:
        for pattern in self.fail_queries_matching:
            if pattern in query:
                raise RuntimeError(f"Simulated Neo4j failure on query matching: '{pattern}'")
        return self._execute_cypher(query, params)

    def _execute_cypher(self, query: str, params: dict[str, Any]) -> tuple[list[Any], Any, Any]:
        self.calls.append((query, params))

        # 1. Loading existing entities: MATCH (e:Entity {repository: $repo})
        if "MATCH (e:Entity {repository: $repo})" in query and "RETURN e.id" in query:
            repo = params.get("repo")
            records = []
            for nid, node in self.nodes.items():
                if node.get("_label") == "Entity" and node.get("repository") == repo:
                    records.append(
                        {
                            "id": node.get("id"),
                            "name": node.get("name"),
                            "label": node.get("label"),
                            "aliases": node.get("aliases", []),
                            "embedding": node.get("embedding"),
                        }
                    )
            return records, None, None

        # 2. Deleting stale ADR knowledge
        if "OPTIONAL MATCH (adr)-[:HAS_CHUNK]->(ch:Chunk" in query:
            adr_id = params.get("adr_id")
            repo = params.get("repo")
            to_del = [
                k for k, n in self.nodes.items()
                if (n.get("_label") in {"Chunk", "ArchitecturalDecision", "ArchitecturalConstraint"} and n.get("adr_id") == adr_id)
                or (n.get("_label") == "GraphAssertion" and n.get("source_id") == adr_id)
            ]
            for k in to_del:
                del self.nodes[k]
            self.relationships = [
                r for r in self.relationships
                if r.get("adr_id") != adr_id and r.get("source_id") != adr_id
            ]
            return [], None, None

        # 3. Upserting ADR node
        if "MERGE (adr:ADR {id: $adr_id})" in query:
            adr_id = params.get("adr_id")
            self.nodes[f"ADR:{adr_id}"] = {
                "_label": "ADR",
                "id": adr_id,
                "repository": params.get("repo"),
                "title": params.get("title"),
                "content_hash": params.get("content_hash"),
                "source_type": params.get("source_type"),
                "source_name": params.get("source_name"),
            }
            return [], None, None

        # 4. Upserting Chunks
        if "MERGE (c:Chunk:ADRChunk {id: item.id})" in query:
            chunks = params.get("chunks", [])
            adr_id = params.get("adr_id")
            repo = params.get("repo")
            for c in chunks:
                cid = c["id"]
                self.nodes[f"Chunk:{cid}"] = {
                    "_label": "Chunk",
                    "id": cid,
                    "adr_id": adr_id,
                    "repository": repo,
                    "section": c.get("section"),
                    "text": c.get("text"),
                    "content_hash": c.get("content_hash"),
                    "embedding": c.get("embedding"),
                }
                self._add_relationship(
                    {
                        "source": f"ADR:{adr_id}",
                        "type": "HAS_CHUNK",
                        "target": f"Chunk:{cid}",
                        "adr_id": adr_id,
                    }
                )
            return [], None, None

        # 5. Upserting Entities
        if "MERGE (e:Entity {id: item.id})" in query:
            entities = params.get("entities", [])
            repo = params.get("repo")
            adr_id = params.get("adr_id")
            for e in entities:
                eid = e["id"]
                self.nodes[f"Entity:{eid}"] = {
                    "_label": "Entity",
                    "id": eid,
                    "name": e.get("name"),
                    "label": e.get("label"),
                    "repository": repo,
                    "source": "ADR",
                    "source_id": adr_id,
                    "embedding": e.get("embedding"),
                }
            return [], None, None

        # 6. Upserting ArchitecturalDecisions
        if "MERGE (d:ArchitecturalDecision {id: item.id})" in query:
            decisions = params.get("decisions", [])
            adr_id = params.get("adr_id")
            repo = params.get("repo")
            for d in decisions:
                did = d["id"]
                self.nodes[f"ArchitecturalDecision:{did}"] = {
                    "_label": "ArchitecturalDecision",
                    "id": did,
                    "adr_id": adr_id,
                    "repository": repo,
                    "title": d.get("title"),
                    "description": d.get("description"),
                    "decision_type": d.get("decision_type"),
                }
                self._add_relationship(
                    {
                        "source": f"ADR:{adr_id}",
                        "type": "DEFINES",
                        "target": f"ArchitecturalDecision:{did}",
                        "adr_id": adr_id,
                    }
                )
                for chunk_id in d.get("evidence", []):
                    self._add_relationship(
                        {
                            "source": f"Chunk:{chunk_id}",
                            "type": "SUPPORTS",
                            "target": f"ArchitecturalDecision:{did}",
                            "adr_id": adr_id,
                        }
                    )
                for aff_id in d.get("affects", []):
                    self._add_relationship(
                        {
                            "source": f"ArchitecturalDecision:{did}",
                            "type": "AFFECTS",
                            "target": f"Entity:{aff_id}",
                            "adr_id": adr_id,
                        }
                    )
                    self._add_relationship(
                        {
                            "source": f"ADR:{adr_id}",
                            "type": "AFFECTS",
                            "target": f"Entity:{aff_id}",
                            "adr_id": adr_id,
                        }
                    )
                for con_id in d.get("constrains", []):
                    self._add_relationship(
                        {
                            "source": f"ArchitecturalDecision:{did}",
                            "type": "CONSTRAINS",
                            "target": f"Entity:{con_id}",
                            "adr_id": adr_id,
                        }
                    )
                    self._add_relationship(
                        {
                            "source": f"ADR:{adr_id}",
                            "type": "CONSTRAINS",
                            "target": f"Entity:{con_id}",
                            "adr_id": adr_id,
                        }
                    )
            return [], None, None

        # 7. First-class ArchitecturalConstraint nodes
        if "MERGE (c:ArchitecturalConstraint {id: item.id})" in query:
            constraints = params.get("constraints", [])
            adr_id = params.get("adr_id")
            repo = params.get("repo")
            for c in constraints:
                cid = c["id"]
                self.nodes[f"ArchitecturalConstraint:{cid}"] = {
                    "_label": "ArchitecturalConstraint",
                    "id": cid,
                    "adr_id": adr_id,
                    "repository": repo,
                    "description": c.get("description"),
                    "constraint_type": c.get("constraint_type"),
                }
                self._add_relationship(
                    {
                        "source": f"ADR:{adr_id}",
                        "type": "DEFINES",
                        "target": f"ArchitecturalConstraint:{cid}",
                        "adr_id": adr_id,
                    }
                )
                for chunk_id in c.get("evidence", []):
                    self._add_relationship(
                        {
                            "source": f"Chunk:{chunk_id}",
                            "type": "SUPPORTS",
                            "target": f"ArchitecturalConstraint:{cid}",
                            "adr_id": adr_id,
                        }
                    )
                for con_id in c.get("constrains", []):
                    self._add_relationship(
                        {
                            "source": f"ArchitecturalConstraint:{cid}",
                            "type": "CONSTRAINS",
                            "target": f"Entity:{con_id}",
                            "adr_id": adr_id,
                        }
                    )
                    self._add_relationship(
                        {
                            "source": f"ADR:{adr_id}",
                            "type": "CONSTRAINS",
                            "target": f"Entity:{con_id}",
                            "adr_id": adr_id,
                        }
                    )
            return [], None, None

        # 8. Direct ADR Constraints
        if "MERGE (adr)-[:CONSTRAINS]->(e)" in query:
            adr_id = params.get("adr_id")
            target_ids = params.get("target_ids", [])
            for tid in target_ids:
                self._add_relationship(
                    {
                        "source": f"ADR:{adr_id}",
                        "type": "CONSTRAINS",
                        "target": f"Entity:{tid}",
                        "adr_id": adr_id,
                    }
                )
            return [], None, None

        # 9. Assertions and Entity-to-Entity Relationships
        if "MERGE (a:GraphAssertion {id: item.assertion_id})" in query:
            relationships = params.get("relationships", [])
            adr_id = params.get("adr_id")
            for r in relationships:
                aid = r["assertion_id"]
                self.nodes[f"GraphAssertion:{aid}"] = {
                    "_label": "GraphAssertion",
                    "id": aid,
                    "repository": params.get("repo"),
                    "source_id": adr_id,
                    "relationship_type": r.get("relationship_type"),
                    "rationale": r.get("rationale"),
                }
                self._add_relationship(
                    {
                        "source": f"Entity:{r['source_id']}",
                        "type": "ASSERTS",
                        "target": f"GraphAssertion:{aid}",
                        "adr_id": adr_id,
                    }
                )
                self._add_relationship(
                    {
                        "source": f"GraphAssertion:{aid}",
                        "type": "TARGETS",
                        "target": f"Entity:{r['target_id']}",
                        "adr_id": adr_id,
                    }
                )
                for cid in r.get("evidence", []):
                    self._add_relationship(
                        {
                            "source": f"Chunk:{cid}",
                            "type": "SUPPORTS",
                            "target": f"GraphAssertion:{aid}",
                            "adr_id": adr_id,
                        }
                    )
            return [], None, None

        # 10. Direct Cypher relationship between entities
        if "MERGE (s)-[r:" in query:
            m = re.search(r"MERGE \(s\)-\[r:([A-Za-z0-9_]+)\]->\(t\)", query)
            rel_type = m.group(1) if m else "RELATES_TO"
            relationships = params.get("relationships", [])
            adr_id = params.get("adr_id")
            for r in relationships:
                self._add_relationship(
                    {
                        "source": f"Entity:{r['source_id']}",
                        "type": rel_type,
                        "target": f"Entity:{r['target_id']}",
                        "adr_id": adr_id,
                    }
                )
            return [], None, None

        return [], None, None


# =========================================================================
# Mock Embedder
# =========================================================================
class MockEmbedder:
    """Mock embedder returning deterministic 3072-dimensional vectors."""

    def __init__(self, dimensions: int = 3072) -> None:
        self._dimensions = dimensions
        self.should_fail = False

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.should_fail:
            raise RuntimeError("Simulated embedding service unavailable.")
        return [[0.05] * self._dimensions for _ in texts]


# =========================================================================
# Mock LLM Extractor
# =========================================================================
class MockArchitecturalExtractor:
    """Mock extractor returning configurable ADRExtractionResult."""

    def __init__(self, result: ADRExtractionResult | None = None) -> None:
        self.result = result or ADRExtractionResult()
        self.should_fail = False

    async def extract_chunk(self, chunk: ADRChunk) -> ADRExtractionResult:
        if self.should_fail:
            raise RuntimeError("Simulated LLM architectural extraction error.")
        return self.result

    async def extract_all(self, chunks: list[ADRChunk]) -> list[tuple[ADRChunk, ADRExtractionResult]]:
        if self.should_fail:
            raise RuntimeError("Simulated LLM architectural extraction error.")
        return [(c, self.result) for c in chunks]


# =========================================================================
# Test Fixture
# =========================================================================
@pytest.fixture
def adr_env():
    """Build isolated environment for ADR Phase 2 pipeline tests."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        storage_path = temp_path / "adrs"
        storage_path.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        store = SqliteApplicationStore(session_factory=session_factory)

        # Seed Users and Repositories
        with session_factory() as session:
            alice = UserModel(id="usr_alice", username="alice", email="alice@test.com")
            session.add(alice)
            conn_alice = GitHubConnectionModel(
                id="conn_alice",
                user_id="usr_alice",
                github_user_id="1010",
                access_token="token_alice",
            )
            session.add(conn_alice)
            repo_a = RepositoryModel(
                id="repo_a",
                github_repository_id="1001",
                owner="alice",
                name="repo-a",
                full_name="alice/repo-a",
                repository_url="https://github.com/alice/repo-a",
                default_branch="main",
                tracked_branch="main",
                user_id="usr_alice",
                github_connection_id="conn_alice",
                status="COMPLETED",
            )
            repo_b = RepositoryModel(
                id="repo_b",
                github_repository_id="2002",
                owner="alice",
                name="repo-b",
                full_name="alice/repo-b",
                repository_url="https://github.com/alice/repo-b",
                default_branch="main",
                tracked_branch="main",
                user_id="usr_alice",
                github_connection_id="conn_alice",
                status="COMPLETED",
            )
            session.add_all([repo_a, repo_b])
            session.commit()

        mock_driver = MockNeo4jDriver()
        mock_embedder = MockEmbedder(dimensions=3072)
        mock_extractor = MockArchitecturalExtractor()

        resolver = ADREntityResolver(
            driver=mock_driver,
            database="neo4j",
            embedder=mock_embedder,
            embedding_dimensions=3072,
        )
        writer = ADRNeo4jWriter(
            driver=mock_driver,
            database="neo4j",
            embedding_dimensions=3072,
        )
        processor = ADRProcessingService(
            chunker=ADRChunker(),
            extractor=mock_extractor,
            resolver=resolver,
            writer=writer,
            embedder=mock_embedder,
            embedding_dimensions=3072,
        )
        service = ADRService(
            sqlite_store=store,
            storage_dir=storage_path,
            max_file_size_bytes=10 * 1024 * 1024,
            processor=processor,
        )

        yield {
            "store": store,
            "driver": mock_driver,
            "embedder": mock_embedder,
            "extractor": mock_extractor,
            "resolver": resolver,
            "writer": writer,
            "processor": processor,
            "service": service,
            "repo_a": "repo_a",
            "repo_b": "repo_b",
            "user_id": "usr_alice",
        }


# =========================================================================
# 1. Basic Ingestion (ADRDocument -> Chunks -> Embeddings -> LLM -> Neo4j)
# =========================================================================
@pytest.mark.asyncio
async def test_basic_adr_ingestion_pipeline(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    # Configure structured extraction
    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="Use PostgreSQL for Transactional Storage",
                description="Adopt PostgreSQL as primary transactional database.",
                decision_type="technology_selection",
                affects=["PostgreSQL", "OrderService"],
                constrains=["MongoDB"],
            )
        ],
        constraints=[
            ADRConstraintOutput(
                description="Frontend must not access database directly.",
                constraint_type="data_access_rule",
                target_entities=["FrontendApp"],
            )
        ],
        entities=[
            ADREntityOutput(name="OrderService", label="Service"),
            ADREntityOutput(name="PostgreSQL", label="Database"),
            ADREntityOutput(name="MongoDB", label="Database"),
            ADREntityOutput(name="FrontendApp", label="Component"),
        ],
        relationships=[
            ADRRelationshipOutput(
                source_name="OrderService",
                source_label="Service",
                relationship_type="USES",
                target_name="PostgreSQL",
                target_label="Database",
                rationale="Persistent storage for orders.",
            )
        ],
    )

    adr_content = (
        "# ADR 001: Transactional Storage\n\n"
        "## Context\nHigh reliability needed.\n\n"
        "## Decision\nUse PostgreSQL for Transactional Storage.\n\n"
        "## Consequences\nMongoDB is retired for transactional operations."
    )

    record, doc = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="001-storage.md",
        file_bytes=adr_content.encode("utf-8"),
    )

    assert record.status == "COMPLETED"
    assert record.title == "ADR 001: Transactional Storage"

    # Verify ADR node in Neo4j
    adr_node = driver.nodes.get(f"ADR:{record.id}")
    assert adr_node is not None
    assert adr_node["repository"] == "repo_a"

    # Verify Chunks created with deterministic IDs
    chunk_nodes = [n for k, n in driver.nodes.items() if n.get("_label") == "Chunk"]
    assert len(chunk_nodes) >= 1
    for c in chunk_nodes:
        assert c["id"].startswith(f"adr:{record.id}:chunk:")
        assert c["embedding"] is not None
        assert len(c["embedding"]) == 3072

    # Verify ArchitecturalDecision node
    dec_nodes = [n for k, n in driver.nodes.items() if n.get("_label") == "ArchitecturalDecision"]
    assert len(dec_nodes) == 1
    assert dec_nodes[0]["title"] == "Use PostgreSQL for Transactional Storage"

    # Verify Relationships: DEFINES, AFFECTS, CONSTRAINS
    rel_types = {r["type"] for r in driver.relationships}
    assert "DEFINES" in rel_types
    assert "AFFECTS" in rel_types
    assert "CONSTRAINS" in rel_types
    assert "HAS_CHUNK" in rel_types


# =========================================================================
# 2. Repository Isolation
# =========================================================================
@pytest.mark.asyncio
async def test_repository_isolation(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    # Pre-seed an existing entity in Repo B
    driver.nodes["Entity:repo_b_auth"] = {
        "_label": "Entity",
        "id": "repo_b_auth",
        "name": "AuthService",
        "label": "Service",
        "repository": "repo_b",
    }

    # Upload ADR to Repo A mentioning AuthService
    extractor.result = ADRExtractionResult(
        entities=[ADREntityOutput(name="AuthService", label="Service")],
        decisions=[
            ADRDecisionOutput(
                title="Auth Decision in Repo A",
                description="Scoped to Repo A only.",
                affects=["AuthService"],
            )
        ],
    )

    record_a, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="auth.md",
        file_bytes=b"# ADR: Auth\nDecision: Implement auth in Repo A.",
    )

    # Repo A's AuthService MUST NOT resolve to Repo B's entity
    repo_a_entities = [
        n for k, n in driver.nodes.items()
        if n.get("_label") == "Entity" and n.get("repository") == "repo_a" and n.get("name") == "AuthService"
    ]
    assert len(repo_a_entities) == 1
    assert repo_a_entities[0]["id"] != "repo_b_auth"

    # All decisions and chunks for this ADR are strictly in repo_a
    for r in driver.relationships:
        if r.get("adr_id") == record_a.id:
            src_node = driver.nodes.get(r["source"])
            if src_node:
                assert src_node.get("repository") == "repo_a"


# =========================================================================
# 3. Entity Resolution (Deduplication against Code Graph)
# =========================================================================
@pytest.mark.asyncio
async def test_entity_resolution_reuses_existing_code_entity(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    # Pre-seed an existing code graph entity in Repo A
    code_entity_id = "canonical_code_payment_service"
    driver.nodes[f"Entity:{code_entity_id}"] = {
        "_label": "Entity",
        "id": code_entity_id,
        "name": "PaymentService",
        "label": "Service",
        "repository": "repo_a",
        "aliases": ["PaymentGateway"],
        "embedding": [0.1] * 3072,
    }

    # ADR mentions "payment service" (case variation)
    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="Stripe Integration",
                description="Connect PaymentService to Stripe.",
                affects=["payment service"],
            )
        ],
        entities=[ADREntityOutput(name="payment service", label="Service")],
    )

    record, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="payment.md",
        file_bytes=b"# ADR: Payment\nDecision: Connect payment service to Stripe.",
    )

    # Must resolve to existing entity: NO new Entity created for PaymentService!
    payment_entities = [
        n for k, n in driver.nodes.items()
        if n.get("_label") == "Entity" and n.get("name") in {"PaymentService", "payment service"}
    ]
    assert len(payment_entities) == 1
    assert payment_entities[0]["id"] == code_entity_id

    # Decision connects directly to the canonical code entity
    affects_rels = [
        r for r in driver.relationships
        if r["type"] == "AFFECTS" and r["target"] == f"Entity:{code_entity_id}"
    ]
    assert len(affects_rels) >= 1


# =========================================================================
# 4. Idempotency (Running Twice Produces Same Counts)
# =========================================================================
@pytest.mark.asyncio
async def test_idempotent_ingestion(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="Caching with Redis",
                description="Use Redis cluster.",
                affects=["Redis"],
            )
        ],
        entities=[ADREntityOutput(name="Redis", label="Database")],
    )

    adr_content = b"# ADR 005: Redis Cache\n\n## Decision\nUse Redis for low-latency cache."

    # First run
    record1, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="redis.md",
        file_bytes=adr_content,
    )
    node_count_1 = len(driver.nodes)
    rel_count_1 = len(driver.relationships)

    # Ingest again by re-running processor directly on document
    processor = adr_env["processor"]
    doc = ADRDocument(
        adr_id=record1.id,
        repository_id=adr_env["repo_a"],
        title="ADR 005: Redis Cache",
        content=adr_content.decode("utf-8"),
        source_name="redis.md",
        content_hash=hashlib.sha256(adr_content).hexdigest(),
        file_path="/tmp/redis.md",
    )
    await processor.process_adr(doc)

    node_count_2 = len(driver.nodes)
    rel_count_2 = len(driver.relationships)

    assert node_count_1 == node_count_2
    assert rel_count_1 == rel_count_2


# =========================================================================
# 5. Changed ADR Content (Stale Knowledge Cleaned Up)
# =========================================================================
@pytest.mark.asyncio
async def test_changed_adr_content_replaces_stale_knowledge(adr_env):
    processor = adr_env["processor"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    adr_id = "adr_change_test"
    repo_id = adr_env["repo_a"]

    # Version 1: Mentions MySQL
    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="Use MySQL",
                description="Select MySQL for storage.",
                affects=["MySQL"],
            )
        ],
        entities=[ADREntityOutput(name="MySQL", label="Database")],
    )
    doc_v1 = ADRDocument(
        adr_id=adr_id,
        repository_id=repo_id,
        title="Database Choice",
        content="We choose MySQL for database storage.",
        source_name="db.md",
        content_hash="hash_v1",
        file_path="/tmp/db_v1.md",
    )
    await processor.process_adr(doc_v1)

    dec_titles_v1 = [
        n["title"] for n in driver.nodes.values()
        if n.get("_label") == "ArchitecturalDecision" and n.get("adr_id") == adr_id
    ]
    assert "Use MySQL" in dec_titles_v1

    # Version 2: Decision changed to PostgreSQL
    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="Use PostgreSQL",
                description="Switch to PostgreSQL for better JSON support.",
                affects=["PostgreSQL"],
            )
        ],
        entities=[ADREntityOutput(name="PostgreSQL", label="Database")],
    )
    doc_v2 = ADRDocument(
        adr_id=adr_id,
        repository_id=repo_id,
        title="Database Choice Revised",
        content="We switch to PostgreSQL for database storage.",
        source_name="db.md",
        content_hash="hash_v2",
        file_path="/tmp/db_v2.md",
    )
    await processor.process_adr(doc_v2)

    dec_titles_v2 = [
        n["title"] for n in driver.nodes.values()
        if n.get("_label") == "ArchitecturalDecision" and n.get("adr_id") == adr_id
    ]
    # Stale decision "Use MySQL" was deleted, replaced by "Use PostgreSQL"
    assert "Use MySQL" not in dec_titles_v2
    assert "Use PostgreSQL" in dec_titles_v2


# =========================================================================
# 6. Failure Handling (Embedding / LLM / Neo4j failure -> status FAILED)
# =========================================================================
@pytest.mark.asyncio
async def test_failure_handling_embedding_error(adr_env):
    service = adr_env["service"]
    store = adr_env["store"]
    embedder = adr_env["embedder"]

    embedder.should_fail = True

    with pytest.raises(Exception):
        await service.process_adr_upload(
            repository_id=adr_env["repo_a"],
            user_id=adr_env["user_id"],
            filename="fail-embed.md",
            file_bytes=b"# Test Embedding Failure\nContent.",
        )

    adrs = store.list_adrs(adr_env["repo_a"])
    failed = next((a for a in adrs if a.source_name == "fail-embed.md"), None)
    assert failed is not None
    assert failed.status == "FAILED"
    assert failed.status != "COMPLETED"


@pytest.mark.asyncio
async def test_failure_handling_llm_error(adr_env):
    service = adr_env["service"]
    store = adr_env["store"]
    extractor = adr_env["extractor"]

    extractor.should_fail = True

    with pytest.raises(Exception):
        await service.process_adr_upload(
            repository_id=adr_env["repo_a"],
            user_id=adr_env["user_id"],
            filename="fail-llm.md",
            file_bytes=b"# Test LLM Failure\nContent.",
        )

    adrs = store.list_adrs(adr_env["repo_a"])
    failed = next((a for a in adrs if a.source_name == "fail-llm.md"), None)
    assert failed is not None
    assert failed.status == "FAILED"
    assert failed.status != "COMPLETED"


@pytest.mark.asyncio
async def test_failure_handling_neo4j_error(adr_env):
    service = adr_env["service"]
    store = adr_env["store"]
    driver = adr_env["driver"]

    with patch.object(driver, "execute_query", side_effect=RuntimeError("Neo4j database connection lost")):
        with pytest.raises(Exception):
            await service.process_adr_upload(
                repository_id=adr_env["repo_a"],
                user_id=adr_env["user_id"],
                filename="fail-neo.md",
                file_bytes=b"# Test Neo4j Failure\nContent.",
            )

    adrs = store.list_adrs(adr_env["repo_a"])
    failed = next((a for a in adrs if a.source_name == "fail-neo.md"), None)
    assert failed is not None
    assert failed.status == "FAILED"
    assert failed.status != "COMPLETED"


# =========================================================================
# 7. Provenance Tracking
# =========================================================================
@pytest.mark.asyncio
async def test_provenance_tracking(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="Service Mesh Adoption",
                description="Adopt Istio for mTLS.",
                affects=["Istio"],
            )
        ],
        entities=[ADREntityOutput(name="Istio", label="Component")],
        relationships=[
            ADRRelationshipOutput(
                source_name="PaymentService",
                source_label="Service",
                relationship_type="USES",
                target_name="Istio",
                target_label="Component",
                rationale="mTLS sidecar proxy.",
            )
        ],
    )

    record, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="mesh.md",
        file_bytes=b"# ADR 010: Service Mesh\nAdopt Istio.",
    )

    # Chunks retain adr_id and content_hash
    chunks = [n for n in driver.nodes.values() if n.get("_label") == "Chunk" and n.get("adr_id") == record.id]
    assert len(chunks) >= 1
    assert chunks[0]["adr_id"] == record.id
    assert chunks[0]["content_hash"] is not None

    # GraphAssertion retains provenance to ADR and Chunk
    assertions = [
        n for n in driver.nodes.values()
        if n.get("_label") == "GraphAssertion" and n.get("source_id") == record.id
    ]
    assert len(assertions) == 1
    assert assertions[0]["source_id"] == record.id

    # Chunk SUPPORTS GraphAssertion
    supports_rels = [
        r for r in driver.relationships
        if r["type"] == "SUPPORTS" and r["source"].startswith(f"Chunk:adr:{record.id}")
    ]
    assert len(supports_rels) >= 1


# =========================================================================
# 8. Empty Extraction (Valid ADR with No Architectural Decisions)
# =========================================================================
@pytest.mark.asyncio
async def test_empty_extraction_ingests_successfully(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    # Empty extraction
    extractor.result = ADRExtractionResult()

    record, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="narrative.md",
        file_bytes=b"# Architectural Philosophy\nHigh level general notes.",
    )

    assert record.status == "COMPLETED"

    # ADR and Chunks exist even if no decisions or entities were extracted
    adr_node = driver.nodes.get(f"ADR:{record.id}")
    assert adr_node is not None
    chunks = [n for n in driver.nodes.values() if n.get("_label") == "Chunk" and n.get("adr_id") == record.id]
    assert len(chunks) >= 1


# =========================================================================
# 9. Two-ADR Provenance Isolation & Scoped Assertions (Req #10, #2, #14)
# =========================================================================
@pytest.mark.asyncio
async def test_two_adr_provenance_isolation(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    # ADR 1: Defines OrderService -> PostgreSQL relationship
    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="PostgreSQL Storage",
                description="Persist orders in Postgres.",
                affects=["OrderService", "PostgreSQL"],
            )
        ],
        entities=[
            ADREntityOutput(name="OrderService", label="Service"),
            ADREntityOutput(name="PostgreSQL", label="Database"),
        ],
        relationships=[
            ADRRelationshipOutput(
                source_name="OrderService",
                source_label="Service",
                relationship_type="USES",
                target_name="PostgreSQL",
                target_label="Database",
                rationale="Transactional storage",
            )
        ],
    )
    rec1, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="adr-001.md",
        file_bytes=b"# ADR 001: DB Choice\nUse Postgres.",
    )

    # ADR 2: Defines NotificationService -> RabbitMQ relationship
    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="RabbitMQ Messaging",
                description="Use RabbitMQ for notifications.",
                affects=["NotificationService", "RabbitMQ"],
            )
        ],
        entities=[
            ADREntityOutput(name="NotificationService", label="Service"),
            ADREntityOutput(name="RabbitMQ", label="MessageBroker"),
        ],
        relationships=[
            ADRRelationshipOutput(
                source_name="NotificationService",
                source_label="Service",
                relationship_type="USES",
                target_name="RabbitMQ",
                target_label="MessageBroker",
                rationale="Asynchronous events",
            )
        ],
    )
    rec2, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="adr-002.md",
        file_bytes=b"# ADR 002: Message Queue\nUse RabbitMQ.",
    )

    assert rec1.id != rec2.id

    # Verify both ADRs coexist in graph
    assert f"ADR:{rec1.id}" in driver.nodes
    assert f"ADR:{rec2.id}" in driver.nodes

    # Verify ADR-scoped GraphAssertion IDs
    assertions = [n for n in driver.nodes.values() if n.get("_label") == "GraphAssertion"]
    assert len(assertions) == 2

    asrt1 = next(a for a in assertions if a["source_id"] == rec1.id)
    asrt2 = next(a for a in assertions if a["source_id"] == rec2.id)

    assert asrt1["id"].startswith(f"adr:{rec1.id}:")
    assert asrt2["id"].startswith(f"adr:{rec2.id}:")
    assert asrt1["id"] != asrt2["id"]

    # Verify Chunk SUPPORTS relationships point strictly to their respective ADR's chunks
    supports1 = [
        r["source"] for r in driver.relationships
        if r["type"] == "SUPPORTS" and r["target"] == f"GraphAssertion:{asrt1['id']}"
    ]
    supports2 = [
        r["source"] for r in driver.relationships
        if r["type"] == "SUPPORTS" and r["target"] == f"GraphAssertion:{asrt2['id']}"
    ]
    assert len(supports1) >= 1
    assert all(src.startswith(f"Chunk:adr:{rec1.id}") for src in supports1)
    assert len(supports2) >= 1
    assert all(src.startswith(f"Chunk:adr:{rec2.id}") for src in supports2)

    # Re-upload ADR 1 with modified decision; verify ADR 2 is completely undisturbed
    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="PostgreSQL Storage Updated",
                description="Persist orders in Postgres v16.",
                affects=["OrderService", "PostgreSQL"],
            )
        ],
        entities=[
            ADREntityOutput(name="OrderService", label="Service"),
            ADREntityOutput(name="PostgreSQL", label="Database"),
        ],
    )
    rec1_updated, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="adr-001.md",
        file_bytes=b"# ADR 001: DB Choice Updated\nUse Postgres 16.",
    )
    assert rec1_updated.status == "COMPLETED"

    # ADR 2 nodes and assertions remain intact
    assert f"ADR:{rec2.id}" in driver.nodes
    assert f"GraphAssertion:{asrt2['id']}" in driver.nodes
    adr2_chunks = [n for n in driver.nodes.values() if n.get("_label") == "Chunk" and n.get("adr_id") == rec2.id]
    assert len(adr2_chunks) >= 1


# =========================================================================
# 10. Partial Neo4j Failure & Transaction Rollback (Req #1, #11)
# =========================================================================
@pytest.mark.asyncio
async def test_partial_neo4j_failure_transaction_rollback(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]
    store = adr_env["store"]

    extractor.result = ADRExtractionResult(
        decisions=[
            ADRDecisionOutput(
                title="Faulty Transaction Decision",
                description="Will fail mid-transaction.",
                affects=["AuthService"],
            )
        ],
        entities=[ADREntityOutput(name="AuthService", label="Service")],
    )

    # Pre-seed a clean existing node
    driver.nodes["Entity:preexisting"] = {
        "_label": "Entity",
        "id": "preexisting",
        "name": "Preexisting",
        "repository": adr_env["repo_a"],
    }

    initial_node_count = len(driver.nodes)
    initial_rel_count = len(driver.relationships)

    # Simulate a mid-transaction failure when upserting ArchitecturalDecision
    driver.fail_queries_matching = ["ArchitecturalDecision"]

    with pytest.raises(Exception):
        await service.process_adr_upload(
            repository_id=adr_env["repo_a"],
            user_id=adr_env["user_id"],
            filename="tx-fail.md",
            file_bytes=b"# ADR 999: Failure\nFail inside transaction.",
        )

    # Rollback verification: Driver state must be restored to pre-transaction snapshot
    assert len(driver.nodes) == initial_node_count
    assert len(driver.relationships) == initial_rel_count
    assert "Entity:preexisting" in driver.nodes
    assert not any(n.get("title") == "ADR 999: Failure" for n in driver.nodes.values())

    # SQLite status must be marked FAILED
    adrs = store.list_adrs(adr_env["repo_a"])
    failed = next((a for a in adrs if a.source_name == "tx-fail.md"), None)
    assert failed is not None
    assert failed.status == "FAILED"


# =========================================================================
# 11. ArchitecturalConstraint as First-Class Graph Node (Req #13)
# =========================================================================
@pytest.mark.asyncio
async def test_architectural_constraint_graph_nodes(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    extractor.result = ADRExtractionResult(
        constraints=[
            ADRConstraintOutput(
                description="Frontend must never access database schemas directly.",
                constraint_type="data_isolation_boundary",
                target_entities=["FrontendApp", "PaymentDatabase"],
            )
        ],
        entities=[
            ADREntityOutput(name="FrontendApp", label="Component"),
            ADREntityOutput(name="PaymentDatabase", label="Database"),
        ],
    )

    record, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="boundary-constraints.md",
        file_bytes=b"# ADR 020: Isolation Boundaries\nNo direct DB access from frontend.",
    )

    assert record.status == "COMPLETED"

    # Find the ArchitecturalConstraint node
    constraints = [
        n for n in driver.nodes.values()
        if n.get("_label") == "ArchitecturalConstraint" and n.get("adr_id") == record.id
    ]
    assert len(constraints) == 1
    c_node = constraints[0]
    assert c_node["description"] == "Frontend must never access database schemas directly."
    assert c_node["constraint_type"] == "data_isolation_boundary"
    assert c_node["repository"] == adr_env["repo_a"]

    # Verify ADR -> DEFINES -> ArchitecturalConstraint
    defines_rel = [
        r for r in driver.relationships
        if r["type"] == "DEFINES"
        and r["source"] == f"ADR:{record.id}"
        and r["target"] == f"ArchitecturalConstraint:{c_node['id']}"
    ]
    assert len(defines_rel) == 1

    # Verify ArchitecturalConstraint -> CONSTRAINS -> Entities
    constraint_edges = [
        r for r in driver.relationships
        if r["type"] == "CONSTRAINS" and r["source"] == f"ArchitecturalConstraint:{c_node['id']}"
    ]
    assert len(constraint_edges) == 2

    # Evidence from the source ADR chunk must directly support the constraint node.
    constraint_support = [
        r for r in driver.relationships
        if r["type"] == "SUPPORTS"
        and r["target"] == f"ArchitecturalConstraint:{c_node['id']}"
    ]
    assert len(constraint_support) >= 1

    # Verify direct ADR -> CONSTRAINS -> Entities
    direct_adr_constraints = [
        r for r in driver.relationships
        if r["type"] == "CONSTRAINS" and r["source"] == f"ADR:{record.id}"
    ]
    assert len(direct_adr_constraints) == 2


# =========================================================================
# Repository-scoped canonicalization and same-relationship provenance
# =========================================================================
@pytest.mark.asyncio
async def test_entity_alias_registry_is_repository_scoped(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    # Repo A has an existing canonical entity. Processing Repo A first would
    # populate a buggy application-global alias registry in the old implementation.
    driver.nodes["Entity:repo_a_auth"] = {
        "_label": "Entity",
        "id": "repo_a_auth",
        "name": "AuthService",
        "label": "Service",
        "repository": "repo_a",
        "aliases": ["AuthenticationService"],
        "embedding": [0.05] * 3072,
    }
    extractor.result = ADRExtractionResult(
        entities=[ADREntityOutput(name="AuthService", label="Service")]
    )
    rec_a, _ = await service.process_adr_upload(
        repository_id="repo_a",
        user_id=adr_env["user_id"],
        filename="repo-a-auth.md",
        file_bytes=b"# Repo A Auth\nUse AuthService.",
    )
    assert rec_a.status == "COMPLETED"

    # The same name in Repo B must never resolve to Repo A's canonical entity.
    extractor.result = ADRExtractionResult(
        entities=[ADREntityOutput(name="AuthService", label="Service")]
    )
    rec_b, _ = await service.process_adr_upload(
        repository_id="repo_b",
        user_id=adr_env["user_id"],
        filename="repo-b-auth.md",
        file_bytes=b"# Repo B Auth\nUse AuthService in Repo B.",
    )
    assert rec_b.status == "COMPLETED"

    repo_b_entities = [
        n for n in driver.nodes.values()
        if n.get("_label") == "Entity"
        and n.get("repository") == "repo_b"
        and n.get("name") == "AuthService"
    ]
    assert len(repo_b_entities) == 1
    assert repo_b_entities[0]["id"] != "repo_a_auth"

    # No Repo B ADR graph edge may point at the Repo A entity.
    assert not any(
        r.get("adr_id") == rec_b.id and r.get("target") == "Entity:repo_a_auth"
        for r in driver.relationships
    )


@pytest.mark.asyncio
async def test_same_relationship_from_two_adrs_keeps_independent_provenance(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    result = ADRExtractionResult(
        entities=[
            ADREntityOutput(name="PaymentService", label="Service"),
            ADREntityOutput(name="Stripe", label="ExternalSystem"),
        ],
        relationships=[
            ADRRelationshipOutput(
                source_name="PaymentService",
                source_label="Service",
                relationship_type="USES",
                target_name="Stripe",
                target_label="ExternalSystem",
                rationale="Payment processing integration.",
            )
        ],
    )

    extractor.result = result
    rec1, _ = await service.process_adr_upload(
        repository_id="repo_a",
        user_id=adr_env["user_id"],
        filename="payment-1.md",
        file_bytes=b"# ADR 1\nPaymentService uses Stripe for payments.",
    )

    extractor.result = result
    rec2, _ = await service.process_adr_upload(
        repository_id="repo_a",
        user_id=adr_env["user_id"],
        filename="payment-2.md",
        file_bytes=b"# ADR 2\nPaymentService uses Stripe for card payments.",
    )

    assertions = [
        n for n in driver.nodes.values()
        if n.get("_label") == "GraphAssertion"
    ]
    assert len(assertions) == 2
    assert {a["source_id"] for a in assertions} == {rec1.id, rec2.id}
    assert len({a["id"] for a in assertions}) == 2

    # ADR relationships must be represented through GraphAssertion, not direct
    # Entity->Entity edges that cannot safely carry multi-ADR provenance.
    assert not any(
        r.get("type") == "USES"
        and r.get("source", "").startswith("Entity:")
        and r.get("target", "").startswith("Entity:")
        for r in driver.relationships
    )


@pytest.mark.asyncio
async def test_batch_adrs_keep_separate_graph_assertions_and_chunk_provenance(adr_env):
    from starlette.datastructures import UploadFile
    adr_env['extractor'].result = ADRExtractionResult(
        entities=[ADREntityOutput(name='PaymentService', label='Service'),
                  ADREntityOutput(name='Stripe', label='ExternalSystem')],
        relationships=[ADRRelationshipOutput(
            source_name='PaymentService', source_label='Service', relationship_type='USES',
            target_name='Stripe', target_label='ExternalSystem', rationale='Payment integration.',
        )],
    )
    results = await adr_env['service'].process_adr_uploads(
        adr_env['repo_a'], adr_env['user_id'], [
            UploadFile(io.BytesIO(b'# ADR One\nPaymentService uses Stripe.'), filename='one.md'),
            UploadFile(io.BytesIO(b'# ADR Two\nPaymentService uses Stripe for cards.'), filename='two.md'),
        ],
    )
    assert [item.status for item in results] == ['completed', 'completed']
    adr_ids = {item.adr.id for item in results}
    nodes = list(adr_env['driver'].nodes.values())
    assertions = [node for node in nodes if node.get('_label') == 'GraphAssertion']
    assert len(assertions) == 2
    assert {node['source_id'] for node in assertions} == adr_ids
    assert len({node['id'] for node in assertions}) == 2
    assert all(node.get('repository') == adr_env['repo_a'] for node in assertions)
    chunks = [node for node in nodes if node.get('_label') == 'Chunk']
    assert {node['adr_id'] for node in chunks} == adr_ids
    for assertion in assertions:
        supports = [rel for rel in adr_env['driver'].relationships
                    if rel.get('type') == 'SUPPORTS'
                    and rel.get('target') == f"GraphAssertion:{assertion['id']}"]
        assert supports
        assert all(rel['source'].startswith(f"Chunk:adr:{assertion['source_id']}") for rel in supports)
    assert not any(
        rel.get('type') == 'USES' and rel.get('source', '').startswith('Entity:')
        and rel.get('target', '').startswith('Entity:')
        for rel in adr_env['driver'].relationships
    )


# =========================================================================
# 12. Label-Incompatible Entity Collision Prevention (Req #9, #8)
# =========================================================================
@pytest.mark.asyncio
async def test_label_incompatible_entities_are_not_merged(adr_env):
    service = adr_env["service"]
    driver = adr_env["driver"]
    extractor = adr_env["extractor"]

    # Pre-seed existing code entity named "Vault" with label "Database"
    driver.nodes["Entity:existing_vault_db"] = {
        "_label": "Entity",
        "id": "existing_vault_db",
        "name": "Vault",
        "label": "Database",
        "repository": adr_env["repo_a"],
        "aliases": ["vault"],
        "embedding": [0.05] * 3072,
    }

    # Pre-seed existing code entity named "Billing" with label "Service"
    driver.nodes["Entity:existing_billing_svc"] = {
        "_label": "Entity",
        "id": "existing_billing_svc",
        "name": "Billing",
        "label": "Service",
        "repository": adr_env["repo_a"],
        "aliases": ["billing"],
        "embedding": [0.05] * 3072,
    }

    # ADR defines "Vault" as "Service" (incompatible with "Database")
    # ADR also defines "Billing" as "Component" (generic label, compatible with "Service")
    extractor.result = ADRExtractionResult(
        entities=[
            ADREntityOutput(name="Vault", label="Service"),
            ADREntityOutput(name="Billing", label="Component"),
        ],
        decisions=[
            ADRDecisionOutput(
                title="Vault and Billing Integration",
                description="Connect Vault service with Billing.",
                affects=["Vault", "Billing"],
            )
        ],
    )

    record, _ = await service.process_adr_upload(
        repository_id=adr_env["repo_a"],
        user_id=adr_env["user_id"],
        filename="vault-service.md",
        file_bytes=b"# ADR 030: Vault Service\nVault as a credential service.",
    )

    assert record.status == "COMPLETED"

    # Find decision node
    dec_nodes = [
        n for n in driver.nodes.values()
        if n.get("_label") == "ArchitecturalDecision" and n.get("adr_id") == record.id
    ]
    assert len(dec_nodes) == 1
    dec_node = dec_nodes[0]

    # Check AFFECTS targets from the decision
    affects_targets = [
        r["target"] for r in driver.relationships
        if r["type"] == "AFFECTS" and r["source"] == f"ArchitecturalDecision:{dec_node['id']}"
    ]

    # "Billing" with generic "Component" should be merged into "Entity:existing_billing_svc"
    assert "Entity:existing_billing_svc" in affects_targets

    # "Vault" with "Service" must NOT be merged into "Entity:existing_vault_db"
    assert "Entity:existing_vault_db" not in affects_targets

    # A separate ADR-originated Entity node with label "Service" should exist for Vault
    adr_vault_entities = [
        n for n in driver.nodes.values()
        if n.get("_label") == "Entity"
        and n.get("name") == "Vault"
        and n.get("label") == "Service"
        and n.get("source") == "ADR"
    ]
    assert len(adr_vault_entities) == 1
    assert adr_vault_entities[0]["id"] != "existing_vault_db"


# =========================================================================
# 13. Chunker Strictly Enforces max_chunk_chars (Req #7)
# =========================================================================
def test_chunker_strictly_enforces_max_chunk_chars():
    long_sentence = "This is a critical architectural requirement that spans several paragraphs without breaks. "
    massive_body = long_sentence * 50  # ~4550 chars
    assert len(massive_body) > 4000

    doc = ADRDocument(
        adr_id="adr_long_001",
        repository_id="repo_a",
        title="ADR Long Paragraph",
        content=f"# ADR Long\n\n{massive_body}",
        file_path="/tmp/test_adr_long.md",
        source_name="001-long.md",
        sections=[
            ADRSection(
                heading="Context",
                content=massive_body,
                level=2,
            )
        ],
        content_hash="hash_long_001",
    )

    chunker = ADRChunker(max_chunk_chars=600)
    chunks = chunker.chunk(doc)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 600, f"Chunk text length {len(chunk.text)} exceeds 600 limit"
        assert chunk.content_hash is not None
        assert chunk.chunk_id.startswith("adr:adr_long_001:chunk:")

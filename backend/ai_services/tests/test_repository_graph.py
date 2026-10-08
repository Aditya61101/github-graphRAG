from __future__ import annotations

from typing import Any
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_services.graph.repository import GraphRepositoryError, Neo4jGraphRepository
from ai_services.graph.service import (
    RepositoryAccessDeniedError,
    RepositoryGraphService,
    RepositoryNotFoundError,
)
from ai_services.ingestion.persistence.models import (
    Base,
    GitHubConnectionModel,
    RepositoryModel,
    UserModel,
)
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from api_services.app.models.graph import (
    GraphEdge,
    GraphNode,
    RepositoryGraphResponse,
)
from api_services.app.routers.repositories import router as repositories_router
from api_services.app.utils.jwt_utils import create_access_token


class MockNeo4jDriver:
    """In-memory mock of Neo4j driver providing node and edge records."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Key: id -> dict of entity properties
        self.entities: dict[str, dict[str, Any]] = {}
        # List of direct relationships: {"source_id", "target_id", "rel_type", "repo", ...}
        self.direct_rels: list[dict[str, Any]] = []
        # List of assertions: {"source_id", "target_id", "rel_type", "repo", ...}
        self.assertions: list[dict[str, Any]] = []
        self.should_fail = False

    def execute_query(self, query: str, database_: str = "neo4j", **params: Any) -> tuple[list[Any], Any, Any]:
        self.calls.append((query, params))
        if self.should_fail:
            raise RuntimeError("Simulated Neo4j database connection failure.")

        repo_ids = params.get("repo_ids", [])

        # 1. Fetching entities
        if "MATCH (e:Entity)" in query:
            records = []
            for eid, e in self.entities.items():
                if e.get("repository") in repo_ids:
                    records.append(
                        {
                            "id": e.get("id"),
                            "name": e.get("name"),
                            "label": e.get("label"),
                            "aliases": e.get("aliases", []),
                            "description": e.get("description"),
                            "source": e.get("source"),
                            "repository": e.get("repository"),
                        }
                    )
            return records, None, None

        # 2. Fetching direct relationships
        if "MATCH (s:Entity)-[r]->(t:Entity)" in query:
            records = []
            for r in self.direct_rels:
                src_ent = self.entities.get(r["source_id"])
                tgt_ent = self.entities.get(r["target_id"])
                if (
                    src_ent
                    and tgt_ent
                    and src_ent.get("repository") in repo_ids
                    and tgt_ent.get("repository") in repo_ids
                ):
                    records.append(
                        {
                            "source_id": r["source_id"],
                            "target_id": r["target_id"],
                            "rel_type": r["rel_type"],
                            "confidence": r.get("confidence", 1.0),
                            "rationale": r.get("rationale", "Direct code relationship"),
                            "source_type": r.get("source_type", "code"),
                        }
                    )
            return records, None, None

        # 3. Fetching assertion relationships
        if "MATCH (s:Entity)-[:ASSERTS]->(a:GraphAssertion)-[:TARGETS]->(t:Entity)" in query:
            records = []
            for a in self.assertions:
                src_ent = self.entities.get(a["source_id"])
                tgt_ent = self.entities.get(a["target_id"])
                if (
                    src_ent
                    and tgt_ent
                    and src_ent.get("repository") in repo_ids
                    and tgt_ent.get("repository") in repo_ids
                    and a.get("repository") in repo_ids
                ):
                    records.append(
                        {
                            "source_id": a["source_id"],
                            "target_id": a["target_id"],
                            "rel_type": a["rel_type"],
                            "confidence": a.get("confidence", 1.0),
                            "rationale": a.get("rationale", "ADR architectural assertion"),
                            "source_type": a.get("source_type", "ADR"),
                        }
                    )
            return records, None, None

        return [], None, None


@pytest.fixture
def graph_env():
    """Create isolated test environment with SQLite and in-memory Neo4j mock."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = SqliteApplicationStore(session_factory=session_factory)

    # Seed Users
    with session_factory() as session:
        alice = UserModel(id="usr_alice", username="alice", email="alice@test.com")
        bob = UserModel(id="usr_bob", username="bob", email="bob@test.com")
        session.add_all([alice, bob])

        conn_alice = GitHubConnectionModel(
            id="conn_alice",
            user_id="usr_alice",
            github_user_id="1010",
            access_token="token_alice",
        )
        conn_bob = GitHubConnectionModel(
            id="conn_bob",
            user_id="usr_bob",
            github_user_id="2020",
            access_token="token_bob",
        )
        session.add_all([conn_alice, conn_bob])

        # Repo A owned by Alice
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
        # Repo B owned by Bob
        repo_b = RepositoryModel(
            id="repo_b",
            github_repository_id="2002",
            owner="bob",
            name="repo-b",
            full_name="bob/repo-b",
            repository_url="https://github.com/bob/repo-b",
            default_branch="main",
            tracked_branch="main",
            user_id="usr_bob",
            github_connection_id="conn_bob",
            status="COMPLETED",
        )
        session.add_all([repo_a, repo_b])
        session.commit()

    mock_driver = MockNeo4jDriver()
    graph_repo = Neo4jGraphRepository(driver=mock_driver, database="neo4j")
    graph_service = RepositoryGraphService(sqlite_store=store, graph_repo=graph_repo)

    app = FastAPI()
    app.state.sqlite_store = store
    app.state.graph_service = graph_service
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    app.state.github_app = SimpleNamespace(authorize_tracked=AsyncMock(
        side_effect=lambda user_id, identifier: store.get_repository(identifier)))
    app.include_router(repositories_router, prefix="/repositories")

    client = TestClient(app)
    alice_token = create_access_token({"sub": "usr_alice"})
    bob_token = create_access_token({"sub": "usr_bob"})

    return {
        "store": store,
        "driver": mock_driver,
        "graph_repo": graph_repo,
        "graph_service": graph_service,
        "client": client,
        "alice_token": alice_token,
        "bob_token": bob_token,
        "repo_a": "repo_a",
        "repo_b": "repo_b",
    }


# =========================================================================
# 1. Successful Graph Fetch
# =========================================================================
def test_successful_graph_fetch(graph_env):
    driver = graph_env["driver"]
    client = graph_env["client"]
    token = graph_env["alice_token"]
    repo_a = graph_env["repo_a"]

    # Seed Repo A entities in Neo4j
    driver.entities["order_svc"] = {
        "id": "order_svc",
        "name": "OrderService",
        "label": "Service",
        "repository": repo_a,
        "aliases": ["orders"],
        "description": "Handles customer orders",
    }
    driver.entities["postgres_db"] = {
        "id": "postgres_db",
        "name": "PostgresDB",
        "label": "Database",
        "repository": repo_a,
        "aliases": ["postgres"],
        "description": "Primary relational store",
    }
    driver.entities["payment_gw"] = {
        "id": "payment_gw",
        "name": "PaymentGateway",
        "label": "Component",
        "repository": repo_a,
    }

    # Seed Relationships
    driver.direct_rels.append(
        {
            "source_id": "order_svc",
            "target_id": "postgres_db",
            "rel_type": "WRITES_TO",
            "confidence": 0.95,
            "rationale": "Direct SQL access",
            "source_type": "code",
        }
    )
    driver.direct_rels.append(
        {
            "source_id": "order_svc",
            "target_id": "payment_gw",
            "rel_type": "DEPENDS_ON",
            "confidence": 0.90,
            "rationale": "REST API client",
            "source_type": "code",
        }
    )

    response = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()

    # Validate against Pydantic schema
    graph_resp = RepositoryGraphResponse(**data)
    assert len(graph_resp.nodes) == 3
    assert len(graph_resp.edges) == 2

    # Verify Node format
    node_ids = {n.id for n in graph_resp.nodes}
    assert node_ids == {"entity:order_svc", "entity:postgres_db", "entity:payment_gw"}

    order_node = next(n for n in graph_resp.nodes if n.id == "entity:order_svc")
    assert order_node.label == "OrderService"
    assert order_node.type == "Service"
    assert order_node.metadata["repository_id"] == repo_a
    assert order_node.metadata["aliases"] == ["orders"]
    assert order_node.metadata["description"] == "Handles customer orders"

    # Verify Edge format and that source and target correspond to returned nodes
    edge_ids = {e.id for e in graph_resp.edges}
    assert "edge:order_svc:WRITES_TO:postgres_db" in edge_ids
    assert "edge:order_svc:DEPENDS_ON:payment_gw" in edge_ids

    for edge in graph_resp.edges:
        assert edge.source in node_ids
        assert edge.target in node_ids
        assert edge.type in {"WRITES_TO", "DEPENDS_ON"}
        assert "sources" in edge.metadata


# =========================================================================
# 2. Empty Graph
# =========================================================================
def test_empty_graph(graph_env):
    client = graph_env["client"]
    token = graph_env["alice_token"]
    repo_a = graph_env["repo_a"]

    response = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data == {"nodes": [], "edges": []}


# =========================================================================
# 3. Repository Isolation
# =========================================================================
def test_repository_isolation(graph_env):
    driver = graph_env["driver"]
    client = graph_env["client"]
    alice_token = graph_env["alice_token"]
    bob_token = graph_env["bob_token"]
    repo_a = graph_env["repo_a"]
    repo_b = graph_env["repo_b"]

    # Entities in Repo A
    driver.entities["repo_a_entity"] = {
        "id": "repo_a_entity",
        "name": "AlphaService",
        "label": "Service",
        "repository": repo_a,
    }
    # Entities in Repo B
    driver.entities["repo_b_entity"] = {
        "id": "repo_b_entity",
        "name": "BetaService",
        "label": "Service",
        "repository": repo_b,
    }

    # Edges in Repo A
    driver.direct_rels.append(
        {
            "source_id": "repo_a_entity",
            "target_id": "repo_a_entity_2",
            "rel_type": "CALLS",
        }
    )
    # Edge in Repo B
    driver.direct_rels.append(
        {
            "source_id": "repo_b_entity",
            "target_id": "repo_b_entity_2",
            "rel_type": "CALLS",
        }
    )

    # Alice fetches Repo A
    resp_a = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    nodes_a = [n["id"] for n in data_a["nodes"]]
    assert "entity:repo_a_entity" in nodes_a
    assert "entity:repo_b_entity" not in nodes_a

    # Bob fetches Repo B
    resp_b = client.get(
        f"/repositories/{repo_b}/graph",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    nodes_b = [n["id"] for n in data_b["nodes"]]
    assert "entity:repo_b_entity" in nodes_b
    assert "entity:repo_a_entity" not in nodes_b


# =========================================================================
# 4. Unauthorized Repository (403 for other user, 404 for nonexistent)
# =========================================================================
def test_unauthorized_repository(graph_env):
    client = graph_env["client"]
    alice_token = graph_env["alice_token"]
    repo_b = graph_env["repo_b"]  # Owned by Bob

    # Alice attempts to access Bob's repository
    resp_forbidden = client.get(
        f"/repositories/{repo_b}/graph",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp_forbidden.status_code == 403
    assert "permission" in resp_forbidden.json()["detail"].lower()

    # Nonexistent repository lookup
    resp_not_found = client.get(
        "/repositories/repo_nonexistent/graph",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp_not_found.status_code == 404
    assert "not found" in resp_not_found.json()["detail"].lower()


# =========================================================================
# 5. Stable IDs Across Repeated Invocations
# =========================================================================
def test_stable_ids(graph_env):
    driver = graph_env["driver"]
    client = graph_env["client"]
    token = graph_env["alice_token"]
    repo_a = graph_env["repo_a"]

    driver.entities["svc_user"] = {
        "id": "svc_user",
        "name": "UserService",
        "label": "Service",
        "repository": repo_a,
    }
    driver.entities["db_user"] = {
        "id": "db_user",
        "name": "UserDatabase",
        "label": "Database",
        "repository": repo_a,
    }
    driver.direct_rels.append(
        {
            "source_id": "svc_user",
            "target_id": "db_user",
            "rel_type": "READS_FROM",
        }
    )

    resp1 = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    resp2 = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert resp1 == resp2
    assert resp1["nodes"][0]["id"] == "entity:db_user"
    assert resp1["nodes"][1]["id"] == "entity:svc_user"
    assert resp1["edges"][0]["id"] == "edge:svc_user:READS_FROM:db_user"


# =========================================================================
# 6. No Neo4j Internal IDs Exposed
# =========================================================================
def test_no_neo4j_internal_ids_exposed(graph_env):
    driver = graph_env["driver"]
    client = graph_env["client"]
    token = graph_env["alice_token"]
    repo_a = graph_env["repo_a"]

    driver.entities["12345"] = {
        "id": "auth-module",
        "name": "AuthModule",
        "label": "Module",
        "repository": repo_a,
    }

    response = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    node = response["nodes"][0]
    assert node["id"] == "entity:auth-module"
    assert not node["id"].startswith("<id:")
    assert not node["id"].startswith("4:")
    assert "element_id" not in node
    assert "identity" not in node


# =========================================================================
# 7. Neo4j Database Failure Handling (Not Swallowed -> HTTP 500)
# =========================================================================
def test_neo4j_failure_produces_500(graph_env):
    driver = graph_env["driver"]
    client = graph_env["client"]
    token = graph_env["alice_token"]
    repo_a = graph_env["repo_a"]

    driver.should_fail = True

    response = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 500
    assert "error" in response.json()["detail"].lower()


# =========================================================================
# 8. Unification of Code Relationships and ADR GraphAssertions
# =========================================================================
def test_graph_assertion_and_code_edges_unified(graph_env):
    driver = graph_env["driver"]
    client = graph_env["client"]
    token = graph_env["alice_token"]
    repo_a = graph_env["repo_a"]

    driver.entities["svc_orders"] = {
        "id": "svc_orders",
        "name": "OrderService",
        "label": "Service",
        "repository": repo_a,
    }
    driver.entities["db_postgres"] = {
        "id": "db_postgres",
        "name": "PostgreSQL",
        "label": "Database",
        "repository": repo_a,
    }
    driver.entities["cache_redis"] = {
        "id": "cache_redis",
        "name": "RedisCache",
        "label": "Database",
        "repository": repo_a,
    }

    # 1. Direct code relationship: OrderService -> PostgreSQL (USES)
    driver.direct_rels.append(
        {
            "source_id": "svc_orders",
            "target_id": "db_postgres",
            "rel_type": "USES",
            "confidence": 0.8,
            "rationale": "Static code import",
            "source_type": "code",
        }
    )

    # 2. ADR GraphAssertion: OrderService -> PostgreSQL (USES) [Same edge type]
    driver.assertions.append(
        {
            "source_id": "svc_orders",
            "target_id": "db_postgres",
            "rel_type": "USES",
            "confidence": 1.0,
            "rationale": "ADR 001 mandated Postgres usage",
            "source_type": "ADR",
            "repository": repo_a,
        }
    )

    # 3. ADR-only GraphAssertion: OrderService -> RedisCache (DEPENDS_ON)
    driver.assertions.append(
        {
            "source_id": "svc_orders",
            "target_id": "cache_redis",
            "rel_type": "DEPENDS_ON",
            "confidence": 1.0,
            "rationale": "ADR 002 session cache requirement",
            "source_type": "ADR",
            "repository": repo_a,
        }
    )

    response = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    edges = data["edges"]

    # Deduplicated into exactly 2 canonical edges
    assert len(edges) == 2

    postgres_edge = next(e for e in edges if e["target"] == "entity:db_postgres")
    assert postgres_edge["id"] == "edge:svc_orders:USES:db_postgres"
    assert postgres_edge["source"] == "entity:svc_orders"
    assert postgres_edge["type"] == "USES"
    # Merged sources from both code and ADR
    assert set(postgres_edge["metadata"]["sources"]) == {"code", "ADR"}
    assert postgres_edge["metadata"]["confidence"] == 1.0

    redis_edge = next(e for e in edges if e["target"] == "entity:cache_redis")
    assert redis_edge["id"] == "edge:svc_orders:DEPENDS_ON:cache_redis"
    assert redis_edge["metadata"]["sources"] == ["ADR"]


# =========================================================================
# 9. Unauthenticated Request Rejected (401)
# =========================================================================
def test_unauthenticated_request_rejected(graph_env):
    client = graph_env["client"]
    repo_a = graph_env["repo_a"]

    # Request without Authorization header
    resp_no_token = client.get(f"/repositories/{repo_a}/graph")
    assert resp_no_token.status_code == 401

    # Request with invalid JWT
    resp_invalid_token = client.get(
        f"/repositories/{repo_a}/graph",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    assert resp_invalid_token.status_code == 401

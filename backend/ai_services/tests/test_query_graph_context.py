from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from ai_services.agents.rag_agent.dataclasses.result import RAGAgentResult
from ai_services.graph import GraphContext
from api_services.app.main import app
from api_services.app.utils.jwt_utils import create_access_token
from api_services.app.utils.query_response_mapper import build_api_response


def test_existing_query_sources_behavior_preserved():
    """11. Existing query behavior: answer and sources mapping remain unchanged."""
    rag_result = RAGAgentResult(
        answer="The auth flow uses JWT tokens.",
        sources={
            "chunks": [
                {
                    "file_path": "backend/auth.py",
                    "excerpt": "def login(): ...",
                    "score": 0.95,
                }
            ]
        },
        graph_context=GraphContext(
            node_ids=["entity:AuthService"],
            edge_ids=["edge:AuthService:USES:JWT"],
            assertion_ids=["assertion:adr:auth"],
        ),
    )

    response = build_api_response(rag_result, conversation_id="conv-456")
    assert response.answer == "The auth flow uses JWT tokens."
    assert len(response.sources) == 1
    assert response.sources[0].file_path == "backend/auth.py"
    assert response.sources[0].excerpt == "def login(): ..."
    assert response.sources[0].score == 0.95
    assert response.graph_context.node_ids == ["entity:AuthService"]
    assert response.graph_context.edge_ids == ["edge:AuthService:USES:JWT"]
    assert response.graph_context.assertion_ids == ["assertion:adr:auth"]


def test_fastapi_query_endpoints_return_graph_context():
    """12. API endpoints: POST /query and POST /repositories/{repo_id}/query return graph_context."""
    from ai_services.ingestion.persistence.models import UserModel
    client = TestClient(app)
    token = create_access_token({"sub": "user_test_gc"})

    mock_rag_agent = MagicMock()
    mock_rag_agent.query = AsyncMock(return_value=RAGAgentResult(
        answer="Grounded answer.",
        sources={
            "chunks": [
                {"file_path": "order.py", "excerpt": "class Order: pass", "score": 0.88}
            ]
        },
        graph_context=GraphContext(
            node_ids=["entity:OrderService"],
            edge_ids=["edge:OrderService:CALLS:PaymentService"],
            assertion_ids=["assertion:adr:1001"],
        ),
    ))

    # Mock sqlite_store
    mock_store = MagicMock()
    mock_user = UserModel(id="user_test_gc", username="testuser", email="test@example.com")
    mock_store.get_user.return_value = mock_user

    mock_repo = MagicMock()
    mock_repo.id = "repo_test_1"
    mock_repo.user_id = "user_test_gc"
    mock_repo.github_connection = None
    mock_store.get_repository.return_value = mock_repo

    app.state.rag_agent = mock_rag_agent
    app.state.sqlite_store = mock_store

    headers = {"Authorization": f"Bearer {token}"}

    # 1. POST /query
    resp = client.post(
        "/query",
        headers=headers,
        json={
            "query": "explain order service",
            "conversation_id": "thread-1",
            "repository_id": "repo_test_1",
        },
    )
    assert resp.status_code == 200
    mock_rag_agent.query.assert_awaited_with(
        conversation_id="thread-1", query="explain order service", repository_id="repo_test_1",
    )
    data = resp.json()
    assert data["answer"] == "Grounded answer."
    assert "graph_context" in data
    assert data["graph_context"] == {
        "node_ids": ["entity:OrderService"],
        "edge_ids": ["edge:OrderService:CALLS:PaymentService"],
        "assertion_ids": ["assertion:adr:1001"],
    }

    # 2. POST /repositories/{repo_id}/query
    resp_repo = client.post(
        "/repositories/repo_test_1/query",
        headers=headers,
        json={
            "query": "explain order service",
            "conversation_id": "thread-2",
        },
    )
    assert resp_repo.status_code == 200
    data_repo = resp_repo.json()
    assert data_repo["answer"] == "Grounded answer."
    assert data_repo["graph_context"] == {
        "node_ids": ["entity:OrderService"],
        "edge_ids": ["edge:OrderService:CALLS:PaymentService"],
        "assertion_ids": ["assertion:adr:1001"],
    }

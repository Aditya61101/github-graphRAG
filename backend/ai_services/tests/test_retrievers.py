import asyncio
from types import SimpleNamespace

from ai_services.runs.run_retrieval import answer_query
from ai_services.retrievers.graph_expansion import expand_entities
from ai_services.retrievers.hybrid_retrievers import hybrid_retrieve
from ai_services.retrievers.retriever_factory import entity_result_formatter


class Item:
    def __init__(self, content, metadata):
        self.content = content
        self.metadata = metadata


class SearchResult:
    def __init__(self, items):
        self.items = items


def test_entity_formatter_exposes_canonical_id_label_and_name():
    item = entity_result_formatter(
        {
            "node": {"id": "entity-123", "label": "Service", "name": "AuthService"},
            "score": 0.91,
        }
    )

    assert item.content == "AuthService"
    assert item.metadata == {
        "canonical_id": "entity-123",
        "label": "Service",
        "name": "AuthService",
        "score": 0.91,
    }


def test_one_hop_expansion_uses_canonical_ids_and_returns_directed_endpoints():
    class Driver:
        def __init__(self):
            self.calls = []

        def execute_query(self, query, **kwargs):
            self.calls.append((query, kwargs))
            return (
                [
                    {
                        "source_id": "service-id",
                        "source_label": "Service",
                        "source": "AuthService",
                        "relationship": "USES",
                        "target_id": "repo-id",
                        "target_label": "Repository",
                        "target": "UserRepository",
                    }
                ],
                None,
                None,
            )

    driver = Driver()
    records = expand_entities(driver, ["service-id", "service-id"], "neo4j")

    query, kwargs = driver.calls[0]
    assert "MATCH (source:Entity)-[r]->(target:Entity)" in query
    assert "neighbor" not in query
    assert kwargs["entity_ids"] == ["service-id"]
    assert records[0]["source"] == "AuthService"
    assert records[0]["target"] == "UserRepository"




def test_answer_wrapper_uses_existing_async_llm_adapter():
    class LLM:
        async def ainvoke(self, prompt):
            assert "GraphRAG Context:\nentity context" in prompt
            return "grounded answer"

    answer = asyncio.run(answer_query("question", "entity context", LLM()))

    assert answer == "grounded answer"

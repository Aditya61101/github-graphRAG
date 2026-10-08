from __future__ import annotations

from typing import Any
from hashlib import sha256
import json

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    SummarizationMiddleware,
)
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from neo4j import Driver

from ai_services.agents.rag_agent.dataclasses.result import RAGAgentResult
from ai_services.agents.rag_agent.state.agent_state import RAGAgentState
from ai_services.agents.rag_agent.tools.query_tool import create_query_graph_rag_tool
from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from ai_services.graph.context import GraphContext
from ai_services.retrievers.reranker import Reranker
from ai_services.retrievers.settings import RetrievalSettings

from .prompts import RAG_AGENT_SYSTEM_PROMPT


class RAGQueryAgent:
    """Stateful conversational agent sitting above the GraphRAG retriever."""

    def __init__(
        self,
        *,
        driver: Driver,
        database: str | None,
        embedder: AzureOpenAIEmbedder,
        entity_retriever: Any,
        community_retriever: Any,
        chunk_retriever: Any,
        llm: AzureChatOpenAI,
        reranker: Reranker,
        retrieval_settings: RetrievalSettings | None = None,
        max_tokens_before_summary: int = 4000,
        messages_to_keep: int = 10,
        tool_context_clear_trigger: int = 8000,
        tool_context_keep: int = 1,
    ) -> None:
        self.checkpointer = InMemorySaver()

        self.query_tool = create_query_graph_rag_tool(
            driver=driver,
            database=database,
            embedder=embedder,
            entity_retriever=entity_retriever,
            community_retriever=community_retriever,
            chunk_retriever=chunk_retriever,
            reranker=reranker,
            retrieval_settings=retrieval_settings,
        )

        self.agent = create_agent(
            model=llm,
            tools=[self.query_tool],
            system_prompt=RAG_AGENT_SYSTEM_PROMPT,
            state_schema=RAGAgentState,
            middleware=[
                ContextEditingMiddleware(
                    edits=[
                        ClearToolUsesEdit(
                            trigger=tool_context_clear_trigger,
                            keep=tool_context_keep,
                        )
                    ]
                ),
                SummarizationMiddleware(
                    model=llm,
                    max_tokens_before_summary=max_tokens_before_summary,
                    messages_to_keep=messages_to_keep,
                ),
            ],
            checkpointer=self.checkpointer,
        )

    async def query(
        self,
        conversation_id: str,
        query: str,
        repository_id: str | None = None,
        user_id: str = "local",
    ) -> RAGAgentResult:
        if not repository_id:
            raise ValueError('An authorized repository is required')
        thread_id = sha256(json.dumps([user_id, repository_id, conversation_id]).encode()).hexdigest()
        state_input: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": query,
                }
            ]
        }
        # Always overwrite the current scope, including None, so a previous
        # checkpoint cannot silently select a different repository.
        state_input["repository_id"] = repository_id

        result = await self.agent.ainvoke(
            state_input,
            {
                "configurable": {
                    "thread_id": thread_id,
                }
            },
        )

        return self._extract_result(result)

    @staticmethod
    def _extract_result(result: dict) -> RAGAgentResult:
        messages = result.get("messages", [])

        if not messages:
            raise RuntimeError("RAG query agent returned no messages.")

        final_message = messages[-1]
        if getattr(final_message, "type", None) != "ai":
            raise RuntimeError("RAG query agent did not return a final AI message.")

        sources = result.get("retrieval_sources") or {}
        raw_gc = result.get("retrieval_graph_context") or {}
        graph_context = GraphContext(
            node_ids=list(raw_gc.get("node_ids", [])),
            edge_ids=list(raw_gc.get("edge_ids", [])),
            assertion_ids=list(raw_gc.get("assertion_ids", [])),
        )

        return RAGAgentResult(
            answer=final_message.content,
            sources=sources,
            graph_context=graph_context,
        )

    def get_state(self, conversation_id: str, repository_id: str, user_id: str = "local"):
        """Return the current persisted state for debugging/inspection."""
        return self.agent.get_state(
            {
                "configurable": {
                    "thread_id": sha256(json.dumps([user_id, repository_id, conversation_id]).encode()).hexdigest(),
                }
            }
        )

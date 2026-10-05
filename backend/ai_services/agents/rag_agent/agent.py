from __future__ import annotations

from typing import Any

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
    ) -> RAGAgentResult:
        result = await self.agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": query,
                    }
                ]
            },
            {
                "configurable": {
                    "thread_id": conversation_id,
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

        return RAGAgentResult(
            answer=final_message.content,
            sources=sources,
        )

    def get_state(self, conversation_id: str):
        """Return the current persisted state for debugging/inspection."""
        return self.agent.get_state(
            {
                "configurable": {
                    "thread_id": conversation_id,
                }
            }
        )

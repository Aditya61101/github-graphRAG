from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from openai import AsyncAzureOpenAI

from ai_services.agents.rag_agent.tools.query_tool import create_query_graph_rag_tool
from ai_services.utils.env_helper import require_env

from .models import ContextualizedRetrievalQuery
from .prompts import RAG_AGENT_SYSTEM_PROMPT

llm = AsyncAzureOpenAI(
    azure_endpoint=require_env("AZURE_OPENAI_ENDPOINT"),
    api_key=require_env("AZURE_OPENAI_API_KEY"),
    api_version=require_env("AZURE_OPENAI_API_VERSION"),
    azure_deployment=require_env("AZURE_OPENAI_DEPLOYMENT_NAME"),
)

class RAGQueryAgent:
    """
    Stateful conversational layer for the RAG endpoint.

    Responsibilities at this stage:
      - preserve conversation state per conversation_id
      - summarize long conversations
      - contextualize follow-up questions
      - return a retrieval-ready standalone query

    It deliberately does NOT call GraphRAG yet. That remains a separate
    retrieval subsystem and can be wired after this boundary.
    """

    def __init__(
        self,
        max_tokens_before_summary: int = 4000,
        messages_to_keep: int = 10,
    ) -> None:
        self.checkpointer = InMemorySaver()
        self.query_tool = create_query_graph_rag_tool(
            driver=driver,
            database=require_env("NEO4J_DATABASE"),
            embedder=embedder,
            entity_retriever=entity_retriever,
            community_retriever=community_retriever,
            chunk_retriever=chunk_retriever,
        )
        self.agent = create_agent(
            model=llm,
            tools=[self.query_tool],
            system_prompt=RAG_AGENT_SYSTEM_PROMPT,
            # response_format=ContextualizedRetrievalQuery,
            middleware=[
                SummarizationMiddleware(
                    model=llm,
                    max_tokens_before_summary=max_tokens_before_summary,
                    messages_to_keep=messages_to_keep,
                )
            ],
            checkpointer=self.checkpointer,
        )
        

    def query(
        self,
        conversation_id: str,
        query: str,
    ) -> ContextualizedRetrievalQuery:
        result = self.agent.invoke(
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
    def _extract_result(result: dict) -> dict:
        messages = result.get("messages", [])

        if not messages:
            raise RuntimeError(
                "RAG query agent returned no messages."
            )

        final_message = messages[-1]

        return {
            "answer": final_message.content,
            "retrieval_result": RAGQueryAgent._extract_retrieval_result(
                messages
            ),
        }

    @staticmethod
    def _extract_retrieval_result(messages: list) -> Any:
        """
        Extract the RetrievalResult returned by query_graph_rag.

        The tool result is expected to be present in the agent message
        history after the tool call.
        """
        for message in reversed(messages):
            if getattr(message, "type", None) != "tool":
                continue

            if getattr(message, "name", None) == "query_graph_rag":
                return message.content

        raise RuntimeError(
            "RAG query agent did not call query_graph_rag."
        )
    
    def get_state(self, conversation_id: str):
        """Useful for debugging/inspection of a conversation thread."""
        return self.agent.get_state(
            {
                "configurable": {
                    "thread_id": conversation_id,
                }
            }
        )

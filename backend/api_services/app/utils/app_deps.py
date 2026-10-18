from typing import Any

from fastapi import Request
from neo4j import Driver

from shared.ai.provider import Embedder
from shared.ai.provider import LLM


def get_driver(request: Request) -> Driver:
    return request.app.state.deps.driver


def get_embedder(request: Request) -> Embedder:
    return request.app.state.deps.embedder


def get_llm(request: Request) -> LLM:
    return request.app.state.deps.llm

def get_entity_retriever(request: Request) -> Any:
    return request.app.state.deps.entity_retriever

def get_chunk_retriever(request: Request) -> Any:
    return request.app.state.deps.chunk_retriever

def get_community_retriever(request: Request) -> Any:
    return request.app.state.deps.community_retriever


from api_services.app.utils.jwt_utils import get_current_user  # Re-export for standard dependency injection
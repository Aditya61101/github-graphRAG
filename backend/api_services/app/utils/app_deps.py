from typing import Any

from fastapi import Request
from neo4j import Driver

from ai_services.embeddings.azure_openai import AzureOpenAIEmbedder
from shared.utils.llm_utils import AzureOpenAILLM


def get_driver(request: Request) -> Driver:
    return request.app.state.deps.driver


def get_embedder(request: Request) -> AzureOpenAIEmbedder:
    return request.app.state.deps.embedder


def get_llm(request: Request) -> AzureOpenAILLM:
    return request.app.state.deps.llm

def get_entity_retriever(request: Request) -> Any:
    return request.app.state.deps.entity_retriever

def get_chunk_retriever(request: Request) -> Any:
    return request.app.state.deps.chunk_retriever

def get_community_retriever(request: Request) -> Any:
    return request.app.state.deps.community_retriever
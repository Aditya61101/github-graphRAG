import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

from github_graphrag.embeddings.azure_openai import AzureOpenAIEmbedder

from ..batching import BatchConfig, TokenBudgetBatcher
from ..candidate_generation import RelationshipCandidateGenerator
from ..canonicalization import EntityCanonicalizer
from ..cross_chunk import CrossChunkReasoner
from ..llm_adapters import (
    AzureOpenAILLM,
    AzureOpenAIExtractor,
    AzureOpenAIRelationshipValidator,
    # SentenceTransformerEmbedder,
)
from ..pipeline import PipelineConfig, RepositoryKnowledgePipeline
from ..store import JsonlCandidateKnowledgeStore
from ..token_estimation import rough_token_count

load_dotenv()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def build_extractor(azure_llm: AzureOpenAILLM):
    return AzureOpenAIExtractor(azure_llm)


def build_relationship_validator(*, azure_llm: AzureOpenAILLM):
    return AzureOpenAIRelationshipValidator(azure_llm, minimum_confidence=0.70)


def build_knowledge_pipeline(candidates_path: str | Path, examples: str = ""):
    api_version = _required_env("AZURE_OPENAI_API_VERSION")
    # _validate_structured_output_api_version(api_version)
    
    client = AsyncAzureOpenAI(
        azure_endpoint=_required_env("AZURE_OPENAI_ENDPOINT"),
        api_key=_required_env("AZURE_OPENAI_API_KEY"),
        api_version=api_version,
    )
    
    azure_llm = AzureOpenAILLM(
        client=client,
        deployment=_required_env("AZURE_OPENAI_DEPLOYMENT_NAME"),
    )
    
    embedding_dimensions = int(
        os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072")
    )
    if embedding_dimensions <= 0:
        raise RuntimeError(
            "AZURE_OPENAI_EMBEDDING_DIMENSIONS must be a positive integer."
        )

    embedder = AzureOpenAIEmbedder(
        client,
        deployment=_required_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"),
        dimensions=embedding_dimensions,
        batch_size=128,
    )

    # embedder = SentenceTransformerEmbedder(model_name="all-MiniLM-L6-v2")
    store = JsonlCandidateKnowledgeStore(candidates_path)

    relationship_validator = build_relationship_validator(azure_llm=azure_llm)

    return RepositoryKnowledgePipeline(
        embedder=embedder,
        extractor=build_extractor(azure_llm=azure_llm),
        store=store,
        canonicalizer=EntityCanonicalizer(),
        candidate_generator=RelationshipCandidateGenerator(),
        cross_chunk_reasoner=CrossChunkReasoner(relationship_validator),
        batcher=TokenBudgetBatcher(
            rough_token_count,
            BatchConfig(max_tokens=12_000, max_chunks=32),
        ),
        config=PipelineConfig(
            examples=examples,
            embedding_dimensions=embedding_dimensions,
        ),
    )

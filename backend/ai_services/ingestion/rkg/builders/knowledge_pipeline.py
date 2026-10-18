from pathlib import Path
import hashlib
import json
from shared.ai.provider import LLM

from ..batching import TokenBudgetBatcher, extraction_batch_config
from ..candidate_generation import RelationshipCandidateGenerator
from ..canonicalization import EntityCanonicalizer
from ..cross_chunk import CrossChunkReasoner
from ..llm_adapters import (
    ExtractionOutput,
    StructuredExtractor,
    StructuredRelationshipValidator,
)
from ..pipeline import PipelineConfig, RepositoryKnowledgePipeline
from ..store import JsonlCandidateKnowledgeStore
from ..token_estimation import rough_token_count
from ..prompts import LOCAL_EXTRACTION_SYSTEM_PROMPT, LOCAL_EXTRACTION_USER_PROMPT

def build_extractor(azure_llm: LLM):
    return StructuredExtractor(azure_llm)


def build_relationship_validator(*, azure_llm: LLM):
    return StructuredRelationshipValidator(azure_llm, minimum_confidence=0.70)


def build_knowledge_pipeline(candidates_path: str | Path, examples: str = "", *, llm=None, embedder=None):
    if llm is None or embedder is None:
        raise ValueError("Knowledge pipeline requires the application provider adapters")
    azure_llm = llm  # historical parameter name; transport selected at the boundary
    embedding_dimensions = embedder.dimensions
    identity = getattr(llm, "identity", "azure-legacy")
    candidates_path = Path(candidates_path)
    if identity != "azure-legacy":
        identity = hashlib.sha256(json.dumps([identity, LOCAL_EXTRACTION_SYSTEM_PROMPT,
            LOCAL_EXTRACTION_USER_PROMPT, ExtractionOutput.model_json_schema(), examples], sort_keys=True).encode()).hexdigest()
        candidates_path = candidates_path.with_name(candidates_path.stem + "_" + identity + candidates_path.suffix)
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
            (lambda text: len(text.encode("utf-8"))) if getattr(llm, "provider", None) == "google" else rough_token_count,
            extraction_batch_config(getattr(llm, "provider", None)),
        ),
        config=PipelineConfig(
            examples=examples,
            embedding_dimensions=embedding_dimensions,
        ),
    )

"""Community Layer for Repository Knowledge Graph (RKG) / GraphRAG."""

from __future__ import annotations

from ai_services.embeddings.base import Embedder

from .config import CommunityConfig
from .detector import (
    DeterministicCommunityIdStrategy,
    GDSLeidenDetector,
    GDSLouvainDetector,
    SnapshotCommunityIdStrategy,
)
from .embedder import CommunityEmbeddingGenerator, build_embedding_text
from .exceptions import (
    CommunityConfigurationError,
    CommunityDetectionError,
    CommunityEmbeddingError,
    CommunityError,
    CommunityPersistenceError,
    CommunityProjectionError,
    CommunitySummaryError,
    CommunityVectorIndexError,
)
from .interfaces import (
    CommunityDetector,
    CommunityIdentityStrategy,
    CommunityStore,
    CommunitySummarizer,
    SummarizationLLM,
    VectorIndexManager,
)
from .membership import Neo4jCommunityStore
from .models import (
    Community,
    CommunityContext,
    CommunityDetectionResult,
    CommunityEmbedding,
    CommunityMember,
    CommunityProcessingState,
    CommunitySummary,
    CommunitySummaryOutput,
    DirectedRelationship,
)
from .pipeline import (
    CommunityPipeline,
    CommunityPipelineResult,
    build_community_pipeline,
)
from .projection import GDSProjectionManager
from .summarizer import LLMCommunitySummarizer
from .vector_index import Neo4jVectorIndexManager

__all__ = [
    # Models
    "Community",
    "CommunityMember",
    "DirectedRelationship",
    "CommunityContext",
    "CommunitySummary",
    "CommunitySummaryOutput",
    "CommunityEmbedding",
    "CommunityProcessingState",
    "CommunityDetectionResult",
    # Config
    "CommunityConfig",
    # Interfaces
    "Embedder",
    "SummarizationLLM",
    "CommunityIdentityStrategy",
    "CommunityDetector",
    "CommunityStore",
    "CommunitySummarizer",
    "VectorIndexManager",
    # Projection & Detection
    "GDSProjectionManager",
    "SnapshotCommunityIdStrategy",
    "DeterministicCommunityIdStrategy",
    "GDSLeidenDetector",
    "GDSLouvainDetector",
    # Storage & Membership
    "Neo4jCommunityStore",
    # Summarization & Embedding
    "LLMCommunitySummarizer",
    "CommunityEmbeddingGenerator",
    "build_embedding_text",
    # Vector Indexing
    "Neo4jVectorIndexManager",
    # Pipeline & Builder
    "CommunityPipeline",
    "CommunityPipelineResult",
    "build_community_pipeline",
    # Exceptions
    "CommunityError",
    "CommunityConfigurationError",
    "CommunityProjectionError",
    "CommunityDetectionError",
    "CommunityPersistenceError",
    "CommunitySummaryError",
    "CommunityEmbeddingError",
    "CommunityVectorIndexError",
]

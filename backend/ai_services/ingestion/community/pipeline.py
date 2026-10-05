"""Orchestration pipeline for the Community Layer."""

from __future__ import annotations

import logging
from typing import Any, Sequence
from pydantic import BaseModel, Field

from ai_services.embeddings.base import Embedder

from .config import CommunityConfig
from .detector import DeterministicCommunityIdStrategy, GDSLeidenDetector
from .embedder import CommunityEmbeddingGenerator
from .exceptions import CommunityPersistenceError
from .interfaces import (
    CommunityDetector,
    CommunityStore,
    CommunitySummarizer,
    SummarizationLLM,
    VectorIndexManager,
)
from .membership import Neo4jCommunityStore
from .projection import GDSProjectionManager
from .summarizer import LLMCommunitySummarizer
from .vector_index import Neo4jVectorIndexManager

logger = logging.getLogger(__name__)


class CommunityPipelineResult(BaseModel):
    """Execution summary for a community pipeline run."""

    community_count: int = Field(description="Total number of detected or targeted communities.")
    summaries_generated: int = Field(description="Number of community summaries newly generated.")
    summaries_skipped_unchanged: int = Field(
        description="Number of communities whose summary was reused due to unchanged context."
    )
    embeddings_generated: int = Field(description="Number of community embeddings newly generated.")
    embeddings_skipped_unchanged: int = Field(
        description="Number of community embeddings skipped because embedding is already current."
    )
    algorithm: str = Field(description="Community detection algorithm used.")


class CommunityPipeline:
    """High-level orchestrator for community detection, summarization, embedding, and vector indexing.

    Composes independent, replaceable components without embedding database queries,
    prompts, or vendor-specific embedding calls directly.
    """

    def __init__(
        self,
        driver: Any,
        projection_manager: GDSProjectionManager,
        detector: CommunityDetector,
        store: CommunityStore,
        summarizer: CommunitySummarizer,
        embedding_generator: CommunityEmbeddingGenerator,
        index_manager: VectorIndexManager,
        config: CommunityConfig | None = None,
    ) -> None:
        self.driver = driver
        self.projection_manager = projection_manager
        self.detector = detector
        self.store = store
        self.summarizer = summarizer
        self.embedding_generator = embedding_generator
        self.index_manager = index_manager
        self.config = config or CommunityConfig()

    async def run(
        self,
        *,
        community_ids: Sequence[str | int] | None = None,
        force_refresh: bool = False,
        cleanup_projection: bool = True,
        await_index_readiness: bool = True,
    ) -> CommunityPipelineResult:
        """Execute the community processing pipeline.

        Args:
            community_ids: Optional subset of community IDs for targeted community processing.
                           If None, runs full authoritative detection on the projected graph.
            force_refresh: If True, regenerates summaries and embeddings even if unchanged.
            cleanup_projection: If True, drops the in-memory GDS projection after full detection.
            await_index_readiness: If True, polls until the vector index reaches ONLINE state.
        """
        is_targeted = community_ids is not None
        logger.info(
            "Starting CommunityPipeline run (mode=%s, force_refresh=%s)",
            "targeted" if is_targeted else "full_sync",
            force_refresh,
        )

        target_community_ids: list[str | int]
        algorithm_name = self.config.algorithm

        # 2. Graph projection & Community detection (only for full synchronization)
        if not is_targeted:
            try:
                proj_stats = await self.projection_manager.ensure_projection(self.driver)
                if proj_stats["nodeCount"] == 0:
                    # A zero-eligible-node full sync is authoritative. Clear any stale
                    # community state before returning.
                    await self.store.persist_memberships({}, is_full_sync=True)
                    logger.info(
                        "Projected graph contains zero community-eligible entities. Cleared stale community state."
                    )
                    return CommunityPipelineResult(
                        community_count=0,
                        summaries_generated=0,
                        summaries_skipped_unchanged=0,
                        embeddings_generated=0,
                        embeddings_skipped_unchanged=0,
                        algorithm=algorithm_name,
                    )

                detection_result = await self.detector.detect(
                    self.driver,
                    self.config.graph_name,
                )
                algorithm_name = detection_result.algorithm

                # 3. Synchronize memberships (authoritative full sync drops empty communities)
                await self.store.persist_memberships(
                    detection_result.assignments,
                    is_full_sync=True,
                )
                target_community_ids = list(detection_result.assignments.keys())
            finally:
                if cleanup_projection:
                    await self.projection_manager.drop_projection_if_exists(self.driver)
                await self.store.cleanup_temporary_detection_properties()
        else:
            target_community_ids = list(community_ids or [])

        total_communities = len(target_community_ids)
        if total_communities == 0:
            logger.info("No communities targeted for processing.")
            return CommunityPipelineResult(
                community_count=0,
                summaries_generated=0,
                summaries_skipped_unchanged=0,
                embeddings_generated=0,
                embeddings_skipped_unchanged=0,
                algorithm=algorithm_name,
            )

        # 4. Load architectural contexts for targeted communities
        contexts = await self.store.load_community_contexts(target_community_ids)
        loaded_context_ids = {context.community_id for context in contexts}
        requested_context_ids = set(target_community_ids)
        if loaded_context_ids != requested_context_ids:
            missing_context_ids = sorted(
                requested_context_ids - loaded_context_ids,
                key=str,
            )
            raise CommunityPersistenceError(
                "Community context invariant failed: contexts were not loaded for "
                f"all requested communities. Missing IDs: {missing_context_ids}"
            )

        # 5. Load persisted processing state (summary & embedding status)
        existing_states = await self.store.get_processing_states(target_community_ids)

        # 6. Generate structured architectural summaries (skipping unchanged contexts)
        new_summaries, reused_summaries = await self.summarizer.summarize_batch(
            contexts,
            existing_states=existing_states,
            force_refresh=force_refresh,
        )

        if new_summaries:
            await self.store.persist_summaries(new_summaries)

        summaries_generated = len(new_summaries)
        summaries_skipped = len(reused_summaries)
        logger.info(
            "Community summarization: %d newly generated, %d reused unchanged",
            summaries_generated,
            summaries_skipped,
        )

        # 7. Independent evaluation of embeddings to generate
        # Invariant: embedding is current IFF has_embedding AND embeddedSummaryHash == target_emb_hash
        all_summaries = new_summaries + reused_summaries
        summaries_to_embed = []

        for s in all_summaries:
            state = existing_states.get(s.community_id)
            target_emb_hash = self.embedding_generator.get_version_hash(s.summary_hash)

            needs_embedding = (
                force_refresh
                or state is None
                or not state.has_embedding
                or state.embedded_summary_hash != target_emb_hash
            )
            if needs_embedding:
                summaries_to_embed.append(s)

        embeddings_generated = 0
        embeddings_skipped = len(all_summaries) - len(summaries_to_embed)

        # The vector index is only needed once there are communities to embed.
        # Create/validate it after summaries are known and before embeddings are persisted.
        await self.index_manager.ensure_index(self.embedding_generator.dimensions)

        if summaries_to_embed:
            embeddings = await self.embedding_generator.generate_embeddings(summaries_to_embed)
            await self.store.persist_embeddings(embeddings)
            embeddings_generated = len(embeddings)

        logger.info(
            "Community embeddings: %d generated, %d skipped unchanged",
            embeddings_generated,
            embeddings_skipped,
        )

        # 8. Optionally await vector index online readiness
        if await_index_readiness and self.config.vector_index_await_timeout > 0:
            await self.index_manager.await_online(
                timeout_seconds=self.config.vector_index_await_timeout,
                poll_interval=self.config.vector_index_poll_interval,
            )

        return CommunityPipelineResult(
            community_count=total_communities,
            summaries_generated=summaries_generated,
            summaries_skipped_unchanged=summaries_skipped,
            embeddings_generated=embeddings_generated,
            embeddings_skipped_unchanged=embeddings_skipped,
            algorithm=algorithm_name,
        )


def build_community_pipeline(
    driver: Any,
    llm: SummarizationLLM,
    embedder: Embedder,
    config: CommunityConfig | None = None,
) -> CommunityPipeline:
    """Convenience builder constructing a standard CommunityPipeline with dependency injection."""
    cfg = config or CommunityConfig()
    projection_mgr = GDSProjectionManager(config=cfg)
    detector = GDSLeidenDetector(
        config=cfg,
        identity_strategy=DeterministicCommunityIdStrategy(),
    )
    store = Neo4jCommunityStore(driver=driver, config=cfg)
    summarizer = LLMCommunitySummarizer(llm=llm, config=cfg)
    embedder_gen = CommunityEmbeddingGenerator(embedder=embedder, config=cfg)
    index_mgr = Neo4jVectorIndexManager(driver=driver, config=cfg)

    return CommunityPipeline(
        driver=driver,
        projection_manager=projection_mgr,
        detector=detector,
        store=store,
        summarizer=summarizer,
        embedding_generator=embedder_gen,
        index_manager=index_mgr,
        config=cfg,
    )

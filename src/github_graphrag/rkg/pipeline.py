from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .batching import TokenBudgetBatcher


@dataclass
class PipelineConfig:
    # Examples are optional few-shot guidance. There is intentionally no rigid
    # extraction schema: architectural scope is defined by the system prompt.
    examples: str = ""
    extraction_concurrency: int = 3
    embedding_batch_size: int = 64


class RepositoryKnowledgePipeline:
    """Repository-level candidate knowledge construction pipeline."""

    def __init__(
        self,
        embedder,
        extractor,
        store,
        canonicalizer,
        candidate_generator,
        cross_chunk_reasoner,
        batcher: TokenBudgetBatcher,
        config: PipelineConfig,
    ):
        self.embedder = embedder
        self.extractor = extractor
        self.store = store
        self.canonicalizer = canonicalizer
        self.candidate_generator = candidate_generator
        self.cross_chunk_reasoner = cross_chunk_reasoner
        self.batcher = batcher
        self.config = config

    async def _embed_chunks(self, chunks):
        if not self.embedder:
            return [None] * len(chunks)

        vectors = []
        batch_size = max(1, self.config.embedding_batch_size)
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors.extend(await self.embedder.embed([chunk.text for chunk in batch]))
        return vectors

    async def process_chunks(self, chunks):
        chunks = list(chunks)
        if not chunks:
            return {
                "chunks": [],
                "candidate_knowledge": [],
                "entities": None,
                "relationship_candidates": [],
            }

        embeddings = await self._embed_chunks(chunks)
        embedded_chunks = [
            chunk.__class__(
                **{
                    **chunk.__dict__,
                    "embedding": embeddings[index],
                }
            )
            for index, chunk in enumerate(chunks)
        ]

        by_id = {chunk.chunk_id: chunk for chunk in embedded_chunks}

        # Resume support: only send chunks without cached candidate knowledge to
        # the LLM. Previously extracted chunks are read back from the store.
        cached = {}
        pending = []
        for chunk in embedded_chunks:
            candidate = await self.store.get_for_chunk(chunk.chunk_id)
            if candidate is None:
                pending.append(chunk)
            else:
                cached[chunk.chunk_id] = candidate

        batches = self.batcher.batches(pending)
        semaphore = asyncio.Semaphore(max(1, self.config.extraction_concurrency))

        async def extract_batch(batch):
            async with semaphore:
                return await self.extractor.extract(
                    batch,
                    examples=self.config.examples,
                )

        batch_results = []
        if batches:
            batch_results = await asyncio.gather(
                *(extract_batch(batch) for batch in batches)
            )

        for results in batch_results:
            for result in results:
                await self.store.put(result)
                cached[result.chunk_id] = result

        # Keep only candidates for the chunks in this ingestion run, regardless
        # of what may exist in a shared historical JSONL cache.
        extracted = [cached[chunk.chunk_id] for chunk in embedded_chunks if chunk.chunk_id in cached]

        all_entities = [entity for knowledge in extracted for entity in knowledge.entities]
        resolution = self.canonicalizer.resolve(
            all_entities,
            scope=f"{embedded_chunks[0].repository}:{embedded_chunks[0].commit}",
        )

        candidates = self.candidate_generator.generate(
            candidates=extracted,
            canonical_entities=resolution.entities,
            extracted_to_canonical=resolution.extracted_to_canonical,
            chunks_by_id=by_id,
        )

        return {
            "chunks": embedded_chunks,
            "candidate_knowledge": extracted,
            "entities": resolution,
            "relationship_candidates": candidates,
        }

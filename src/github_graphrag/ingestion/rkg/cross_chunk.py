from __future__ import annotations

import asyncio

from github_graphrag.ingestion.rkg.models import CanonicalEntity

# from .models import EvidenceChunk, RelationshipCandidate, ValidatedRelationship


class CrossChunkReasoner:
    def __init__(self, validator, *, max_concurrency: int = 4):
        self.validator = validator
        self.max_concurrency = max_concurrency

    async def validate_all(
        self,
        candidates,
        entities,
        chunks_by_id,
        graph_neighborhood_loader,
    ):
        candidates = list(candidates)
        semaphore = asyncio.Semaphore(max(1, self.max_concurrency))
        entities_by_id = entities.entities

        async def validate_one(candidate):
            evidence = [
                chunks_by_id[cid]
                for cid in candidate.evidence_chunk_ids
                if cid in chunks_by_id
            ]
            source_entity = entities_by_id.get(candidate.source_id)
            target_entity = entities_by_id.get(candidate.target_id)

            if source_entity is None or target_entity is None:
                print(
                    "REJECT: missing entity mapping",
                    candidate.source_id,
                    candidate.relationship_type,
                    candidate.target_id,
                )
                return None
            async with semaphore:
                source_neighborhood = await graph_neighborhood_loader(candidate.source_id)
                target_neighborhood = await graph_neighborhood_loader(candidate.target_id)
                neighborhood = (
                    "SOURCE NEIGHBORHOOD:\n"
                    + (source_neighborhood or "(none)")
                    + "\n\nTARGET NEIGHBORHOOD:\n"
                    + (target_neighborhood or "(none)")
                )
                return await self.validator.validate(
                    candidate,
                    source_entity=source_entity,
                    target_entity=target_entity,
                    evidence=evidence,
                    neighborhood=neighborhood,
                )

        results = await asyncio.gather(*(validate_one(c) for c in candidates))
        return [result for result in results if result is not None]

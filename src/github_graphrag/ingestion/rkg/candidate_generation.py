from .models import RelationshipCandidate
from .canonicalization import normalized_entity_key


class RelationshipCandidateGenerator:
    """Candidate generation prevents O(N^2) all-chunk comparisons."""

    def generate(self, *, candidates, canonical_entities, extracted_to_canonical, chunks_by_id):
        result = {}
        for knowledge in candidates:
            for rel in knowledge.relationships:
                source_key = normalized_entity_key(rel.source_label, rel.source_name)
                target_key = normalized_entity_key(rel.target_label, rel.target_name)
                source_id = extracted_to_canonical.get(source_key) if source_key else None
                target_id = extracted_to_canonical.get(target_key) if target_key else None
                if not source_id or not target_id or source_id == target_id:
                    continue

                key = (source_id, target_id, rel.relationship_type)
                evidence = list(dict.fromkeys(rel.source_chunk_ids + [knowledge.chunk_id]))
                if key in result:
                    existing = result[key]
                    existing.evidence_chunk_ids = list(
                        dict.fromkeys(existing.evidence_chunk_ids + evidence)
                    )
                    for prop, value in rel.properties.items():
                        existing.properties.setdefault(prop, value)
                else:
                    result[key] = RelationshipCandidate(
                        source_id=source_id,
                        target_id=target_id,
                        relationship_type=rel.relationship_type,
                        properties=dict(rel.properties),
                        evidence_chunk_ids=evidence,
                        reason="explicitly extracted from one or more evidence chunks",
                    )
        return list(result.values())

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Sequence, TypeAlias

from .models import CanonicalEntity, ExtractedEntity

EntityKey: TypeAlias = tuple[str, str]


def normalized_entity_key(label: object, name: object) -> EntityKey | None:
    """Return the identity key used consistently for aliases and lookups."""
    if not isinstance(label, str) or not isinstance(name, str):
        return None

    normalized_label = label.strip().casefold()
    normalized_name = name.strip().casefold()
    if not normalized_label or not normalized_name:
        return None
    return normalized_label, normalized_name


@dataclass
class AliasRegistry:
    aliases: dict[EntityKey, str] = field(default_factory=dict)

    def add(self, label: str, alias: str, canonical_id: str) -> None:
        key = normalized_entity_key(label, alias)
        if key:
            self.aliases[key] = canonical_id

    def resolve(self, label: str, alias: str) -> str | None:
        key = normalized_entity_key(label, alias)
        return self.aliases.get(key) if key else None

    def remap(self, source_id: str, target_id: str) -> None:
        for key, canonical_id in self.aliases.items():
            if canonical_id == source_id:
                self.aliases[key] = target_id


@dataclass
class CanonicalizationResult:
    entities: dict[str, CanonicalEntity]
    extracted_to_canonical: dict[EntityKey, str]


class EntityCanonicalizer:
    """Exact identity first; semantic matching remains an optional verifier hook."""

    def __init__(self, *, alias_registry=None, semantic_candidates=None, verifier=None):
        self.alias_registry = alias_registry or AliasRegistry()
        self.semantic_candidates = semantic_candidates
        self.verifier = verifier

    @staticmethod
    def canonical_id(label: str, name: str, scope: str) -> str:
        key = normalized_entity_key(label, name)
        if key is None:
            raise ValueError("Canonical entities require a non-empty label and name.")
        return sha256(f"{scope}|{key[0]}|{key[1]}".encode()).hexdigest()

    @staticmethod
    def _merge_properties(target: dict, incoming: dict):
        for key, value in incoming.items():
            if key and value is not None and value != "":
                target.setdefault(key, value)

    def resolve(self, entities: Sequence[ExtractedEntity], *, scope: str):
        canonical, mapping = {}, {}
        for entity in entities:
            key = normalized_entity_key(entity.label, entity.name)
            if key is None:
                # Candidate data can be resumed from a historical cache or
                # constructed by another extractor. Do not let malformed
                # identities enter the authoritative graph.
                continue

            alias_id = self.alias_registry.resolve(entity.label, entity.name)
            if alias_id and alias_id in canonical:
                target = canonical[alias_id]
                self._merge_properties(target.properties, entity.properties)
                target.evidence_chunk_ids = list(dict.fromkeys(
                    target.evidence_chunk_ids + entity.source_chunk_ids
                ))
                mapping[key] = alias_id
                continue

            cid = self.canonical_id(entity.label, entity.name, scope)
            if cid in canonical:
                target = canonical[cid]
                self._merge_properties(target.properties, entity.properties)
                target.evidence_chunk_ids = list(dict.fromkeys(
                    target.evidence_chunk_ids + entity.source_chunk_ids
                ))
            else:
                canonical[cid] = CanonicalEntity(
                    canonical_id=cid,
                    label=entity.label,
                    name=entity.name,
                    properties=dict(entity.properties),
                    evidence_chunk_ids=list(dict.fromkeys(entity.source_chunk_ids)),
                )
            self.alias_registry.add(entity.label, entity.name, cid)
            mapping[key] = cid

        if self.semantic_candidates:
            for entity in entities:
                key = normalized_entity_key(entity.label, entity.name)
                source_id = mapping.get(key) if key else None
                if not source_id or source_id not in canonical:
                    continue

                # Only current, distinct entities are eligible merge targets.
                # This prevents a verifier from accepting the entity itself and
                # keeps the candidate set correct after earlier merges.
                current = [
                    candidate
                    for candidate_id, candidate in canonical.items()
                    if candidate_id != source_id
                ]
                candidates = self.semantic_candidates(entity, current)
                target = next(
                    (
                        candidate
                        for candidate in candidates or []
                        if candidate.canonical_id in canonical
                        and candidate.canonical_id != source_id
                    ),
                    None,
                )
                if target and self.verifier and self.verifier(entity, target):
                    source = canonical[source_id]
                    target_id = target.canonical_id

                    # A semantic merge changes the authoritative identity.
                    # Consolidate every source detail, redirect all lookups,
                    # then remove the obsolete entity before persistence.
                    self._merge_properties(target.properties, source.properties)
                    target.evidence_chunk_ids = list(dict.fromkeys(
                        target.evidence_chunk_ids + source.evidence_chunk_ids
                    ))
                    for alias in [source.name, *source.aliases]:
                        if alias != target.name and alias not in target.aliases:
                            target.aliases.append(alias)
                        self.alias_registry.add(entity.label, alias, target_id)
                    for mapped_key, mapped_id in list(mapping.items()):
                        if mapped_id == source_id:
                            mapping[mapped_key] = target_id
                    self.alias_registry.remap(source_id, target_id)
                    del canonical[source_id]

        return CanonicalizationResult(canonical, mapping)

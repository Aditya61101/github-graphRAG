from dataclasses import dataclass, field
from hashlib import sha256
from typing import Sequence

from .models import CanonicalEntity, ExtractedEntity


@dataclass
class AliasRegistry:
    aliases: dict[str, str] = field(default_factory=dict)

    def add(self, alias: str, canonical_id: str):
        self.aliases[alias.strip().lower()] = canonical_id

    def resolve(self, alias: str):
        return self.aliases.get(alias.strip().lower())


@dataclass
class CanonicalizationResult:
    entities: dict[str, CanonicalEntity]
    extracted_to_canonical: dict[str, str]


class EntityCanonicalizer:
    """Exact identity first; semantic matching remains an optional verifier hook."""

    def __init__(self, *, alias_registry=None, semantic_candidates=None, verifier=None):
        self.alias_registry = alias_registry or AliasRegistry()
        self.semantic_candidates = semantic_candidates
        self.verifier = verifier

    @staticmethod
    def canonical_id(label, name, scope):
        return sha256(f"{scope}|{label}|{name.strip().lower()}".encode()).hexdigest()

    @staticmethod
    def _merge_properties(target: dict, incoming: dict):
        for key, value in incoming.items():
            if key and value is not None and value != "":
                target.setdefault(key, value)

    def resolve(self, entities: Sequence[ExtractedEntity], *, scope: str):
        canonical, mapping = {}, {}
        for entity in entities:
            alias_id = self.alias_registry.resolve(entity.name)
            if alias_id and alias_id in canonical:
                target = canonical[alias_id]
                self._merge_properties(target.properties, entity.properties)
                target.evidence_chunk_ids = list(dict.fromkeys(
                    target.evidence_chunk_ids + entity.source_chunk_ids
                ))
                mapping[f"{entity.label}:{entity.name}"] = alias_id
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
            mapping[f"{entity.label}:{entity.name}"] = cid

        if self.semantic_candidates:
            current = list(canonical.values())
            for entity in entities:
                candidates = self.semantic_candidates(entity, current)
                if candidates and self.verifier and self.verifier(entity, candidates[0]):
                    c = candidates[0]
                    mapping[f"{entity.label}:{entity.name}"] = c.canonical_id
                    if entity.name not in c.aliases and entity.name != c.name:
                        c.aliases.append(entity.name)
                    self._merge_properties(c.properties, entity.properties)
                    c.evidence_chunk_ids = list(dict.fromkeys(
                        c.evidence_chunk_ids + entity.source_chunk_ids
                    ))

        return CanonicalizationResult(canonical, mapping)

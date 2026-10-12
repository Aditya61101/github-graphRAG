import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextSettings:
    max_entities: int = 80
    max_relationships: int = 160
    max_decisions: int = 12
    max_constraints: int = 12
    max_chunks: int = 160
    max_catalog_items: int = 500
    max_adr_documents: int = 100
    max_community_associations: int = 160
    max_scope_evidence_chunks: int = 160
    max_deltas_per_file: int = 40
    max_evidence_chars: int = 6000
    max_context_tokens: int = 24000
    dependency_hops: int = 1

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(
            **{
                name: int(os.getenv("DG_CONTEXT_" + name.upper(), str(value)))
                for name, value in defaults.__dict__.items()
            }
        )

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in self.__dict__.values()):
            raise ValueError("Context limits must be positive integers")
        if self.dependency_hops > 2:
            raise ValueError("Dependency traversal is limited to two hops")

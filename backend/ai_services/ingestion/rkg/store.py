from pathlib import Path

from .interfaces import CandidateKnowledgeStore
from .models import CandidateKnowledge


class JsonlCandidateKnowledgeStore(CandidateKnowledgeStore):
    """Durable append-only candidate cache keyed by content-derived chunk ID."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, CandidateKnowledge] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf8").splitlines():
                if line.strip():
                    item = CandidateKnowledge.model_validate_json(line)
                    self._items[item.chunk_id] = item

    async def put(self, candidate: CandidateKnowledge) -> None:
        existing = self._items.get(candidate.chunk_id)
        if existing is not None and existing == candidate:
            return
        self._items[candidate.chunk_id] = candidate
        with self.path.open("a", encoding="utf8") as handle:
            handle.write(candidate.model_dump_json() + "\n")

    async def get_for_chunk(self, chunk_id: str):
        return self._items.get(chunk_id)

    async def all(self):
        return list(self._items.values())

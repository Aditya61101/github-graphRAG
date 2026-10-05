from dataclasses import dataclass


@dataclass(frozen=True)
class RAGAgentResult:
    answer: str
    sources: dict[str, list[dict]]

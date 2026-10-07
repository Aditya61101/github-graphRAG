from typing import Sequence

from .evidence import RerankedEvidence


def format_retrieval_context(selected_evidence: Sequence[RerankedEvidence]) -> str:
    """Only selected chunk text is model-visible; graph metadata stays in state."""
    if not selected_evidence:
        return "No supporting repository evidence met the relevance threshold. State that the evidence is insufficient to answer."
    return "## Supporting Repository Evidence\n\n" + "\n\n".join(
        f"Source: {item.candidate.file_path or '(unknown file)'}\n"
        f"```text\n{item.candidate.text}\n```"
        for item in selected_evidence
    )

"""Deterministic, bounded ADR context with references to the actual source chunks."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ai_services.ingestion.adr.models import ADRChunk
from ai_services.models.adr_document import ADRDocument

SCOPE_WORDS = re.compile(
    r"\b(scope|applicab\w*|context|all|every|only|except\w*|exclud\w*|unless|"
    r"when|if|subsystem|repository|project|language|directory|workflow|layer)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ADRContext:
    text: str
    evidence_chunk_ids: tuple[str, ...]
    truncated: bool


def build_document_context(
    doc: ADRDocument,
    chunks: Sequence[ADRChunk],
    primary_ids: Sequence[str],
    max_chars: int = 6000,
) -> ADRContext:
    if max_chars < 512:
        raise ValueError("Document context limit must be at least 512 characters")
    # Headings are a navigational hint; only referenced snippets may be cited as evidence.
    headings = [f"{'#' * min(s.level, 6)} {s.heading}" for s in doc.sections]
    if not headings:
        headings = [c.section for c in chunks]
    header = json.dumps(
        {"title": doc.title[:300], "headings": headings}, ensure_ascii=False
    )
    header_limit = min(1200, max_chars // 3)
    header_truncated = len(header) > header_limit
    text = "Document navigation (untrusted):\n" + header[:header_limit] + "\n"
    primary = set(primary_ids)
    candidates = [c for c in chunks if c.chunk_id not in primary]
    # Scope/exception-bearing chunks first, followed by the introduction and other context.
    candidates.sort(key=lambda c: (-len(SCOPE_WORDS.findall(c.text)), c.chunk_index))
    included: list[str] = []
    truncated = header_truncated
    for chunk in candidates:
        prefix = f"\nContext chunk {chunk.chunk_id} ({chunk.section}):\n"
        remaining = max_chars - len(text) - len(prefix)
        if remaining < 64:
            truncated = True
            break
        snippet = chunk.text[:remaining]
        text += prefix + snippet
        included.append(chunk.chunk_id)
        if len(snippet) < len(chunk.text):
            truncated = True
            break
    truncated = truncated or len(included) < len(candidates)
    return ADRContext(
        text=text, evidence_chunk_ids=tuple(included), truncated=truncated
    )


def primary_source_prompt(chunks: Sequence[ADRChunk], context: ADRContext) -> str:
    # JSON encoding prevents content from accidentally breaking structural delimiters.
    return json.dumps(
        {
            "primary_chunks": [
                {"chunk_id": c.chunk_id, "section": c.section, "text": c.text}
                for c in chunks
            ],
            "document_context": context.text,
            "context_truncated": context.truncated,
            "allowed_evidence_chunk_ids": [c.chunk_id for c in chunks]
            + list(context.evidence_chunk_ids),
        },
        ensure_ascii=False,
    )


def validate_applicability_evidence(
    app, primary_ids: Sequence[str], context: ADRContext
):
    """Reject fabricated or unseen evidence; keep requirement provenance independent."""
    allowed = set(primary_ids) | set(context.evidence_chunk_ids)
    refs = app.evidence_chunk_ids + [
        cid for t in app.targets for cid in t.evidence_chunk_ids
    ]
    if not set(refs) <= allowed:
        raise ValueError("Applicability cites chunks outside the supplied ADR evidence")
    app = app.model_copy(deep=True)
    app.evidence_chunk_ids = list(
        dict.fromkeys(list(primary_ids) + app.evidence_chunk_ids)
    )
    if not app.scopes:
        app.scopes = ["unknown"]
    if context.truncated:
        app.uncertainty = list(
            dict.fromkeys(app.uncertainty + ["Document context was truncated."])
        )
    return app

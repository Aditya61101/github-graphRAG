from __future__ import annotations

import re
from typing import Sequence

from ai_services.ingestion.adr.models import ADRChunk
from ai_services.models.adr_document import ADRDocument


class ADRChunker:
    """Chunks parsed ADR documents preserving architectural headings and sections."""

    def __init__(self, max_chunk_chars: int = 2500) -> None:
        self.max_chunk_chars = max_chunk_chars

    def chunk(self, doc: ADRDocument) -> list[ADRChunk]:
        """Produce deterministic, structured chunks from an ADRDocument."""
        if not doc.content.strip():
            return []

        chunks: list[ADRChunk] = []
        chunk_index = 0

        # If parser extracted structured sections (e.g. Context, Decision, Consequences)
        if doc.sections:
            for section in doc.sections:
                sec_heading = section.heading.strip()
                sec_text = section.content.strip()

                if not sec_text:
                    continue

                # Include the heading title in the chunk text for rich contextual embedding
                full_section_text = f"## {sec_heading}\n\n{sec_text}"

                if len(full_section_text) <= self.max_chunk_chars:
                    chunks.append(
                        ADRChunk.create(
                            adr_id=doc.adr_id,
                            repository_id=doc.repository_id,
                            section=sec_heading,
                            chunk_index=chunk_index,
                            text=full_section_text,
                            metadata={
                                "adr_id": doc.adr_id,
                                "repository_id": doc.repository_id,
                                "title": doc.title,
                                "section": sec_heading,
                                "source_name": doc.source_name,
                            },
                        )
                    )
                    chunk_index += 1
                else:
                    # Split oversized sections by paragraphs
                    sub_chunks = self._split_paragraphs(full_section_text)
                    for sub_text in sub_chunks:
                        chunks.append(
                            ADRChunk.create(
                                adr_id=doc.adr_id,
                                repository_id=doc.repository_id,
                                section=sec_heading,
                                chunk_index=chunk_index,
                                text=sub_text,
                                metadata={
                                    "adr_id": doc.adr_id,
                                    "repository_id": doc.repository_id,
                                    "title": doc.title,
                                    "section": sec_heading,
                                    "source_name": doc.source_name,
                                },
                            )
                        )
                        chunk_index += 1

        # Fallback if no sections were parsed
        if not chunks:
            paragraphs = self._split_paragraphs(doc.content)
            for text_block in paragraphs:
                chunks.append(
                    ADRChunk.create(
                        adr_id=doc.adr_id,
                        repository_id=doc.repository_id,
                        section="General",
                        chunk_index=chunk_index,
                        text=text_block,
                        metadata={
                            "adr_id": doc.adr_id,
                            "repository_id": doc.repository_id,
                            "title": doc.title,
                            "section": "General",
                            "source_name": doc.source_name,
                        },
                    )
                )
                chunk_index += 1

        return chunks

    def _split_paragraphs(self, text: str) -> list[str]:
        """Split text on double newlines and strictly enforce max_chunk_chars."""
        raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not raw_paragraphs:
            return self._slice_oversized(text.strip()) if text.strip() else []

        normalized_paragraphs: list[str] = []
        for p in raw_paragraphs:
            if len(p) > self.max_chunk_chars:
                normalized_paragraphs.extend(self._slice_oversized(p))
            else:
                normalized_paragraphs.append(p)

        result: list[str] = []
        current_block: list[str] = []
        current_len = 0

        for p in normalized_paragraphs:
            p_len = len(p)
            if current_len + p_len + 2 > self.max_chunk_chars and current_block:
                result.append("\n\n".join(current_block))
                current_block = [p]
                current_len = p_len
            else:
                current_block.append(p)
                current_len += p_len + 2

        if current_block:
            result.append("\n\n".join(current_block))

        return [r for r in result if r]

    def _slice_oversized(self, text: str) -> list[str]:
        """Strictly bound text blocks so no block exceeds max_chunk_chars."""
        if len(text) <= self.max_chunk_chars:
            return [text]

        pieces: list[str] = []
        # Attempt sentence/line splitting first
        segments = re.split(r"(?<=[.!?\n])\s+", text)
        current = ""

        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue

            if len(seg) > self.max_chunk_chars:
                if current:
                    pieces.append(current)
                    current = ""
                # Hard-slice oversized segments
                for idx in range(0, len(seg), self.max_chunk_chars):
                    sub = seg[idx : idx + self.max_chunk_chars].strip()
                    if sub:
                        pieces.append(sub)
            elif len(current) + len(seg) + 1 > self.max_chunk_chars:
                if current:
                    pieces.append(current)
                current = seg
            else:
                current = f"{current} {seg}".strip() if current else seg

        if current:
            pieces.append(current)

        return [p for p in pieces if p]

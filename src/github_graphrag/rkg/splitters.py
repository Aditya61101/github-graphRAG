from __future__ import annotations
from .models import ChunkStrategy, EvidenceChunk

class RepositoryTextSplitter:
    """Deterministic strategy dispatcher. Never calls an LLM."""

    def __init__(self, *, max_chunk_chars: int = 24_000):
        self.max_chunk_chars = max_chunk_chars

    def split(self, repository, commit, file_path, text, strategy, language=None):
        strategy = ChunkStrategy(strategy)
        if strategy == ChunkStrategy.SYMBOL:
            chunks = self._symbol(repository, commit, file_path, text, language)
        elif strategy == ChunkStrategy.MARKDOWN_SECTION:
            chunks = self._markdown(repository, commit, file_path, text)
        else:
            chunks = [
                EvidenceChunk.create(
                    repository=repository,
                    commit=commit,
                    file_path=file_path,
                    chunk_index=0,
                    text=text,
                    strategy=strategy,
                )
            ]

        # A symbol parser may return nothing for files containing only module-level
        # statements. Never silently drop such a file.
        if not chunks and text.strip():
            chunks = [
                EvidenceChunk.create(
                    repository=repository,
                    commit=commit,
                    file_path=file_path,
                    chunk_index=0,
                    text=text,
                    strategy=ChunkStrategy.FALLBACK,
                    metadata={"fallback_reason": "symbol_parser_returned_no_chunks"},
                )
            ]

        return self._cap_chunks(chunks)

    def _markdown(self, repository, commit, path, text):
        try:
            from markdown_it import MarkdownIt
        except ImportError as exc:
            raise RuntimeError("Install markdown-it for MARKDOWN_SECTION splitting") from exc

        md = MarkdownIt()
        tokens = md.parse(text)

        sections: list[str] = []
        lines = text.splitlines(keepends=True)

        section_start = 0

        for token in tokens:
            if token.type != "heading_open" or token.map is None:
                continue

            heading_start = token.map[0]

            if heading_start > section_start:
                section = "".join(lines[section_start:heading_start]).strip()
                if section:
                    sections.append(section)

            # Start the new section at the heading itself.
            section_start = heading_start

        if section_start < len(lines):
            section = "".join(lines[section_start:]).strip()
            if section:
                sections.append(section)

        return [
            EvidenceChunk.create(
                repository=repository,
                commit=commit,
                file_path=path,
                chunk_index=i,
                text=section,
                strategy=ChunkStrategy.MARKDOWN_SECTION,
            )
            for i, section in enumerate(section for section in sections if section)
        ]

    def _symbol(self, repository, commit, path, text, language):
        if not language:
            raise ValueError("language is required for SYMBOL splitting")
        try:
            from chunker import chunk_text
        except ImportError as exc:
            raise RuntimeError("Install treesitter-chunker for SYMBOL splitting") from exc

        raw_chunks = list(chunk_text(text, language))
        seen: set[tuple[int, int, str]] = set()
        candidates = []
        for item in raw_chunks:
            content = item.content.strip()
            if not content:
                continue
            key = (item.start_line, item.end_line, content)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)

        # treesitter-chunker can emit both a class and every method inside that
        # class. Keep the outer symbol as the authoritative chunk rather than
        # ingesting the same source lines repeatedly. Distinct top-level symbols
        # remain separate.
        candidates.sort(key=lambda item: (item.start_line, -item.end_line))
        kept = []
        for item in candidates:
            if any(
                parent.start_line <= item.start_line
                and parent.end_line >= item.end_line
                and (parent.start_line < item.start_line or parent.end_line > item.end_line)
                for parent in kept
            ):
                continue
            kept.append(item)

        kept.sort(key=lambda item: (item.start_line, item.end_line))
        return [
            EvidenceChunk.create(
                repository=repository,
                commit=commit,
                file_path=path,
                chunk_index=index,
                text=item.content.strip(),
                strategy=ChunkStrategy.SYMBOL,
                metadata={
                    "language": language,
                    "symbol_type": item.node_type,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "parent_context": item.parent_context,
                },
            )
            for index, item in enumerate(kept)
        ]

    def _cap_chunks(self, chunks):
        result: list[EvidenceChunk] = []
        for chunk in chunks:
            if len(chunk.text) <= self.max_chunk_chars:
                result.append(chunk)
                continue

            lines = chunk.text.splitlines(keepends=True)
            part: list[str] = []
            size = 0
            part_index = 0
            for line in lines:
                if part and size + len(line) > self.max_chunk_chars:
                    result.append(
                        self._overflow_chunk(chunk, len(result), part_index, "".join(part))
                    )
                    part_index += 1
                    part = []
                    size = 0
                part.append(line)
                size += len(line)
            if part:
                result.append(
                    self._overflow_chunk(chunk, len(result), part_index, "".join(part))
                )
        return result

    @staticmethod
    def _overflow_chunk(source: EvidenceChunk, index: int, part_index: int, text: str):
        return EvidenceChunk.create(
            repository=source.repository,
            commit=source.commit,
            file_path=source.file_path,
            chunk_index=index,
            text=text.strip(),
            strategy=source.strategy,
            metadata={
                **source.metadata,
                "overflow_part": part_index,
                "overflow_of_chunk": source.chunk_id,
            },
        )

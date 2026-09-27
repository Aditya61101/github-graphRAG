"""
Stage 2 (docs): turn one document into a Doc row and Section rows.

    Markdown / text  -> one Section per heading (heading path kept as a prefix)
    Slides (.pptx)   -> one Section per slide: title + body + speaker notes
    Word (.docx)     -> one Section per Heading 1/2/3

Long sections are split by paragraph so no Section exceeds MAX_SECTION_CHARS.

`doc_type` decides how stage 7 links a doc:
    engineering (md/txt)   -> Functions / Classes / Endpoints
    product (pptx/docx)    -> Functions and Modules
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from .parse_code import sha256

MAX_SECTION_CHARS = 3_000   # ~750 tokens
MIN_SECTION_CHARS = 20


@dataclass
class ParsedDoc:
    doc: dict
    sections: list[dict] = field(default_factory=list)


def parse_doc(repo: str, path: str, data: bytes, kind: str, sha: str) -> ParsedDoc:
    doc = {
        "id": f"{repo}:{path}", "repo": repo, "path": path, "kind": kind, "sha": sha,
        "doc_type": "product" if kind in ("slides", "word") else "engineering",
        "title": path.rsplit("/", 1)[-1],
    }
    if kind == "markdown":
        pieces = _markdown(data.decode("utf-8", "replace"))
    elif kind == "text":
        pieces = [("", data.decode("utf-8", "replace"))]
    elif kind == "slides":
        pieces = _pptx(data)
    elif kind == "word":
        pieces = _docx(data)
    else:
        raise ValueError(f"unsupported doc kind {kind}")

    out = ParsedDoc(doc=doc)
    seen: dict[str, int] = {}
    order = 0
    for heading, body in pieces:
        for part_no, chunk in enumerate(_split(body.strip())):
            if len(chunk) < MIN_SECTION_CHARS:
                continue
            label = heading or "(intro)"
            if part_no:
                label = f"{label} (part {part_no + 1})"
            n = seen.get(label, 0)
            seen[label] = n + 1
            anchor = label if n == 0 else f"{label} #{n + 1}"
            text = f"{heading}\n\n{chunk}" if heading else chunk
            out.sections.append({
                "id": f"{doc['id']}#{anchor}", "repo": repo, "doc_id": doc["id"],
                "path": path, "heading": label, "text": text, "order": order,
                "doc_type": doc["doc_type"], "content_hash": sha256(text),
            })
            order += 1
    return out


# ---------------------------------------------------------------- markdown

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _markdown(text: str) -> list[tuple[str, str]]:
    """[(heading path, body)] - ignores '#' lines inside code fences."""
    pieces: list[tuple[str, str]] = []
    stack: list[str] = []
    buf: list[str] = []
    in_fence = False

    def flush():
        if buf:
            pieces.append((" > ".join(stack), "\n".join(buf)))
            buf.clear()

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            stack[:] = stack[: level - 1] + [m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    return pieces


# ---------------------------------------------------------------- office files

def _pptx(data: bytes) -> list[tuple[str, str]]:
    from pptx import Presentation  # python-pptx

    pieces = []
    for number, slide in enumerate(Presentation(io.BytesIO(data)).slides, start=1):
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip()
        texts = []
        for shape in slide.shapes:
            if shape == slide.shapes.title:
                continue
            if shape.has_text_frame and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    texts.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                texts.append(f"Speaker notes: {notes}")
        heading = f"Slide {number}" + (f": {title}" if title else "")
        pieces.append((heading, "\n".join(texts) or title))
    return pieces


def _docx(data: bytes) -> list[tuple[str, str]]:
    from docx import Document  # python-docx

    pieces: list[tuple[str, str]] = []
    stack: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            pieces.append((" > ".join(stack), "\n".join(buf)))
            buf.clear()

    for para in Document(io.BytesIO(data)).paragraphs:
        style = (para.style.name or "") if para.style is not None else ""
        m = re.match(r"Heading (\d)", style)
        if m and para.text.strip():
            flush()
            level = int(m.group(1))
            stack[:] = stack[: level - 1] + [para.text.strip()]
        elif para.text.strip():
            buf.append(para.text.strip())
    flush()
    return pieces


# ---------------------------------------------------------------- splitting

def _split(body: str) -> list[str]:
    if len(body) <= MAX_SECTION_CHARS:
        return [body]
    chunks, current = [], ""
    for para in re.split(r"\n\s*\n", body):
        if current and len(current) + len(para) + 2 > MAX_SECTION_CHARS:
            chunks.append(current)
            current = ""
        while len(para) > MAX_SECTION_CHARS:           # one giant paragraph
            chunks.append(para[:MAX_SECTION_CHARS])
            para = para[MAX_SECTION_CHARS:]
        current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks

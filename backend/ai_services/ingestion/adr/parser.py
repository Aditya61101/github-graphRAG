from __future__ import annotations

import io
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from ai_services.models.adr_document import ADRDocument, ADRSection


class ADRParseError(Exception):
    """Raised when an ADR document cannot be parsed into normalized text."""
    pass


def clean_title_from_filename(filename: str) -> str:
    """Generate a clean, readable title fallback from a filename."""
    stem = Path(filename).stem
    # Replace underscores and hyphens with spaces
    cleaned = re.sub(r"[-_]+", " ", stem).strip()
    # Normalize multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Title-case if all lower
    if cleaned.islower() or cleaned.isupper():
        cleaned = cleaned.title()
    return cleaned or "Untitled ADR"


def _extract_markdown_sections(text: str) -> tuple[str | None, list[ADRSection]]:
    """Parse Markdown headings into sections and find the document title."""
    lines = text.splitlines()
    sections: list[ADRSection] = []
    extracted_title: str | None = None

    current_heading = ""
    current_level = 1
    current_lines: list[str] = []

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

    for line in lines:
        match = heading_pattern.match(line.strip())
        if match:
            level = len(match.group(1))
            heading_text = match.group(2).strip()

            if extracted_title is None and level == 1:
                extracted_title = heading_text
            elif extracted_title is None and not sections and not current_heading:
                # If no H1 found yet, first heading can be the title
                extracted_title = heading_text

            if current_heading or current_lines:
                sections.append(
                    ADRSection(
                        heading=current_heading or "Overview",
                        level=current_level,
                        content="\n".join(current_lines).strip(),
                    )
                )
                current_lines = []

            current_heading = heading_text
            current_level = level
        else:
            current_lines.append(line)

    if current_heading or current_lines:
        sections.append(
            ADRSection(
                heading=current_heading or "Overview",
                level=current_level,
                content="\n".join(current_lines).strip(),
            )
        )

    return extracted_title, sections


def _parse_markdown_or_txt(file_bytes: bytes, source_name: str) -> tuple[str, str | None, list[ADRSection]]:
    """Decode and extract normalized text from Markdown or plain text bytes."""
    # Attempt decoding with UTF-8, then UTF-8-sig, then latin-1 fallback
    decoded: str | None = None
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            decoded = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        raise ADRParseError(f"Failed to decode text file '{source_name}' with supported encodings.")

    normalized_content = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized_content:
        raise ADRParseError(f"Uploaded text document '{source_name}' is empty.")

    extracted_title, sections = _extract_markdown_sections(normalized_content)
    if not extracted_title:
        for line in normalized_content.splitlines():
            line_str = line.strip()
            if line_str:
                candidate = re.sub(r"^(?:Title|ADR Title|Subject)\s*:\s*", "", line_str, flags=re.IGNORECASE).strip()
                if candidate:
                    extracted_title = candidate
                break

    return normalized_content, extracted_title, sections


def _parse_pdf(file_bytes: bytes, source_name: str) -> tuple[str, str | None, list[ADRSection], dict[str, Any]]:
    """Extract text and metadata from PDF bytes using pypdf."""
    try:
        import pypdf
    except ImportError:
        raise ADRParseError("pypdf is required to parse PDF documents.")

    try:
        stream = io.BytesIO(file_bytes)
        reader = pypdf.PdfReader(stream)
    except Exception as exc:
        raise ADRParseError(f"Corrupted or invalid PDF document '{source_name}': {exc}") from exc

    if len(reader.pages) == 0:
        raise ADRParseError(f"PDF document '{source_name}' has 0 pages.")

    page_texts: list[str] = []
    for idx, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
            if text.strip():
                page_texts.append(text.strip())
        except Exception as exc:
            raise ADRParseError(f"Error extracting text from PDF page {idx + 1}: {exc}") from exc

    full_text = "\n\n".join(page_texts).strip()
    if not full_text:
        raise ADRParseError(f"PDF document '{source_name}' contains no extractable text.")

    # Check PDF metadata title
    meta = reader.metadata or {}
    pdf_title = getattr(meta, "title", None)
    if pdf_title and isinstance(pdf_title, str) and pdf_title.strip():
        extracted_title = pdf_title.strip()
    else:
        # First non-empty line of the first page
        first_line = full_text.splitlines()[0].strip() if full_text.splitlines() else None
        extracted_title = first_line if first_line and len(first_line) < 150 else None

    # Treat markdown-style headings in PDF if any, or construct default section
    _, sections = _extract_markdown_sections(full_text)
    if not sections:
        sections = [ADRSection(heading="Document Content", level=1, content=full_text)]

    extra_meta = {
        "page_count": len(reader.pages),
        "author": getattr(meta, "author", None),
        "producer": getattr(meta, "producer", None),
    }

    return full_text, extracted_title, sections, extra_meta


def _parse_docx(file_bytes: bytes, source_name: str) -> tuple[str, str | None, list[ADRSection]]:
    """Extract text, headings, and structure from a DOCX file using standard library zipfile/XML."""
    stream = io.BytesIO(file_bytes)
    if not zipfile.is_zipfile(stream):
        raise ADRParseError(f"File '{source_name}' is not a valid DOCX (OpenXML zip) archive.")

    try:
        with zipfile.ZipFile(stream) as zf:
            if "word/document.xml" not in zf.namelist():
                raise ADRParseError(f"DOCX document '{source_name}' is missing word/document.xml.")

            xml_content = zf.read("word/document.xml")
            tree = ET.fromstring(xml_content)
    except Exception as exc:
        raise ADRParseError(f"Failed to parse DOCX document XML in '{source_name}': {exc}") from exc

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[tuple[str, str]] = []  # list of (style_or_heading, text)

    for p in tree.iterfind(".//w:p", ns):
        # Check style
        style_val = ""
        p_style = p.find(".//w:pPr/w:pStyle", ns)
        if p_style is not None:
            style_val = p_style.attrib.get(f"{{{ns['w']}}}val", "") or p_style.attrib.get("val", "")

        texts = [t.text for t in p.iterfind(".//w:t", ns) if t.text]
        p_text = "".join(texts).strip()
        if p_text:
            paragraphs.append((style_val, p_text))

    if not paragraphs:
        raise ADRParseError(f"DOCX document '{source_name}' contains no readable text paragraphs.")

    # Build sections and extract title
    full_text_parts: list[str] = []
    sections: list[ADRSection] = []
    extracted_title: str | None = None

    current_heading = ""
    current_level = 1
    current_lines: list[str] = []

    for style, text in paragraphs:
        full_text_parts.append(text)
        is_title = "Title" in style
        is_heading = "Heading" in style

        if is_title and extracted_title is None:
            extracted_title = text

        if is_heading or is_title:
            if extracted_title is None and not sections and not current_heading:
                extracted_title = text

            if current_heading or current_lines:
                sections.append(
                    ADRSection(
                        heading=current_heading or "Overview",
                        level=current_level,
                        content="\n".join(current_lines).strip(),
                    )
                )
                current_lines = []

            current_heading = text
            # Determine level e.g. Heading1 -> 1, Heading2 -> 2
            level_match = re.search(r"\d+", style)
            current_level = int(level_match.group(0)) if level_match else 1
        else:
            current_lines.append(text)

    if current_heading or current_lines:
        sections.append(
            ADRSection(
                heading=current_heading or "Overview",
                level=current_level,
                content="\n".join(current_lines).strip(),
            )
        )

    if extracted_title is None and paragraphs:
        # Fallback to first paragraph if short
        first_p = paragraphs[0][1]
        if len(first_p) < 150:
            extracted_title = first_p

    return "\n\n".join(full_text_parts).strip(), extracted_title, sections


def parse_adr_content(
    file_bytes: bytes,
    file_extension: str,
    source_name: str,
    adr_id: str,
    repository_id: str,
    file_path: str,
    content_hash: str,
    source_type: str = "MANUAL_UPLOAD",
    source_id: str | None = None,
    source_url: str | None = None,
    source_version: str | None = None,
    mime_type: str | None = None,
) -> ADRDocument:
    """Parse raw ADR document bytes into a normalized ADRDocument.

    Supports Markdown (.md, .markdown), plain text (.txt), PDF (.pdf), and DOCX (.docx).
    """
    ext = file_extension.lower().strip()
    extra_meta: dict[str, Any] = {}

    if ext in {".md", ".markdown", ".txt"}:
        content, extracted_title, sections = _parse_markdown_or_txt(file_bytes, source_name)
    elif ext == ".pdf":
        content, extracted_title, sections, extra_meta = _parse_pdf(file_bytes, source_name)
    elif ext == ".docx":
        content, extracted_title, sections = _parse_docx(file_bytes, source_name)
    else:
        raise ADRParseError(f"Unsupported document format '{file_extension}' for ADR parsing.")

    # Title resolution hierarchy:
    # 1. Document heading / title metadata
    # 2. Cleaned filename fallback
    resolved_title: str
    if extracted_title and extracted_title.strip():
        resolved_title = extracted_title.strip()
    else:
        resolved_title = clean_title_from_filename(source_name)

    return ADRDocument(
        adr_id=adr_id,
        repository_id=repository_id,
        title=resolved_title,
        content=content,
        source_type=source_type,
        source_name=source_name,
        source_id=source_id,
        source_url=source_url,
        source_version=source_version,
        file_path=file_path,
        content_hash=content_hash,
        file_size=len(file_bytes),
        mime_type=mime_type,
        file_extension=ext,
        sections=sections,
        metadata=extra_meta,
    )

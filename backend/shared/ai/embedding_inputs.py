"""Lossless UTF-8 windows; authoritative source text is never modified here."""

EMBEDDING_MAX_BYTES = 8192
EMBEDDING_MAX_WINDOWS = 32
EMBEDDING_INPUT_POLICY = "utf8-8192-unit-byte-weighted-mean-v1"


def text_windows(text, *, max_bytes=EMBEDDING_MAX_BYTES, max_chars=None, prefer_lines=True):
    """Return contiguous character offsets, preferring complete source lines.

    Even an individual oversized line is split. Joining the returned slices
    reproduces the original text exactly, including Unicode and whitespace.
    """
    if max_bytes < 4 or (max_chars is not None and max_chars < 1):
        raise ValueError("Invalid text window bounds")
    start = 0
    while start < len(text):
        end, size, line_end = start, 0, None
        while end < len(text) and (max_chars is None or end - start < max_chars):
            width = len(text[end].encode("utf-8"))
            if size + width > max_bytes:
                break
            size += width
            end += 1
            if text[end - 1] == "\n":
                line_end = end
        if prefer_lines and end < len(text) and line_end is not None:
            end = line_end
        yield start, end, text[start:end]
        start = end


def embedding_windows(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Embedding input must be nonempty text")
    if len(text.encode("utf-8")) > EMBEDDING_MAX_BYTES * EMBEDDING_MAX_WINDOWS:
        raise ValueError("Embedding input exceeds bounded window budget")
    # Blank windows carry no semantic content. Keep the authoritative source,
    # offsets and hashes intact; never send an empty native embedding request.
    windows = [part for _, _, part in text_windows(text, prefer_lines=False) if part.strip()]
    if len(windows) > EMBEDDING_MAX_WINDOWS:
        raise ValueError("Embedding input exceeds bounded window budget")
    return windows

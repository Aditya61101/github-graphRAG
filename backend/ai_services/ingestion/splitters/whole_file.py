from .base import whole_file_chunks


def split_whole_file(text: str, metadata: dict | None = None):
    return whole_file_chunks(
        text,
        strategy="whole_file",
        metadata=metadata,
    )

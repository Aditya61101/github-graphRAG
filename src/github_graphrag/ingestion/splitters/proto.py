from .base import whole_file_chunks


def split_proto(text: str, metadata: dict | None = None):
    # TODO: parse protobuf messages/services and emit semantic chunks.
    return whole_file_chunks(
        text,
        strategy="proto_message",
        metadata=metadata,
    )

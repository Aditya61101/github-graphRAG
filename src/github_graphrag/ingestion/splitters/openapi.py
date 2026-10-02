from .base import whole_file_chunks


def split_openapi(text: str, metadata: dict | None = None):
    # TODO: parse OpenAPI paths -> methods and emit one chunk per operation.
    return whole_file_chunks(
        text,
        strategy="openapi_operation",
        metadata=metadata,
    )

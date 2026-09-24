from github_graphrag.ingestion.chunking.strategy_abc import ChunkerStrategy
from github_graphrag.models.source_chunk import SourceChunk

import json
import yaml

class OpenAPIChunker(ChunkerStrategy):
    """
    Creates one chunk per OpenAPI operation.

    Each chunk contains the path, HTTP method, operation metadata,
    parameters, request body, responses, and other operation-level
    information.
    """

    _HTTP_METHODS = {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "trace",
    }

    def chunk(
        self,
        path: str,
        content: str,
        language: str | None = None,
    ) -> list[SourceChunk]:
        if not content.strip():
            return []

        try:
            spec = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Failed to parse OpenAPI file '{path}': {exc}"
            ) from exc

        if not isinstance(spec, dict):
            raise ValueError(
                f"OpenAPI file '{path}' must contain an object."
            )

        paths = spec.get("paths", {})

        if not isinstance(paths, dict):
            raise ValueError(
                f"Invalid OpenAPI paths section in '{path}'."
            )

        chunks: list[SourceChunk] = []

        for route, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            for method, operation in path_item.items():
                method_lower = method.lower()

                if method_lower not in self._HTTP_METHODS:
                    continue

                if not isinstance(operation, dict):
                    continue

                operation_chunk = {
                    "openapi_version": spec.get("openapi"),
                    "path": route,
                    "method": method_lower.upper(),
                    "operation": operation,
                }

                chunks.append(
                    SourceChunk(
                        chunk_id=f"{path}:{method_lower}:{route}",
                        file_path=path,
                        content=json.dumps(
                            operation_chunk,
                            indent=2,
                            ensure_ascii=False,
                        ),
                        chunk_index=len(chunks),
                        strategy="openapi_operation",
                    )
                )

        return chunks
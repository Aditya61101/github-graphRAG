from api_services.app.models.query import APIResponse


def build_api_response(
    result: dict,
    conversation_id: str,
) -> APIResponse:
    sources_by_file: dict[str, SourceFile] = {}

    retrieval_result = result["retrieval_result"]

    for chunk in retrieval_result.chunks:
        file_path = chunk.file_path or "unknown"

        if file_path not in sources_by_file:
            sources_by_file[file_path] = SourceFile(
                file_path=file_path,
            )

        sources_by_file[file_path].chunks.append(
            SourceChunk(
                chunk_id=chunk.chunk_id,
                file_path=chunk.file_path,
                excerpt=chunk.text,
                score=chunk.score,
            )
        )

    return APIResponse(
        answer=result["answer"],
        sources=list(sources_by_file.values()),
        conversation_id=conversation_id,
    )
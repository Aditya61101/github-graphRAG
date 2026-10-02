from llama_index.core import Document
from llama_index.core.node_parser import MarkdownNodeParser
from neo4j_graphrag.components.types import TextChunk, TextChunks


_parser = MarkdownNodeParser()


def split_markdown(text: str, metadata: dict | None = None) -> TextChunks:
    document = Document(text=text)
    nodes = _parser.get_nodes_from_documents([document])

    base_metadata = dict(metadata or {})
    base_metadata["chunk_strategy"] = "markdown_section"

    return TextChunks(
        chunks=[
            TextChunk(
                text=node.get_content(),
                index=index,
                metadata=base_metadata,
            )
            for index, node in enumerate(nodes)
        ]
    )

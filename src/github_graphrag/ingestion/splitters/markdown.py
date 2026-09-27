from llama_index.core.node_parser import MarkdownNodeParser
from neo4j_graphrag.components.text_splitters.llamaindex import (
    LlamaIndexTextSplitterAdapter,
)


def build_markdown_splitter():
    return LlamaIndexTextSplitterAdapter(
        MarkdownNodeParser()
    )

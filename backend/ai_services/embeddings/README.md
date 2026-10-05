# Embedding Providers

Provider-agnostic asynchronous embedding module with batch support.

## Structure

```text
embeddings/
├── __init__.py
├── base.py
├── azure_openai.py
└── sentence_transformer.py
```

The application depends only on the `Embedder` protocol:

```python
from embeddings import Embedder
```

Both providers implement:

```python
async def embed(texts: Sequence[str]) -> list[list[float]]
```

Returned vectors preserve input order.

### Azure OpenAI

```python
from openai import AsyncAzureOpenAI
from embeddings import AzureOpenAIEmbedder

client = AsyncAzureOpenAI(
    azure_endpoint="https://YOUR_RESOURCE.openai.azure.com/",
    api_key="YOUR_KEY",
    api_version="YOUR_API_VERSION",
)

embedder = AzureOpenAIEmbedder(
    client,
    deployment="YOUR_EMBEDDING_DEPLOYMENT",
    dimensions=1536,
    batch_size=128,
)

vectors = await embedder.embed(["first document", "second document"])
```

`batch_size` controls texts per Azure API request. Set `dimensions` to the actual model output dimension; returned vectors are validated.

### Sentence Transformers

```python
from embeddings import SentenceTransformerEmbedder

embedder = SentenceTransformerEmbedder(
    "sentence-transformers/all-MiniLM-L6-v2",
    batch_size=32,
)

vectors = await embedder.embed(["first document", "second document"])
```

Model inference is moved to a worker thread with `asyncio.to_thread()`.

### Pipeline usage

Your repository pipeline should depend on the interface, not a provider:

```python
def build_pipeline(embedder: Embedder):
    return RepositoryKnowledgePipeline(
        ...,
        embedder=embedder,
    )
```

Switching providers only changes construction:

```python
embedder = AzureOpenAIEmbedder(...)
```

or:

```python
embedder = SentenceTransformerEmbedder(...)
```

### Dependencies

```bash
uv add openai
```

for Azure OpenAI, or:

```bash
uv add sentence-transformers
```

for Sentence Transformers.

from dataclasses import dataclass
from typing import Sequence
from .models import EvidenceChunk

# Raw source bytes, not tokens or the rendered system/schema envelope. Keep the
# legacy Azure token profile unchanged. Source production uses the same cap.
GEMINI_EXTRACTION_MAX_BYTES = 6000
GEMINI_EXTRACTION_MAX_CHUNKS = 8

@dataclass(frozen=True)
class BatchConfig:
    max_tokens: int = 12000
    max_chunks: int = 32
    reject_oversized: bool = False


def extraction_batch_config(provider):
    if provider == "google":
        return BatchConfig(GEMINI_EXTRACTION_MAX_BYTES, GEMINI_EXTRACTION_MAX_CHUNKS, True)
    return BatchConfig()

class TokenBudgetBatcher:
    def __init__(self, estimator, config=None):
        self.estimator = estimator
        self.config = config or BatchConfig()

    def batches(self, chunks: Sequence[EvidenceChunk]):
        batches, current, current_tokens = [], [], 0
        for chunk in chunks:
            tokens = max(1, self.estimator(chunk.text))
            if self.config.reject_oversized and tokens > self.config.max_tokens:
                # Never truncate or invent replacement evidence IDs in a batcher.
                raise ValueError("Extraction singleton exceeds source-production byte bound")
            if current and (current_tokens + tokens > self.config.max_tokens
                             or len(current) >= self.config.max_chunks):
                batches.append(current)
                current, current_tokens = [], 0
            current.append(chunk)
            current_tokens += tokens
        if current:
            batches.append(current)
        return batches

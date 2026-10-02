from dataclasses import dataclass
from typing import Sequence
from .models import EvidenceChunk

@dataclass(frozen=True)
class BatchConfig:
    max_tokens: int = 12000
    max_chunks: int = 32

class TokenBudgetBatcher:
    def __init__(self, estimator, config=None):
        self.estimator = estimator
        self.config = config or BatchConfig()

    def batches(self, chunks: Sequence[EvidenceChunk]):
        batches, current, current_tokens = [], [], 0
        for chunk in chunks:
            tokens = max(1, self.estimator(chunk.text))
            if current and (current_tokens + tokens > self.config.max_tokens
                             or len(current) >= self.config.max_chunks):
                batches.append(current)
                current, current_tokens = [], 0
            current.append(chunk)
            current_tokens += tokens
        if current:
            batches.append(current)
        return batches

"""Dedicated Qwen relevance scoring following the model's Transformers recipe.

https://huggingface.co/Qwen/Qwen3-Reranker-0.6B
"""
import logging
from threading import Lock
from typing import Protocol, Sequence

from .evidence import EvidenceCandidate, RerankedEvidence
from .settings import RetrievalSettings

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    def rerank(self, query: str, candidates: Sequence[EvidenceCandidate]) -> list[RerankedEvidence]: ...


class Qwen3Reranker:
    def __init__(self, settings: RetrievalSettings):
        # Imports and model loading happen at startup, never in retrieval.
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.settings = settings
        self.lock = Lock()
        device = settings.reranker_device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(settings.reranker_model, padding_side="left")
            self.model = AutoModelForCausalLM.from_pretrained(settings.reranker_model).to(self.device).eval()
            self.no_id = self.tokenizer.convert_tokens_to_ids("no")
            self.yes_id = self.tokenizer.convert_tokens_to_ids("yes")
            prefix = (
                '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query '
                'and the Instruct provided. Note that the answer can only be "yes" or "no".'
                '<|im_end|>\n<|im_start|>user\n'
            )
            suffix = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
            self.prefix_tokens = self.tokenizer.encode(prefix, add_special_tokens=False)
            self.suffix_tokens = self.tokenizer.encode(suffix, add_special_tokens=False)
        except Exception as exc:
            raise RuntimeError(f"Cannot initialize reranker {settings.reranker_model} on {device}") from exc
        logger.info("Reranker initialized model=%s device=%s", settings.reranker_model, device)

    def rerank(self, query, candidates):
        candidates = list(candidates)
        if not candidates:
            return []
        scores = []
        # Serialize calls to the shared model to bound CPU/GPU memory usage.
        with self.lock, self.torch.inference_mode():
            for start in range(0, len(candidates), self.settings.reranker_batch_size):
                batch = candidates[start:start + self.settings.reranker_batch_size]
                pairs = [
                    '<Instruct>: Given a software repository question, retrieve source evidence that answers it'
                    f'\n<Query>: {query}\n<Document>: File: {candidate.file_path or "(unknown)"}\n{candidate.text}'
                    for candidate in batch
                ]
                inputs = self.tokenizer(
                    pairs, padding=False, truncation=True, add_special_tokens=False,
                    return_attention_mask=False,
                    max_length=self.settings.reranker_max_length - len(self.prefix_tokens) - len(self.suffix_tokens),
                )
                inputs['input_ids'] = [self.prefix_tokens + tokens + self.suffix_tokens for tokens in inputs['input_ids']]
                inputs = self.tokenizer.pad(inputs, padding=True, return_tensors="pt")
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                # Qwen3 supports logits_to_keep: avoid materializing vocabulary
                # logits for every source token, especially on CPU.
                logits = self.model(**inputs, use_cache=False, logits_to_keep=1).logits[:, -1, :]
                yes_no = self.torch.stack([logits[:, self.no_id], logits[:, self.yes_id]], dim=1)
                scores.extend(self.torch.softmax(yes_no.float(), dim=1)[:, 1].cpu().tolist())
        ranked = sorted(zip(candidates, scores), key=lambda pair: (-pair[1], pair[0].chunk_id))
        return [RerankedEvidence(candidate, score, rank) for rank, (candidate, score) in enumerate(ranked, 1)]

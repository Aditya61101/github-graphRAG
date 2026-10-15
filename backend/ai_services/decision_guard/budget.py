"""Named, model-specific token counters without tokenizer downloads or clients."""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .models import digest


def coverage_summary(limitations, repository_id, *, relevant_ids=(), relevant_paths=()):
    """Bounded discovery hints; full immutable diagnostics stay in context/result audits."""
    details = [l.model_dump(mode="json") for l in limitations]
    counts = Counter(l.code for l in limitations)
    relevant_ids, relevant_paths = set(relevant_ids), set(relevant_paths)

    def priority(code):
        relevant = any(
            l.code == code
            and (
                relevant_ids.intersection(l.item_ids)
                or relevant_paths.intersection(l.paths)
            )
            for l in limitations
        )
        integrity = any(
            word in code for word in ("FAILED", "MISMATCH", "UNAVAILABLE", "INCOMPLETE")
        )
        return (not integrity, not relevant, -counts[code], code)

    selected = sorted(counts, key=priority)[:12]
    categories = []
    for code in selected:
        rows = sorted(
            (l for l in limitations if l.code == code),
            key=lambda l: (
                not bool(
                    relevant_ids.intersection(l.item_ids)
                    or relevant_paths.intersection(l.paths)
                ),
                l.item_ids,
                l.paths,
                l.message,
            ),
        )
        categories.append(
            {
                "code": code,
                "count": counts[code],
                "example_item_ids": list(
                    dict.fromkeys(i[:160] for l in rows for i in l.item_ids)
                )[:2],
                "example_paths": list(
                    dict.fromkeys(p[:160] for l in rows for p in l.paths)
                )[:1],
            }
        )
        if code.startswith(("HYBRID_", "RETRIEVAL_", "INDEX_", "SEMANTIC_")):
            # Retrieval-only identities are audit pointers, not proof citations.
            # Keep counts and the full transcript hash without charging opaque
            # omitted candidate IDs against architectural evidence capacity.
            categories[-1]["example_item_ids"] = [
                i for i in categories[-1]["example_item_ids"] if i in relevant_ids
            ]
    return {
        "schema_version": "dg-coverage-v1",
        "repository_id": repository_id,
        "partial": bool(limitations),
        "total_details": len(details),
        "diagnostic_hash": digest(
            json.dumps(
                {"repository_id": repository_id, "details": details},
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        "categories": categories,
        "other_category_count": len(counts) - len(selected),
        "other_detail_count": sum(
            n for code, n in counts.items() if code not in selected
        ),
    }


def model_context_json(ctx):
    """Stage 4.1 capacity projection, excluding the unbounded audit transcript."""
    data = ctx.model_dump(mode="json", exclude={"budget"})
    data["coverage"]["limitations"] = coverage_summary(
        ctx.coverage.limitations,
        ctx.revision.repository_id,
        relevant_ids=[d.delta_id for d in ctx.deltas],
        relevant_paths=[d.path for d in ctx.deltas],
    )
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ModelTokenCounter:
    name: str
    count_tokens: Callable[[str], int]
    reserve_tokens: int = 0

    def __post_init__(self):
        if (
            not self.name
            or type(self.reserve_tokens) is not int
            or self.reserve_tokens < 0
        ):
            raise ValueError("Expected a named model estimator and nonnegative reserve")

    def __call__(self, text: str) -> int:
        count = self.count_tokens(text)
        if type(count) is not int or count < 0:
            raise ValueError("Tokenizer must return a nonnegative integer")
        return count + self.reserve_tokens

    @classmethod
    def from_encoding(cls, encoding, *, model: str, reserve_tokens: int = 0):
        # Caller supplies an already available encoding for the actual model.
        return cls(
            "model:" + model,
            lambda text: len(encoding.encode(text, disallowed_special=())),
            reserve_tokens,
        )

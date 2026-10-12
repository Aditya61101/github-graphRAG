"""Named, model-specific token counters without tokenizer downloads or clients."""

from collections.abc import Callable
from dataclasses import dataclass


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

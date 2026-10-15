"""Exact BM25, repository-local statistics, no Lucene scorer assumptions."""

import math
import re
from collections import Counter

from ..discovery import identifier_terms


def tokenize(text):
    tokens = []
    for match in re.finditer(r"[A-Za-z_$][\w$]*", text):
        tokens.extend(
            dict.fromkeys(t.casefold() for t in identifier_terms(match.group()))
        )
    return tuple(tokens)


class BM25:
    def __init__(self, units, k1=1.2, b=0.75):
        self.units, self.k1, self.b = tuple(units), k1, b
        self.frequencies = [Counter(u.lexical_tokens) for u in self.units]
        self.df = Counter(t for counts in self.frequencies for t in counts)
        self.average = sum(len(u.lexical_tokens) for u in self.units) / max(
            1, len(self.units)
        )

    def search(self, text):
        query = set(tokenize(text))
        size = len(self.units)
        results = []
        for unit, counts in zip(self.units, self.frequencies, strict=True):
            score = 0.0
            for term in sorted(query & counts.keys()):
                tf = counts[term]
                idf = math.log(1 + (size - self.df[term] + 0.5) / (self.df[term] + 0.5))
                denominator = tf + self.k1 * (
                    1
                    - self.b
                    + self.b * len(unit.lexical_tokens) / max(1, self.average)
                )
                score += idf * tf * (self.k1 + 1) / denominator
            if score > 0:
                results.append((unit.canonical_id, score))
        return sorted(results, key=lambda pair: (-pair[1], pair[0]))

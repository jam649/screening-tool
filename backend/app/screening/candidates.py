"""Token/phonetic blocking with a recall fallback over the complete name index."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator
from itertools import compress, repeat
from operator import mul
from typing import Any

from metaphone import doublemetaphone
from rapidfuzz import fuzz

from app.screening.batch import SLACK, WORKERS, score_rows
from app.screening.normalize import NormalizedName
from app.screening.score import names
from app.screening.settings import ScreeningSettings

# Fallback queries scanned per rapidfuzz call; bounds the result buffer to a
# few megabytes while giving every worker thread its own rows.
FALLBACK_BATCH = 8 * WORKERS


class CandidateIndex:
    def __init__(self, entries: list[Any], settings: ScreeningSettings):
        self.settings = settings
        self.entries = {entry.id: entry for entry in entries}
        self.tokens: dict[str, set[str]] = defaultdict(set)
        self.phonetic: dict[str, set[str]] = defaultdict(set)
        self.trigrams: dict[str, set[str]] = defaultdict(set)
        self.approximate: dict[str, frozenset[str]] = {}
        # Every distinct name variant and the entries carrying it. Identical
        # strings score identically, so the fallback compares each only once.
        self.full_index: dict[str, set[str]] = defaultdict(set)
        self.fallback_hits: dict[str, set[str]] = {}
        self.stopwords = set(settings.candidate_stopwords)
        # Kept for this index's lifetime: entry names outnumber the bounded
        # normalization cache, so later lookups would otherwise recompute them.
        self.names: dict[str, list[NormalizedName]] = {}
        for entry in entries:
            self.names[entry.id] = names(entry, settings, party=False)
            for name in self.names[entry.id]:
                for variant in name.variants:
                    self.full_index[variant].add(entry.id)
                    tokens = variant.split()
                    for token in tokens:
                        if len(token) >= 3 and token not in self.stopwords:
                            self.tokens[token].add(entry.id)
                            if len(token) >= 5:
                                for gram in self._grams(token):
                                    self.trigrams[gram].add(token)
                    if tokens:
                        for code in doublemetaphone(tokens[0]):
                            if code:
                                self.phonetic[code].add(entry.id)
        # Shortest first, so the names of any length band are contiguous.
        self.choices = sorted(self.full_index, key=len)
        self.lengths = [len(choice) for choice in self.choices]
        self.sorted_tokens = [" ".join(sorted(choice.split())) for choice in self.choices]
        self.choice_tokens: dict[str, list[int]] = defaultdict(list)
        self.repeated_tokens: list[int] = []
        for position, choice in enumerate(self.choices):
            tokens = choice.split()
            if len(set(tokens)) < len(tokens):
                self.repeated_tokens.append(position)
            for token in set(tokens):
                self.choice_tokens[token].append(position)

    @staticmethod
    def _grams(token: str) -> set[str]:
        return {token[n : n + 3] for n in range(len(token) - 2)}

    def _approximate_ids(self, token: str) -> frozenset[str]:
        if token not in self.approximate:
            # Supplement all name parts, including surnames, independently of
            # unrelated exact blocks. Two shared trigrams and ratio >=80 keep
            # retrieval bounded by the word index without imposing a result cap.
            words: dict[str, int] = defaultdict(int)
            for gram in self._grams(token):
                for word in self.trigrams.get(gram, ()):
                    words[word] += 1
            ids: set[str] = set()
            for word, count in words.items():
                if count >= 2 and fuzz.ratio(token, word, score_cutoff=80) >= 80:
                    ids.update(self.tokens[word])
            self.approximate[token] = frozenset(ids)
        return self.approximate[token]

    def _blocks(self, party_names: list[NormalizedName]) -> Iterator[Iterable[str]]:
        for name in party_names:
            for variant in name.variants:
                tokens = variant.split()
                for token in tokens:
                    if len(token) >= 3 and token not in self.stopwords:
                        yield self.tokens.get(token, ())
                        if len(token) >= 5:
                            yield self._approximate_ids(token)
                if tokens:
                    for code in doublemetaphone(tokens[0]):
                        yield self.phonetic.get(code, ())

    def _falls_back(self, party_names: list[NormalizedName]) -> bool:
        ids: set[str] = set()
        for block in self._blocks(party_names):
            ids.update(block)
            if len(ids) >= 3:
                return False
        return True

    def _similar_band(self, shortest: int, longest: int) -> tuple[int, int]:
        """Positions of the names WRatio compares with both lengths as similar (< 1.5)."""

        def similar(length: int, other: int) -> bool:
            return (length / other if length > other else other / length) < 1.5

        band = [
            length
            for length in range(1, self.lengths[-1] + 1 if self.lengths else 1)
            if similar(length, shortest) and similar(length, longest)
        ]
        if not band:
            return 0, 0
        return bisect_left(self.lengths, band[0]), bisect_right(self.lengths, band[-1])

    def _scan(self, variants: list[str]) -> None:
        # WRatio with the same cutoff extract_iter applied; below-cutoff scores
        # are returned as 0 and never admitted.
        cutoff = self.settings.candidate_min
        admits, could_admit = float(cutoff).__le__, (float(cutoff) - SLACK).__le__
        variants = sorted(variants, key=len)
        for start in range(0, len(variants), FALLBACK_BATCH):
            batch = variants[start : start + FALLBACK_BATCH]
            repeated = any(len(set(v.split())) < len(v.split()) for v in batch)
            low, high = (0, 0) if repeated else self._similar_band(len(batch[0]), len(batch[-1]))
            others = self.choices[:low] + self.choices[high:]
            rows = score_rows(batch, others, fuzz.WRatio, score_cutoff=cutoff, workers=WORKERS)
            # For similar lengths WRatio is max(ratio, 0.95 * token_ratio), and
            # token_set_ratio equals the sorted-token ratio unless a token is
            # shared or repeated: an exact ceiling that excludes most names.
            ratios = score_rows(batch, self.choices[low:high], fuzz.ratio, workers=WORKERS)
            sorts = score_rows(
                [" ".join(sorted(v.split())) for v in batch],
                self.sorted_tokens[low:high],
                fuzz.ratio,
                workers=WORKERS,
            )
            for variant, row, ratio_row, sort_row in zip(batch, rows, ratios, sorts, strict=True):
                hits: set[str] = set()
                for choice in compress(others, map(admits, row)):
                    hits.update(self.full_index[choice])
                ceilings = map(max, ratio_row, map(mul, repeat(0.95), sort_row))
                possible = set(compress(range(low, high), map(could_admit, ceilings)))
                possible.update(
                    position
                    for token in set(variant.split())
                    for position in self.choice_tokens.get(token, ())
                    if low <= position < high
                )
                possible.update(p for p in self.repeated_tokens if low <= p < high)
                exact = [self.choices[position] for position in sorted(possible)]
                (scores,) = score_rows([variant], exact, fuzz.WRatio, score_cutoff=cutoff)
                for choice in compress(exact, map(admits, scores)):
                    hits.update(self.full_index[choice])
                self.fallback_hits[variant] = hits

    def prefetch_fallbacks(self, parties: Iterable[Any]) -> None:
        """Scan every fallback party's variants together so worker threads share them."""
        pending: dict[str, None] = {}
        for party in parties:
            party_names = names(party, self.settings, party=True)
            if self._falls_back(party_names):
                for name in party_names:
                    pending.update(
                        dict.fromkeys(v for v in name.variants if v not in self.fallback_hits)
                    )
        self._scan(list(pending))

    def candidates(self, party: Any) -> tuple[list[Any], bool]:
        ids: set[str] = set()
        party_names = names(party, self.settings, party=True)
        for block in self._blocks(party_names):
            ids.update(block)
        fallback = len(ids) < 3
        if fallback:
            for name in party_names:
                # The fallback scans every name, not only already blocked candidates.
                self._scan([v for v in name.variants if v not in self.fallback_hits])
                for variant in name.variants:
                    ids.update(self.fallback_hits[variant])
        return [self.entries[key] for key in sorted(ids)], fallback

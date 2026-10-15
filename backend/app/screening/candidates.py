"""Token/phonetic blocking with a recall fallback over the complete name index."""

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Sequence
from itertools import chain, compress, islice, repeat
from operator import mul, truth
from typing import Any

from metaphone import doublemetaphone
from rapidfuzz import fuzz, process

from app.screening.batch import WORKERS, score_rows
from app.screening.normalize import NormalizedName
from app.screening.score import names
from app.screening.settings import ScreeningSettings

# Near score_cutoff, batched WRatio and extract_iter apply slightly different
# tolerances: the normalized cutoff carries a small tolerance, and the
# 0.95/0.9/0.6-scaled results are not rechecked against the original cutoff
# (observed gaps are below 1e-5). Within this far wider margin of the cutoff,
# names are always rescored and extract_iter decides their admission.
FALLBACK_MARGIN = 0.01
# Fallback parties whose names are scanned together, in batches of rows that
# keep every worker busy. Admitted entries are held only for the parties in
# this window, so memory never grows with the number of fallback parties.
FALLBACK_WINDOW = 8 * WORKERS
FALLBACK_ROWS = 2 * WORKERS


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
        self.planned: deque[tuple[str, ...]] = deque()
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

    def _falls_back(self, party_names: list[NormalizedName]) -> bool:
        ids: set[str] = set()
        for block in self._blocks(party_names):
            ids.update(block)
            if len(ids) >= 3:
                return False
        return True

    def _scan(self, variants: list[str]) -> None:
        """Entries with a name that extract_iter(WRatio, score_cutoff=candidate_min) admits."""
        cutoff = self.settings.candidate_min
        if cutoff < 1:
            # Near-zero cutoffs fall inside rapidfuzz's internal cutoff tolerance,
            # where extract_iter may even admit zero scores. Such degenerate
            # settings need no speed, so they use extract_iter itself.
            for variant in variants:
                self.fallback_hits[variant] = set().union(
                    *(
                        self.full_index[choice]
                        for choice, _, _ in process.extract_iter(
                            variant, self.choices, scorer=fuzz.WRatio, score_cutoff=cutoff
                        )
                    )
                )
            return

        certain = float(cutoff) + FALLBACK_MARGIN

        def admitted(variant: str, names: list[str], scores: Sequence[float]) -> list[str]:
            # Batched WRatio reports names it rejects as 0, and extract_iter
            # never admits those at a cutoff of one or more. Well above the
            # cutoff a nonzero score is admitted outright; nearer it the batch
            # and extract_iter tolerances differ in both directions, so
            # extract_iter itself decides the few names there.
            result: list[str] = []
            near: list[str] = []
            for position in compress(range(len(names)), map(truth, scores)):
                (result if scores[position] >= certain else near).append(names[position])
            result.extend(
                choice
                for choice, _, _ in process.extract_iter(
                    variant, near, scorer=fuzz.WRatio, score_cutoff=cutoff
                )
            )
            return result

        could_admit = (float(cutoff) - FALLBACK_MARGIN).__le__
        variants = sorted(variants, key=len)
        for start in range(0, len(variants), FALLBACK_ROWS):
            batch = variants[start : start + FALLBACK_ROWS]
            repeated = any(len(set(v.split())) < len(v.split()) for v in batch)
            low, high = (0, 0) if repeated else self._similar_band(len(batch[0]), len(batch[-1]))
            others = self.choices[:low] + self.choices[high:]
            rows = score_rows(batch, others, fuzz.WRatio, score_cutoff=cutoff, workers=WORKERS)
            # For similar lengths WRatio is max(ratio, 0.95 * token_ratio), and
            # token_set_ratio equals the sorted-token ratio unless a token is
            # shared or repeated: an exact ceiling that excludes most names.
            # Names within FALLBACK_MARGIN of the cutoff are still scored.
            ratios = score_rows(batch, self.choices[low:high], fuzz.ratio, workers=WORKERS)
            sorts = score_rows(
                [" ".join(sorted(v.split())) for v in batch],
                self.sorted_tokens[low:high],
                fuzz.ratio,
                workers=WORKERS,
            )
            for variant, row, ratio_row, sort_row in zip(batch, rows, ratios, sorts, strict=True):
                hits: set[str] = set()
                for choice in admitted(variant, others, row):
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
                for choice in admitted(variant, exact, scores):
                    hits.update(self.full_index[choice])
                self.fallback_hits[variant] = hits

    def plan_fallbacks(self, parties: Iterable[Any]) -> None:
        """Record, in screening order, the names of parties that will fall back.

        Only names are kept; candidates() scans them a window at a time.
        """
        for party in parties:
            party_names = names(party, self.settings, party=True)
            if self._falls_back(party_names):
                self.planned.append(
                    tuple(dict.fromkeys(chain.from_iterable(name.variants for name in party_names)))
                )

    def candidates(self, party: Any) -> tuple[list[Any], bool]:
        ids: set[str] = set()
        party_names = names(party, self.settings, party=True)
        for block in self._blocks(party_names):
            ids.update(block)
        fallback = len(ids) < 3
        if fallback:
            # The fallback scans every name, not only already blocked candidates.
            variants = tuple(dict.fromkeys(chain.from_iterable(n.variants for n in party_names)))
            if self.planned and self.planned[0] == variants:
                self.planned.popleft()
            ahead = list(islice(self.planned, FALLBACK_WINDOW - 1))
            if any(v not in self.fallback_hits for v in variants):
                pending = dict.fromkeys(chain(variants, *ahead))
                self._scan([v for v in pending if v not in self.fallback_hits])
            for variant in variants:
                ids.update(self.fallback_hits[variant])
            # Release what no party still inside the window needs.
            needed = set(chain.from_iterable(ahead))
            for variant in variants:
                if variant not in needed:
                    del self.fallback_hits[variant]
        return [self.entries[key] for key in sorted(ids)], fallback

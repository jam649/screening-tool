"""Proven ceilings that let a screening skip comparisons which cannot be retained.

A ceiling is never an estimate. Anything that could reach candidate_min, or
change the stored best comparison, is still scored by score_pair itself.

For two normalized variants (alphanumeric tokens joined by single spaces):

- token_sort_ratio is fuzz.ratio of the two sorted-token strings.
- When the token sets are disjoint and neither side repeats a token,
  token_set_ratio compares exactly those sorted-token strings and equals that
  ratio. Otherwise it is computed directly; it depends only on the token sets.
- partial_ratio aligns the shorter string (length m) with windows of at most m
  characters of the longer one. A window shares at most h characters with it,
  where h is the multiset character overlap of the whole strings (the LCS of
  both strings with their characters sorted), so no window scores above
  min(100, 200h / (m + h)). This rises with h and falls with m.

All three depend only on a variant's sorted tokens, so each party's distinct
keys are compared with every candidate listing's keys in one C++ batch. A
listing's ceiling combines each ratio's maximum over all of those pairings;
per-pairing ceilings bound individual comparisons.
"""

from collections.abc import Callable
from dataclasses import dataclass
from itertools import accumulate, chain
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import LCSseq

from app.screening.batch import SLACK, score_rows
from app.screening.normalize import NormalizedName
from app.screening.score import EntryFeatures, PartyFeatures, entry_features
from app.screening.settings import ScreeningSettings


@dataclass(frozen=True, slots=True)
class _Key:
    text: str
    tokens: frozenset[str]
    repeated: bool
    characters: str
    length: int


@dataclass(frozen=True, slots=True)
class Listing:
    """An entry's comparison inputs, deduplicated by sorted-token key."""

    features: EntryFeatures
    keys: tuple[_Key, ...]
    texts: tuple[str, ...]
    characters: tuple[str, ...]
    tokens: frozenset[str]
    repeated: bool
    shortest: int


def _partial_ceiling(overlap: int, shorter: int) -> float:
    return min(100.0, 200 * overlap / (shorter + overlap))


class NameBounds:
    """Comparison inputs for one screening snapshot; discarded with it."""

    def __init__(
        self,
        settings: ScreeningSettings,
        entry_names: dict[str, list[NormalizedName]] | None = None,
    ):
        # entry_names: names already normalized by the CandidateIndex, by entry id.
        self.settings, self.entry_names = settings, entry_names or {}
        self.keys: dict[str, _Key] = {}
        self.listings: dict[str, Listing] = {}

    def key(self, variant: str) -> _Key:
        found = self.keys.get(variant)
        if found is None:
            tokens = variant.split()
            found = self.keys[variant] = _Key(
                " ".join(sorted(tokens)),
                frozenset(tokens),
                len(set(tokens)) < len(tokens),
                "".join(sorted(variant)),
                len(variant),
            )
        return found

    def listing(self, entry: Any) -> Listing:
        """Features of an entry, derived when it is first a candidate."""
        found = self.listings.get(entry.id)
        if found is None:
            features = entry_features(entry, self.settings, self.entry_names.get(entry.id))
            keys = tuple(
                {
                    key.text: key for name in features.names for key in map(self.key, name.variants)
                }.values()
            )
            found = self.listings[entry.id] = Listing(
                features,
                keys,
                tuple(key.text for key in keys),
                tuple(key.characters for key in keys),
                frozenset().union(*(key.tokens for key in keys)),
                any(key.repeated for key in keys),
                min((key.length for key in keys), default=0),
            )
        return found

    def party(self, party: PartyFeatures, listings: list[Listing]) -> "PartyBounds":
        lefts = {key.text: key for name in party.names for key in map(self.key, name.variants)}
        return PartyBounds(self, list(lefts.values()), listings)


class PartyBounds:
    """Name-score ceilings for one party against its candidate listings."""

    def __init__(self, bounds: NameBounds, lefts: list[_Key], listings: list[Listing]):
        weights = bounds.settings.score_weights
        self.keys, self.lefts, self.listings = bounds.keys, lefts, listings
        self.weights = weights["token_set"], weights["token_sort"], weights["partial"]
        self.rows = {key.text: row for row, key in enumerate(lefts)}
        self.shortest = min((key.length for key in lefts), default=0)
        # Listing keys stay grouped by listing, so each listing reads a slice.
        self.offsets = list(accumulate((len(listing.texts) for listing in listings), initial=0))
        texts = [key.text for key in lefts]
        key_texts = list(chain.from_iterable(listing.texts for listing in listings))
        self.sorts = score_rows(texts, key_texts, fuzz.ratio)
        self.overlaps = score_rows(
            [key.characters for key in lefts],
            list(chain.from_iterable(listing.characters for listing in listings)),
            LCSseq.similarity,
            integer=True,
        )
        many = len(lefts) > 1
        self.sort_max = list(map(max, *self.sorts)) if many else self.sorts[0] if lefts else []
        self.overlap_max = (
            list(map(max, *self.overlaps)) if many else self.overlaps[0] if lefts else []
        )
        # token_set_ratio is read from the sorted-token ratio unless a listing
        # key shares a token with, or either side repeats one of, the party's keys.
        tokens = frozenset().union(*(key.tokens for key in lefts))
        repeated = any(key.repeated for key in lefts)
        self.shared: dict[int, int] = {}
        self.shared_spans: dict[int, tuple[int, int]] = {}
        for index, listing in enumerate(listings):
            if repeated or listing.repeated or not tokens.isdisjoint(listing.tokens):
                first = len(self.shared)
                for column, key in enumerate(listing.keys, self.offsets[index]):
                    if repeated or key.repeated or not tokens.isdisjoint(key.tokens):
                        self.shared[column] = len(self.shared)
                self.shared_spans[index] = first, len(self.shared)
        texts_shared = [key_texts[column] for column in self.shared]
        self.sets = score_rows(texts, texts_shared, fuzz.token_set_ratio)
        self.set_max = list(map(max, *self.sets)) if many else self.sets[0] if lefts else []

    def ceilings(self) -> list[float | None]:
        """A ceiling on every name comparison between the party and each listing.

        Each ratio takes its maximum over all of a listing's key pairings.
        """
        if not self.lefts:
            return [None] * len(self.listings)
        token_set, token_sort, partial = self.weights
        sort_max, overlap_max, set_max = self.sort_max, self.overlap_max, self.set_max
        spans, shortest = self.shared_spans, self.shortest
        result: list[float | None] = []
        for index, listing in enumerate(self.listings):
            start, end = self.offsets[index], self.offsets[index + 1]
            if end - start == 1:
                sort, overlap = sort_max[start], overlap_max[start]
            elif end > start:
                sort, overlap = max(sort_max[start:end]), max(overlap_max[start:end])
            else:
                result.append(None)
                continue
            set_ratio = sort
            span = spans.get(index)
            if span is not None and span[1] > span[0]:
                set_ratio = max(set_ratio, max(set_max[span[0] : span[1]]))
            result.append(
                token_set * set_ratio
                + token_sort * sort
                + partial * _partial_ceiling(overlap, min(shortest, listing.shortest))
                + SLACK
            )
        return result

    def _cell(self, row: int, column: int, shorter: int) -> float:
        sort = self.sorts[row][column]
        shared = self.shared.get(column)
        token_set, token_sort, partial = self.weights
        return (
            token_set * (sort if shared is None else self.sets[row][shared])
            + token_sort * sort
            + partial * _partial_ceiling(self.overlaps[row][column], shorter)
            + SLACK
        )

    def refined_ceiling(self, index: int) -> float | None:
        """The largest per-pairing ceiling for a listing; never above its ceilings() value."""
        listing, start = self.listings[index], self.offsets[index]
        return max(
            (
                self._cell(row, column, min(this.length, other.length))
                for row, this in enumerate(self.lefts)
                for column, other in enumerate(listing.keys, start)
            ),
            default=None,
        )

    def comparisons(self, index: int) -> Callable[[str, str], float]:
        """Ceilings on name_similarity(left, right) for the party and a listing."""
        listing, start = self.listings[index], self.offsets[index]
        keys, found = self.keys, {}

        def ceiling(left: str, right: str) -> float:
            this, other = keys[left], keys[right]
            pairing = this.text, other.text
            if pairing not in found:
                found[pairing] = self._cell(
                    self.rows[this.text],
                    start + listing.texts.index(other.text),
                    min(this.length, other.length),
                )
            return found[pairing]

        return ceiling

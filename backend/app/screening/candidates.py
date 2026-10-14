"""Token/phonetic blocking with a recall fallback over the complete name index."""

from collections import defaultdict
from typing import Any

from metaphone import doublemetaphone
from rapidfuzz import fuzz, process

from app.screening.score import names
from app.screening.settings import ScreeningSettings


class CandidateIndex:
    def __init__(self, entries: list[Any], settings: ScreeningSettings):
        self.settings = settings
        self.entries = {entry.id: entry for entry in entries}
        self.tokens: dict[str, set[str]] = defaultdict(set)
        self.phonetic: dict[str, set[str]] = defaultdict(set)
        self.full_index: dict[str, str] = {}
        self.choice_entries: dict[str, str] = {}
        self.stopwords = set(settings.candidate_stopwords)
        for entry in entries:
            for name in names(entry, settings, party=False):
                for variant in name.variants:
                    key = str(len(self.full_index))
                    self.full_index[key] = variant
                    self.choice_entries[key] = entry.id
                    tokens = variant.split()
                    for token in tokens:
                        if len(token) >= 3 and token not in self.stopwords:
                            self.tokens[token].add(entry.id)
                    if tokens:
                        for code in doublemetaphone(tokens[0]):
                            if code:
                                self.phonetic[code].add(entry.id)

    def candidates(self, party: Any) -> tuple[list[Any], bool]:
        ids: set[str] = set()
        party_names = names(party, self.settings, party=True)
        for name in party_names:
            for variant in name.variants:
                tokens = variant.split()
                for token in tokens:
                    if len(token) >= 3 and token not in self.stopwords:
                        ids.update(self.tokens.get(token, set()))
                if tokens:
                    for code in doublemetaphone(tokens[0]):
                        ids.update(self.phonetic.get(code, set()))
        fallback = len(ids) < 3
        if fallback:
            for name in party_names:
                for variant in name.variants:
                    # The fallback scans every name, not only already blocked candidates.
                    for _, _, key in process.extract(
                        variant,
                        self.full_index,
                        scorer=fuzz.WRatio,
                        score_cutoff=self.settings.candidate_min,
                        limit=20,
                    ):
                        ids.add(self.choice_entries[key])
        return [self.entries[key] for key in sorted(ids)], fallback

"""Offline pinned Laya token counts and explicitly estimated hosted request budgets."""

import gzip
import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from tokenizers import Tokenizer

from app.adjudicators.contracts import CLAIM, LAYA_TOKENIZER_SHA256


class InputBudgetError(ValueError):
    def __init__(self, metadata: dict):
        super().__init__("Essential identity evidence exceeds the configured input budget")
        self.metadata = metadata


def serialize_state(value: Any) -> str:
    # Match the publisher sequence serializer, including its default JSON spacing.
    return (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    )


@lru_cache(maxsize=1)
def laya_tokenizer() -> Tokenizer:
    path = Path(__file__).with_name("data") / "laya-multilingual-tokenizer.json.gz"
    raw = gzip.decompress(path.read_bytes())
    if hashlib.sha256(raw).hexdigest() != LAYA_TOKENIZER_SHA256:
        raise ValueError("Pinned local tokenizer integrity check failed")
    tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def local_tokens(value: Any) -> int:
    text = serialize_state(value).replace("<mask>", " ")
    return len(laya_tokenizer().encode(text, add_special_tokens=False).ids)


def local_state_room(claim: str = CLAIM, max_tokens: int = 1024) -> int:
    # Publisher's Noul head: CLS, question, SEP, two MASK+option spans, SEP.
    # For the fixed claim/options all spans are below the 256-token head ceiling.
    tokenizer = laya_tokenizer()

    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    head = 1 + count("noul question: " + claim.replace("<mask>", " ")) + 1
    for option in ("false: no, the statement does not hold", "true: yes, the statement holds"):
        head += 1 + min(48, count(" " + option))
    head += 1
    return max_tokens - head - 1


def prepare_state(state: dict, explanation: dict, provider: str) -> tuple[dict, dict]:
    prepared = deepcopy(state)
    local = provider == "laya"
    counter = local_tokens if local else lambda value: len(serialize_state(value).encode("utf-8"))
    room = local_state_room() if local else 32_000 - len(CLAIM.encode("utf-8")) - 128
    steps: list[str] = []
    before = counter(prepared)
    if before > room:
        prepared["listing"].pop("remarks", None)
        steps.append("remarks")
    if counter(prepared) > room:
        prepared["party"]["address_lines"] = []
        for address in prepared["listing"]["addresses"]:
            if isinstance(address, dict):
                address.pop("address", None)
                address.pop("address_lines", None)
        steps.append("address_lines")
    if counter(prepared) > room:
        best = explanation.get("best_aliases", {})
        for side, best_key in (("party", "party"), ("listing", "entry")):
            aliases = prepared[side]["alt_names"]
            chosen = best.get(best_key)
            prepared[side]["alt_names"] = [chosen] if chosen in aliases else []
        steps.append("extra_aliases")
    after = counter(prepared)
    metadata = {
        "method": "pinned_laya_tokenizer" if local else "utf8_bytes_conservative_estimate",
        "before": before,
        "after": after,
        "state_budget": room,
        "removed_fields": steps,
        "tokenizer_sha256": LAYA_TOKENIZER_SHA256 if local else None,
        "sequence_tokens": after + 1024 - room if local else None,
        "exact": local,
    }
    if after > room:
        raise InputBudgetError(metadata)
    return prepared, metadata

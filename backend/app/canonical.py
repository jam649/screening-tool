"""Shared canonical serialization, independent of connectors and screening."""

import hashlib
import json
from typing import Any

HASH_RULE_VERSION = 2


def canonical_hash(value: Any) -> str:
    data = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(data.encode()).hexdigest()

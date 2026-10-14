"""Writer allocation helpers are exact deepcopy substitutes and restore GC state."""

import gc
import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.audit import _safe
from app.screening.writer import copy_tree, paused_gc


def evidence_tree():
    return {
        "name": "Acme Manufacturing",
        "alt_names": ["Acme Mfg", "ACME"],
        "score": 91.5,
        "rank": 3,
        "eligible": True,
        "removed": None,
        "features": [("token_set", 0.9), ("country", [1, {"US": True}])],
        "nested": {"addresses": [{"city": "Springfield", "lines": ["1 Main"]}], "empty": {}},
        "custom": Decimal("1.5"),
        "observed_at": datetime(2026, 10, 14, tzinfo=UTC),
    }


def shape(value):
    if isinstance(value, dict):
        return (type(value), [(key, shape(item)) for key, item in value.items()])
    if isinstance(value, (list, tuple)):
        return (type(value), [shape(item) for item in value])
    return (type(value), value)


def containers(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from containers(item)
    elif isinstance(value, (list, tuple)):
        yield value
        for item in value:
            yield from containers(item)


def test_copy_tree_matches_deepcopy_types_order_and_independence():
    original = evidence_tree()
    copied = copy_tree(original)
    assert shape(copied) == shape(deepcopy(original)) == shape(original)
    assert json.dumps(copied, default=str) == json.dumps(original, default=str)
    copied_ids = {id(item) for item in containers(copied) if not isinstance(item, tuple)}
    assert not copied_ids & {id(item) for item in containers(original)}
    copied["nested"]["addresses"][0]["lines"].append("2 Main")
    copied["features"][1][1][1]["US"] = False
    assert original == evidence_tree()


def test_safe_snapshot_conversion_is_unchanged():
    assert _safe(evidence_tree()) == {
        **evidence_tree(),
        "features": [["token_set", 0.9], ["country", [1, {"US": True}]]],
        "observed_at": "2026-10-14T00:00:00+00:00",
    }


def test_paused_gc_restores_previous_state_after_failure_and_nesting():
    @paused_gc
    def nested(fail):
        assert not gc.isenabled()
        if fail:
            raise RuntimeError("synthetic failure")
        return outer(False)

    @paused_gc
    def outer(fail):
        assert not gc.isenabled()
        return "done" if not fail else nested(True)

    assert gc.isenabled()
    assert nested(False) == "done"
    assert gc.isenabled()
    with pytest.raises(RuntimeError):
        outer(True)
    assert gc.isenabled()
    gc.disable()
    try:
        assert outer(False) == "done"
        assert not gc.isenabled()
    finally:
        gc.enable()

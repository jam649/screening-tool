"""Writer allocation helpers are exact deepcopy substitutes."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.audit import _safe
from app.screening.engine import WRITER_PAGE_CACHE_KIB, _writer_page_cache
from app.screening.writer import copy_tree


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


def test_writer_page_cache_is_raised_only_while_the_writer_runs():
    with Session(create_engine("sqlite://")) as db:

        def cache_size():
            return db.connection().exec_driver_sql("PRAGMA cache_size").scalar()

        before = cache_size()
        with _writer_page_cache(db):
            assert cache_size() == -WRITER_PAGE_CACHE_KIB
        assert cache_size() == before
        with pytest.raises(RuntimeError), _writer_page_cache(db):
            raise RuntimeError("synthetic writer failure")
        assert cache_size() == before

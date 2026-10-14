"""Allocation helpers for the atomic screening writer; they never change written evidence."""

from copy import deepcopy
from typing import Any

_ATOMS = frozenset({str, int, float, bool, type(None)})


def copy_tree(value: Any) -> Any:
    """deepcopy for JSON-like evidence trees without its per-node memo overhead.

    Exact dict/list/tuple types and key order are preserved and atoms are shared
    exactly as deepcopy shares them. Any other type falls back to deepcopy. Repeated
    containers are copied separately: every tree is stored as JSON, which has no
    aliasing either, and nothing mutates a nested container in place.
    """
    kind = type(value)
    if kind in _ATOMS:
        return value
    if kind is dict:
        return {
            key if type(key) in _ATOMS else deepcopy(key): item
            if type(item) in _ATOMS
            else copy_tree(item)
            for key, item in value.items()
        }
    if kind is list:
        return [item if type(item) in _ATOMS else copy_tree(item) for item in value]
    if kind is tuple:
        return tuple(item if type(item) in _ATOMS else copy_tree(item) for item in value)
    return deepcopy(value)

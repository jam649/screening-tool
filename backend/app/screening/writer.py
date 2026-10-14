"""Allocation helpers for the atomic screening writer; they never change written evidence."""

import gc
import threading
from collections.abc import Callable
from copy import deepcopy
from functools import wraps
from typing import Any

_ATOMS = frozenset({str, int, float, bool, type(None)})
_pause_lock = threading.Lock()
_paused = 0
_was_enabled = False


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


def paused_gc[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Pause cyclic GC while the writer runs; reference counting still frees evidence.

    The writer allocates acyclic per-hit evidence that refcounting releases after each
    flush, yet every full collection rescans the entire prepared screening, which made
    collection a large share of writer time. The previous state is restored afterwards,
    also on failure; nested or concurrent writers restore it only when the last exits.
    """

    @wraps(function)
    def paused(*args: P.args, **kwargs: P.kwargs) -> R:
        global _paused, _was_enabled
        with _pause_lock:
            if not _paused:
                _was_enabled = gc.isenabled()
                gc.disable()
            _paused += 1
        try:
            return function(*args, **kwargs)
        finally:
            with _pause_lock:
                _paused -= 1
                if not _paused and _was_enabled:
                    gc.enable()

    return paused

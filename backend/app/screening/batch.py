"""Batched rapidfuzz comparisons without a numpy dependency.

rapidfuzz's public ``process.cdist`` only wraps the compiled implementation's
result matrix in numpy. That matrix exposes the buffer protocol itself, so
reading it through ``memoryview`` yields exactly the values the scalar scorers
return (verified bitwise on the matching fixtures) while the comparisons run
in C++ with the GIL released. No score_cutoff is passed: rapidfuzz's batch
cutoff handling differs from the scalar scorers near the cutoff. rapidfuzz is
pinned exactly; the module tests fail loudly if this internal interface ever
changes.
"""

from collections.abc import Callable, Sequence
from typing import Any

from rapidfuzz.process_cpp_impl import FLOAT64, INT64, cdist

# Absorbs float rounding between mathematically equal ratio formulations
# (observed differences are below 1e-12 on a 0-100 scale).
SLACK = 1e-6


def score_rows(
    queries: Sequence[str],
    choices: Sequence[str],
    scorer: Callable[..., Any],
    *,
    integer: bool = False,
    workers: int = 1,
) -> list[memoryview]:
    """One row of ``scorer(query, choice)`` results per query, in choice order."""
    if not queries or not choices:
        return [memoryview(b"") for _ in queries]
    matrix = memoryview(
        cdist(
            queries,
            choices,
            scorer=scorer,
            dtype=INT64 if integer else FLOAT64,
            workers=workers,
        )
    )
    # The matrix is C-contiguous; flatten it so rows are cheap 1-D slices.
    # Integer results here are non-negative LCS lengths, so "Q" reads them exactly.
    flat: memoryview[Any] = matrix.cast("B").cast("Q" if integer else "d")
    width = len(choices)
    return [flat[row * width : (row + 1) * width] for row in range(len(queries))]

"""Batched rapidfuzz comparisons without a numpy dependency.

rapidfuzz's public ``process.cdist`` only wraps the compiled implementation's
result matrix in numpy. That matrix exposes the buffer protocol itself, so it
is read through ``memoryview`` while the comparisons run in C++ with the GIL
released. Without score_cutoff the values are bitwise those of the scalar
scorers (verified on the matching fixtures). With score_cutoff they follow the
batch semantics shared with ``process.extract_iter``, not the scalar scorers':
a small internal tolerance may keep a score slightly below the cutoff, and
rejected comparisons read as 0. rapidfuzz is pinned exactly; the module tests
fail loudly if this internal interface or those semantics change.
"""

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from rapidfuzz.process_cpp_impl import FLOAT64, INT64, cdist


def _available_cpus() -> int:
    # os.cpu_count() reports host CPUs inside containers; a cgroup v2 quota
    # (the matching gate runs with --cpus 2) or CPU affinity can be lower.
    available = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        available = min(available, len(os.sched_getaffinity(0)))
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            available = min(available, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return available


# Absorbs float rounding between mathematically equal ratio formulations
# (observed differences are below 1e-12 on a 0-100 scale).
SLACK = 1e-6

# Screening runs inside the single application process, so it never takes
# more than four threads even on large hosts. Results never depend on this.
WORKERS = min(4, _available_cpus())


def score_rows(
    queries: Sequence[str],
    choices: Sequence[str],
    scorer: Callable[..., Any],
    *,
    integer: bool = False,
    score_cutoff: float | None = None,
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
            score_cutoff=score_cutoff,
            dtype=INT64 if integer else FLOAT64,
            workers=workers,
        )
    )
    # The matrix is C-contiguous; flatten it so rows are cheap 1-D slices.
    flat: memoryview[Any] = matrix.cast("B").cast("Q" if integer else "d")
    width = len(choices)
    return [flat[row * width : (row + 1) * width] for row in range(len(queries))]

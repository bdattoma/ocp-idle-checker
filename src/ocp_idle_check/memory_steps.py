"""The memory movement rule: a level shift in absolute points between windows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import MEMORY_VARIANCE_WINDOWS, STEP_SECONDS
from .nodes import _round2
from .prometheus import _series_node


@dataclass(frozen=True)
class MemoryStepStats:
    """Result of the memory movement evaluation for one node's windows.

    The rise fields describe the largest increase between window averages
    that fits inside the sliding group of `span_windows` consecutive
    windows - the allocation signal that votes.  The drop fields describe
    the largest decrease - drains, reclaim, pod exits - which is debug
    only: a release is not activity.  `max_rise_by_span` recalculates the
    rise at every group size up to one window wider than the span, so a
    single run reports what each span would have caught.
    """

    node: str
    points: int
    max_rise: float  # largest window-to-window increase inside the span
    rise_at: float | None  # timestamp of the later point of that pair
    rise_from: float | None  # the level the rise started from
    max_drop: float  # largest decrease inside the span (never votes)
    drop_at: float | None  # timestamp of the later point of that pair
    max_rise_by_span: dict[int, float]  # max rise within 2..span+1 windows
    rise_exceeded: bool  # max_rise cleared the points threshold

    @property
    def active(self) -> bool:
        return self.rise_exceeded


def _iso_z(ts: float | None) -> str | None:
    """A unix timestamp as "2026-10-07T16:18:45Z"; None passes through."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluate_memory_steps(
    points: list[tuple[float, float]],
    *,
    points_threshold: float,
    span_windows: int = MEMORY_VARIANCE_WINDOWS,
    step_s: int = STEP_SECONDS,
    node: str = "",
) -> MemoryStepStats:
    """The memory movement rule on one node's (timestamp, value) windows.

    Compares every pair of window averages whose timestamps fit inside a
    sliding group of `span_windows` consecutive windows and tracks the
    largest increase: a jump landing mid-window splits across two window
    averages, each diluted toward its own side, but a group wide enough to
    hold one fully-before and one fully-after window reads it at full
    strength wherever it falls - with the default 4 windows that pair is
    always two slots away.  Increases vote; the largest decrease is
    tracked for debug only.  The pair test is on timestamps, not index
    distance, so a series with a data hole simply has no pair across the
    hole rather than a fabricated step.  Pairs one group wider than the
    span feed `max_rise_by_span` for calibration.
    """
    span_windows = max(2, span_windows)
    # The widest pair the calibration table admits; the rule itself stops
    # one window narrower.
    pair_limit = span_windows * step_s
    per_span: dict[int, float] = {}  # max rise at each exact group size
    max_rise = 0.0
    rise_at: float | None = None
    rise_from: float | None = None
    max_drop = 0.0
    drop_at: float | None = None
    for i, (ti, vi) in enumerate(points):
        for j in range(i + 1, len(points)):
            tj, vj = points[j]
            gap = tj - ti
            if gap > pair_limit:
                break  # timestamps ascend, so every later j is farther still
            delta = vj - vi
            # The smallest group of consecutive windows that holds this
            # pair: one window per step of gap, plus the starting one.
            span = max(2, int(gap / step_s) + 1)
            if delta > per_span.get(span, 0.0):
                per_span[span] = delta
            if span > span_windows:
                continue  # calibration only, past the configured span
            if delta > max_rise:
                max_rise, rise_at, rise_from = delta, tj, vi
            if delta < max_drop:
                max_drop, drop_at = delta, tj
    rise_by_span: dict[int, float] = {}
    running = 0.0
    for n in range(2, span_windows + 2):
        running = max(running, per_span.get(n, 0.0))
        rise_by_span[n] = running
    return MemoryStepStats(
        node=node,
        points=len(points),
        max_rise=max_rise,
        rise_at=rise_at,
        rise_from=rise_from,
        max_drop=max_drop,
        drop_at=drop_at,
        max_rise_by_span=rise_by_span,
        rise_exceeded=max_rise > points_threshold,
    )


def evaluate_memory_steps_by_node(
    series: list[dict[str, Any]] | None,
    *,
    points_threshold: float,
    span_windows: int = MEMORY_VARIANCE_WINDOWS,
    live_nodes: set[str] | None = None,
    step_s: int = STEP_SECONDS,
) -> list[MemoryStepStats]:
    """Evaluate the movement rule once per node, on live nodes only.

    Same grouping as the CPU variance rule - series keyed by the `node`
    label else `instance` sans port, nodes missing from `live_nodes`
    dropped, because a deleted node's refill is history rather than
    activity - but the samples stay (timestamp, value) pairs sorted by
    timestamp, because the rule compares levels across time instead of
    pooling values.  Returns stats sorted by node name for deterministic
    output.
    """
    if not series:
        return []
    grouped: dict[str, list[tuple[float, float]]] = {}
    for s in series:
        node = _series_node(s.get("metric", {}))
        if live_nodes is not None and node not in live_nodes:
            continue
        pairs = grouped.setdefault(node, [])
        for ts, value in s.get("values", []):
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                pairs.append((float(ts), v))
    return [
        evaluate_memory_steps(
            sorted(pairs),
            points_threshold=points_threshold,
            span_windows=span_windows,
            step_s=step_s,
            node=node,
        )
        for node, pairs in sorted(grouped.items())
        if pairs
    ]


def summarize_memory_steps(per_node: list[MemoryStepStats]) -> MemoryStepStats:
    """Cluster summary over per-node evaluations: the node with the largest
    rise drives the summary (its rise is the cluster's max by construction),
    the node with the largest drop supplies the debug drop fields, and
    points pool across nodes."""
    if not per_node:
        return MemoryStepStats("", 0, 0.0, None, None, 0.0, None, {}, False)
    driver = max(per_node, key=lambda s: s.max_rise)
    drop_driver = min(per_node, key=lambda s: s.max_drop)
    return MemoryStepStats(
        node=driver.node,
        points=sum(s.points for s in per_node),
        max_rise=driver.max_rise,
        rise_at=driver.rise_at,
        rise_from=driver.rise_from,
        max_drop=drop_driver.max_drop,
        drop_at=drop_driver.drop_at,
        max_rise_by_span=driver.max_rise_by_span,
        rise_exceeded=any(s.rise_exceeded for s in per_node),
    )


def _memory_steps_entry(
    stats: MemoryStepStats, per_node: list[MemoryStepStats] | None = None
) -> dict[str, Any]:
    """JSON detail for a movement evaluation (additive to result/value)."""
    entry: dict[str, Any] = {
        "max_rise": _round2(stats.max_rise),
        "rise_at": _iso_z(stats.rise_at),
        "rise_from": _round2(stats.rise_from),
        "max_drop": _round2(stats.max_drop),
        "drop_at": _iso_z(stats.drop_at),
        "max_rise_by_span": {str(n): _round2(v) for n, v in stats.max_rise_by_span.items()},
        "points": stats.points,
        "rise_exceeded": stats.rise_exceeded,
    }
    if stats.node:
        entry["node"] = stats.node
    if per_node:
        entry["per_node"] = [_memory_steps_entry(s) for s in per_node]
    return entry

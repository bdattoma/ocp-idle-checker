"""The variance rule: one power-law burst detector shared by CPU, GPU, and API."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

from .config import (
    VARIANCE_ANCHOR_BASELINE,
    VARIANCE_ANCHOR_MULTIPLE,
    VARIANCE_DEFAULT_BASELINE_FULL,
)
from .nodes import _round2
from .prometheus import _series_node, _to_floats


@dataclass(frozen=True)
class VarianceStats:
    """Result of the peak + variance evaluation."""

    points: int
    peak: float
    baseline: float  # second-lowest window in trough mode, else median
    ratio: float | None  # peak / baseline; None when the baseline is zero
    required_ratio: (
        float | None
    )  # scaled multiple the peak must clear; None when the baseline is zero
    required_peak: (
        float | None
    )  # absolute peak the variance rule demands; None only when there is no data
    peak_exceeded: bool
    variance_exceeded: bool
    node: str = ""  # set by per-node evaluation; empty when not node-scoped
    # The absolute peak threshold actually applied to this evaluation
    # (per-node scaled for CPU); None when the rule has no absolute branch.
    peak_threshold: float | None = None

    @property
    def active(self) -> bool:
        return self.peak_exceeded or self.variance_exceeded


def _baseline(points: list[float], mode: str) -> float:
    """The level the peak is measured against.

    Trough mode (CPU, GPU): the second-lowest window, not the absolute
    minimum - one dropped sample or monitoring gap must not become the
    workload's floor.  Median mode (API): the classic median.
    """
    if mode != "trough":
        return statistics.median(points)
    if len(points) < 2:
        return min(points)
    return sorted(points)[1]


def variance_curve_exponent(baseline_full: float, ratio_threshold: float) -> float:
    """Power-law exponent placing the curve at 10x (VARIANCE_ANCHOR_MULTIPLE)
    at a baseline of 0.2 (VARIANCE_ANCHOR_BASELINE) and ratio_threshold at
    baseline_full."""
    return math.log(VARIANCE_ANCHOR_MULTIPLE / ratio_threshold) / math.log(
        baseline_full / VARIANCE_ANCHOR_BASELINE
    )


def variance_required_peak(
    baseline: float,
    ratio_threshold: float,
    floor: float = 0.0,
    baseline_full: float = VARIANCE_DEFAULT_BASELINE_FULL,
) -> float:
    """Absolute window peak the variance rule demands at a baseline.

    One law shared by CPU, GPU, and the API server, in each criterion's
    native unit (percent for CPU/GPU, req/s for the API server): the
    required multiple is 10x (VARIANCE_ANCHOR_MULTIPLE) at a baseline of
    0.2 (VARIANCE_ANCHOR_BASELINE) and falls to ratio_threshold at
    `baseline_full` - the criterion's absolute threshold divided by the
    ratio (25% CPU, ~33.3% GPU, ~83.3 req/s API at the default 1.2) -
    following a power law in between, so quiet baselines need
    proportionally bigger bursts before they count as bursty.  The
    handoff to the absolute branch (window peak over the peak threshold,
    API windowed average over the request threshold) is exact by
    construction: at baseline_full the required peak is
    ratio x threshold/ratio = the threshold itself.  Past baseline_full
    the power law keeps easing the multiple below the ratio; the
    absolute branch owns those peaks anyway.  `floor` is the minimum
    required peak and stands in for the whole curve on a zero baseline:
    the CPU (1%) and GPU (5%) rules pass one so DCGM's 1-4% idle noise
    tail over a zero median does not read as an unbounded burst, while
    the API rule passes none - a zero baseline there demands only a
    positive peak.
    """
    if baseline <= 0:
        return floor
    multiple = ratio_threshold * (baseline_full / baseline) ** variance_curve_exponent(
        baseline_full, ratio_threshold
    )
    return max(floor, multiple * baseline)


def evaluate_variance(
    points: list[float],
    *,
    peak_threshold: float | None = None,
    ratio_threshold: float,
    floor: float = 0.0,
    baseline_full: float = VARIANCE_DEFAULT_BASELINE_FULL,
    node: str = "",
    baseline_mode: str = "median",
) -> VarianceStats:
    """The variance rule: ACTIVE when any window exceeded `peak_threshold`
    (when given), or when the peak clears the baseline-scaled required peak
    (see variance_required_peak; `floor` is that requirement's minimum).
    `baseline_mode` picks what the peak is measured against - "trough" for
    utilization series (CPU, GPU), "median" for the API server."""
    if not points:
        return VarianceStats(0, 0.0, 0.0, None, None, None, False, False)
    peak = max(points)
    baseline = _baseline(points, baseline_mode)
    required_peak = variance_required_peak(baseline, ratio_threshold, floor, baseline_full)
    ratio = peak / baseline if baseline > 0 else None
    required_ratio = required_peak / baseline if baseline > 0 else None
    peak_exceeded = peak_threshold is not None and peak > peak_threshold
    variance_exceeded = peak > required_peak
    return VarianceStats(
        len(points),
        peak,
        baseline,
        ratio,
        required_ratio,
        required_peak,
        peak_exceeded,
        variance_exceeded,
        node,
        peak_threshold,
    )


def evaluate_variance_by_node(
    series: list[dict[str, Any]] | None,
    *,
    peak_threshold: float,
    ratio_threshold: float,
    floor: float = 0.0,
    baseline_full: float = VARIANCE_DEFAULT_BASELINE_FULL,
    live_nodes: set[str] | None = None,
    baseline_mode: str = "median",
    node_peak_thresholds: dict[str, float] | None = None,
    variance_only_nodes: set[str] | None = None,
) -> list[VarianceStats]:
    """Evaluate the variance rule once per node, each against its own
    baseline (trough or median per `baseline_mode`).

    Grouping matters because a pooled baseline is dragged by cross-node
    heterogeneity: a steady busy node pooled with a steady quiet sibling
    lands the baseline in the valley between the two modes, and the busy
    node's peak over that valley reads as a burst.  Judged per node, both
    are steady.  `live_nodes`, when given, drops series from nodes that no
    longer exist (their history is real but says nothing about the cluster
    as it stands); the GPU criterion deliberately omits it, since a deleted
    node's samples are still activity.  `node_peak_thresholds`, when given,
    scales each node's absolute peak threshold (CPU: large machines rarely
    max out their cores, so the percent bar eases with core count) and
    moves the curve's handoff point with it.  `variance_only_nodes`, when
    given, strips the absolute peak branch for those nodes - only the
    baseline-scaled burst rule can accuse them (CPU: a cluster's
    control-plane nodes when it has workers, whose steady load is platform
    overhead rather than workload activity).  Series with neither a `node`
    nor an `instance` label share the empty node name and pool as one
    group.  Returns stats sorted by node name for deterministic output.
    """
    if not series:
        return []
    grouped: dict[str, list[float]] = {}
    for s in series:
        node = _series_node(s.get("metric", {}))
        if live_nodes is not None and node not in live_nodes:
            continue
        grouped.setdefault(node, []).extend(_to_floats([v for _, v in s.get("values", [])]))
    return [
        evaluate_variance(
            points,
            peak_threshold=(
                None
                if variance_only_nodes and node in variance_only_nodes
                else (
                    node_peak_thresholds.get(node, peak_threshold)
                    if node_peak_thresholds
                    else peak_threshold
                )
            ),
            ratio_threshold=ratio_threshold,
            floor=floor,
            baseline_full=(
                node_peak_thresholds.get(node, peak_threshold) / ratio_threshold
                if node_peak_thresholds
                else baseline_full
            ),
            node=node,
            baseline_mode=baseline_mode,
        )
        for node, points in sorted(grouped.items())
        if points
    ]


def _variance_severity(stats: VarianceStats) -> tuple[float, float]:
    """Ranking key for which node drives a summarized evaluation: how far
    the peak clears that node's required peak (above 1 means the rule
    fired), then the raw peak."""
    required = stats.required_peak or 0.0
    if required <= 0:
        return (0.0, stats.peak)
    return (stats.peak / required, stats.peak)


def summarize_variance(per_node: list[VarianceStats]) -> VarianceStats:
    """Cluster summary over per-node evaluations.

    peak and peak_exceeded are pooled (any node can trip the peak rule);
    baseline, ratio, required_ratio, required_peak, and node come from the
    node that comes closest to (or furthest past) its own required peak,
    so the summary numbers describe the node that drove the variance verdict.
    """
    if not per_node:
        return VarianceStats(0, 0.0, 0.0, None, None, None, False, False)
    driver = max(per_node, key=_variance_severity)
    return VarianceStats(
        points=sum(s.points for s in per_node),
        peak=max(s.peak for s in per_node),
        baseline=driver.baseline,
        ratio=driver.ratio,
        required_ratio=driver.required_ratio,
        required_peak=driver.required_peak,
        peak_exceeded=any(s.peak_exceeded for s in per_node),
        variance_exceeded=any(s.variance_exceeded for s in per_node),
        node=driver.node,
    )


def _variance_numbers(stats: VarianceStats, prefix: str = "") -> dict[str, Any]:
    """The comparable peak/baseline/ratio fields of an evaluation, rounded
    for the report; `prefix` namespaces them (the API criterion's
    variance_* fields)."""
    return {
        f"{prefix}peak": _round2(stats.peak),
        f"{prefix}baseline": _round2(stats.baseline),
        f"{prefix}ratio": _round2(stats.ratio),
        f"{prefix}required_ratio": _round2(stats.required_ratio),
        f"{prefix}required_peak": _round2(stats.required_peak),
    }


def _variance_entry(
    stats: VarianceStats, per_node: list[VarianceStats] | None = None
) -> dict[str, Any]:
    """JSON detail for a variance evaluation (additive to result/value)."""
    entry: dict[str, Any] = {
        **_variance_numbers(stats),
        "points": stats.points,
        "peak_exceeded": stats.peak_exceeded,
        "variance_exceeded": stats.variance_exceeded,
    }
    if stats.node:
        entry["node"] = stats.node
    if per_node:
        entry["per_node"] = [_variance_entry(s) for s in per_node]
    return entry


def _threshold_ratio(value: float, threshold: float) -> float:
    """`value` as a fraction of the level that fires it (>= 1 means it fired,
    exact-boundary strictness aside).  A zero threshold is a degenerate
    config no finite ratio expresses; any positive value reads as
    unboundedly over it."""
    if threshold > 0:
        return value / threshold
    return math.inf if value > 0 else 0.0


def _variance_threshold_ratio(stats: VarianceStats, peak_threshold: float | None) -> float | None:
    """A variance evaluation's threshold ratio: the peak as a fraction of the
    level that fires it - the absolute peak threshold (when the rule has one;
    the per-node scaled one when the stats carry it) and the baseline-scaled
    required peak, whichever is closer.  None when there is no data.  A
    floorless rule on a zero baseline (required peak 0) fires on any positive
    peak, which no finite ratio expresses."""
    if stats.points == 0:
        return None
    heat: float = 0.0
    applied = stats.peak_threshold if stats.peak_threshold is not None else peak_threshold
    if applied is not None:
        heat = _threshold_ratio(stats.peak, applied)
    required = stats.required_peak
    if required is None:
        return heat
    if required <= 0:
        # A floorless rule on a zero baseline demands only a positive peak;
        # no finite ratio expresses firing on "anything above zero".
        return math.inf if stats.peak > 0 else heat
    return max(heat, stats.peak / required)

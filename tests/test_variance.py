"""Table-driven tests for the variance evaluator."""

from __future__ import annotations

import pytest

import ocp_idle_check as oic
from helpers import base_config, make_range_series


@pytest.mark.parametrize(
    ("points", "expected_active", "why"),
    [
        # Peak branch: any 15-min window above the threshold.
        ([5.0, 5.0, 45.0], True, "peak above threshold"),
        ([5.0] * 10 + [30.0], True, "variance: median 5, peak 30 clears the required 16.1"),
        # The motivating pattern: a node that idles low and loads under use.
        # At a 1% median the requirement is ~5.7x (a required peak of 5.7),
        # at 2% ~4.5x (a required peak of 8.9).
        ([1.0, 1.0, 7.0], True, "median 1, peak 7 clears the required 5.7"),
        ([1.0, 1.0, 5.5], False, "peak 5.5 just below the required 5.7 at a median of 1"),
        ([2.0, 2.0, 10.0], True, "median 2, peak 10 clears the required 8.9"),
        ([2.0, 2.0, 8.5], False, "peak 8.5 just below the required 8.9 at a median of 2"),
        ([20.0] * 10, False, "steady moderate load: requirement of 39.6"),
        ([25.0] * 10, False, "steady load below the peak threshold"),
        # With no floor passed (the API rule's configuration), a zero
        # baseline fires on any positive peak; the CPU/GPU rules pass one.
        ([0.0, 0.0, 10.0], True, "zero median, no floor: any positive peak fires"),
        ([0.0, 0.0, 0.5], True, "zero median, no floor: however small the peak"),
        ([0.0] * 5, False, "all-zero data"),
        ([], False, "no data"),
        # Boundaries: strictly greater-than on both branches.
        ([30.0] * 4, False, "peak exactly at the threshold is not exceeded"),
        # At a 15% median the required peak is 32.9, above the peak
        # threshold, so neither branch fires on a 30% peak.
        ([15.0, 15.0, 30.0], False, "required peak of 32.9 is not reached"),
    ],
)
def test_evaluate_variance(points, expected_active, why):
    stats = oic.evaluate_variance(
        points,
        peak_threshold=30.0,
        ratio_threshold=1.2,
    )
    assert stats.active is expected_active, why


def test_required_peak_scales_with_baseline():
    """The required peak follows one power law: 10x the baseline at a
    baseline of 0.2, falling to 1.2x at the handoff (baseline_full, the
    absolute threshold divided by the ratio).  The floor defaults to zero
    (the API rule); the CPU/GPU rules pass an absolute floor that stands in
    for the whole curve on a zero baseline."""
    points = [10.0] * 6  # ratio 1: never fires, but the requirement is computed

    def required(baseline):
        return oic.evaluate_variance(
            [baseline] * len(points),
            peak_threshold=30.0,
            ratio_threshold=1.2,
        ).required_peak

    assert required(0.2) == pytest.approx(2.0)  # 10x at a baseline of 0.2
    assert required(1.0) == pytest.approx(5.68, abs=0.01)  # ~5.7x
    assert required(5.0) == pytest.approx(16.13, abs=0.01)  # ~3.2x on quiet baselines
    assert required(25.0) == pytest.approx(45.8, abs=0.01)  # ~1.8x in between
    assert required(100.0 / 1.2) == pytest.approx(100.0)  # 1.2x at the handoff
    assert required(0.0) == pytest.approx(0.0)  # zero baseline, floorless
    # The floor is the minimum requirement and the whole curve on a zero
    # baseline; the curve stands above it once the baseline is big enough.
    assert oic.variance_required_peak(0.0, 1.2, floor=5.0) == pytest.approx(5.0)
    assert oic.variance_required_peak(0.1, 1.2, floor=5.0) == pytest.approx(5.0)  # curve: 1.3
    assert oic.variance_required_peak(1.0, 1.2, floor=5.0) == pytest.approx(
        5.68, abs=0.01
    )  # curve: 5.7


def test_required_multiple_keeps_easing_past_the_handoff():
    """Past the handoff the power law keeps relaxing the required multiple
    below the ratio, so the required peak keeps rising with the baseline
    but more slowly - at the default handoff a flat 400 req/s series
    demands only ~277 and fires.  The absolute branch owns those peaks
    anyway (the windowed average reads ACTIVE), and the criteria that
    allow a raised absolute threshold scale the handoff with it, so the
    divergence never decides a real verdict."""
    # Baseline 400, default handoff (100/1.2): the law demands ~277 req/s,
    # so the flat 400 series fires the variance branch.
    flat = oic.evaluate_variance([400.0] * 10, ratio_threshold=1.2)
    assert flat.required_peak == pytest.approx(276.56, abs=0.01)
    assert flat.required_ratio == pytest.approx(0.69, abs=0.01)
    assert flat.variance_exceeded
    # At the handoff itself the requirement is exactly the absolute
    # threshold, and beyond it the multiple keeps easing below the ratio.
    assert oic.variance_required_peak(100.0 / 1.2, 1.2) == pytest.approx(100.0)
    assert oic.variance_required_peak(100.0, 1.2) == pytest.approx(112.55, abs=0.01)


def test_variance_handoff_is_exact_per_criterion():
    # The handoff point is the absolute threshold divided by the ratio, and
    # there the required peak is exactly the threshold - the variance
    # branch hands the verdict to the absolute branch precisely there.
    # CPU 25% -> 30% (threshold 30), GPU 33.3% -> 40% (threshold 40), API
    # 83.3 req/s -> 100 (threshold 100).
    assert oic.variance_required_peak(25.0, 1.2, baseline_full=25.0) == pytest.approx(30.0)
    assert oic.variance_required_peak(40.0 / 1.2, 1.2, floor=5.0, baseline_full=40.0 / 1.2) == (
        pytest.approx(40.0)
    )
    assert oic.variance_required_peak(100.0 / 1.2, 1.2, baseline_full=100.0 / 1.2) == (
        pytest.approx(100.0)
    )
    # The anchor survives the per-criterion exponent: 10x at a baseline of 0.2.
    assert oic.variance_required_peak(0.2, 1.2, baseline_full=25.0) == pytest.approx(2.0)
    assert oic.variance_required_peak(0.2, 1.2, baseline_full=40.0 / 1.2) == pytest.approx(2.0)


def test_evaluate_variance_scaled_requirement_is_strict():
    # Median 5: the requirement is a peak of 16.1.  A 16% peak stays idle,
    # a 17% peak fires, and both sit below the peak threshold so only the
    # variance branch is in play.
    below = oic.evaluate_variance([5.0] * 4 + [16.0], peak_threshold=30.0, ratio_threshold=1.2)
    assert not below.variance_exceeded
    assert below.required_peak == pytest.approx(16.13, abs=0.01)
    assert below.required_ratio == pytest.approx(3.23, abs=0.01)  # 16.13 / 5
    above = oic.evaluate_variance([5.0] * 4 + [17.0], peak_threshold=30.0, ratio_threshold=1.2)
    assert above.variance_exceeded


def test_evaluate_variance_reports_stats():
    stats = oic.evaluate_variance(
        [4.0, 5.0, 6.0, 50.0],
        peak_threshold=30.0,
        ratio_threshold=1.2,
    )
    assert stats.points == 4
    assert stats.peak == pytest.approx(50.0)
    # Median of an even-count list: mean of the middle two.
    assert stats.baseline == pytest.approx(5.5)
    assert stats.ratio == pytest.approx(50.0 / 5.5)
    assert stats.variance_exceeded
    assert stats.peak_exceeded


def test_evaluate_variance_zero_baseline_demands_the_floor():
    # A zero baseline makes the floor the whole requirement (the GPU rule's
    # configuration): 22% of DCGM-style noise over a zero median clears the
    # 5% floor and fires, a 3.3% noise tail does not, and
    # all-zero data never does.
    stats = oic.evaluate_variance(
        [0.0, 0.0, 22.0],
        peak_threshold=40.0,
        ratio_threshold=1.2,
        floor=5.0,
    )
    assert stats.ratio is None
    assert stats.required_ratio is None
    assert stats.required_peak == pytest.approx(5.0)
    assert stats.variance_exceeded
    noise = oic.evaluate_variance(
        [0.0, 0.0, 3.3],
        peak_threshold=40.0,
        ratio_threshold=1.2,
        floor=5.0,
    )
    assert not noise.variance_exceeded
    assert noise.required_peak == pytest.approx(5.0)
    zeros = oic.evaluate_variance(
        [0.0, 0.0, 0.0],
        peak_threshold=40.0,
        ratio_threshold=1.2,
        floor=5.0,
    )
    assert not zeros.variance_exceeded


def test_api_variance_zero_baseline_has_no_floor():
    # The API rule passes no floor: a zero median demands only a positive
    # request rate, so 40 req/s over a zero median fires.
    stats = oic.evaluate_variance([0.0, 0.0, 40.0], ratio_threshold=1.2)
    assert stats.peak_exceeded is False  # the API rule has no peak branch
    assert stats.required_peak == pytest.approx(0.0)
    assert stats.variance_exceeded


def test_api_variance_uses_the_shared_curve():
    # The API rule shares the CPU/GPU law in req/s: 1.2x at an 83.3 req/s
    # median (the handoff), ~1.8x at 25 req/s - a required peak of
    # 45.8 req/s.
    below = oic.evaluate_variance([25.0] * 6 + [45.0], ratio_threshold=1.2)
    assert below.peak_exceeded is False  # the API rule has no peak branch
    assert below.required_peak == pytest.approx(45.8, abs=0.01)
    assert below.required_ratio == pytest.approx(1.83, abs=0.01)
    assert not below.variance_exceeded
    above = oic.evaluate_variance([25.0] * 6 + [46.0], ratio_threshold=1.2)
    assert above.variance_exceeded


def test_evaluate_variance_by_node_judges_each_node_alone():
    """A steady busy node pooled with a steady quiet
    sibling.  Pooled, the median lands between the modes and the busy node's
    peak over it reads as a 2x+ burst; per node, both are steady."""
    series = [
        make_range_series("busy-node", ["17"] * 10 + ["19.5"]),
        make_range_series("quiet-node", ["2"] * 10 + ["2.4"]),
    ]
    per_node = oic.evaluate_variance_by_node(series, peak_threshold=30.0, ratio_threshold=1.2)
    assert [s.node for s in per_node] == ["busy-node", "quiet-node"]
    assert all(not s.variance_exceeded for s in per_node)

    summary = oic.summarize_variance(per_node)
    assert not summary.active
    assert summary.points == 22
    assert summary.peak == pytest.approx(19.5)  # pooled max
    # The busy node comes closest to its own requirement (19.5 of 35.7)
    # vs the quiet one (2.4 of 8.9), so it drives the summary.
    assert summary.node == "busy-node"
    assert summary.ratio == pytest.approx(19.5 / 17.0)


def test_evaluate_variance_by_node_fires_on_one_bursty_node():
    series = [
        make_range_series("steady", ["5"] * 10),
        make_range_series("bursty", ["5"] * 10 + ["28"]),
        make_range_series("busy", ["8"] * 11),
    ]
    per_node = oic.evaluate_variance_by_node(series, peak_threshold=30.0, ratio_threshold=1.2)
    summary = oic.summarize_variance(per_node)
    assert summary.variance_exceeded
    assert summary.node == "bursty"  # the driver, not the steady siblings
    assert summary.ratio == pytest.approx(5.6)
    assert summary.required_peak == pytest.approx(16.13, abs=0.01)
    assert [s.node for s in per_node] == ["bursty", "busy", "steady"]  # sorted by name


def test_evaluate_variance_by_node_pools_cards_on_one_node():
    # DCGM: one series per card.  Cards on the same node pool into that
    # node's windows; the per-node grouping keys on `instance`.
    series = [
        {"metric": {"gpu": "0", "instance": "gpu-a:9400"}, "values": [[0, "5"], [1, "6"]]},
        {"metric": {"gpu": "1", "instance": "gpu-a:9400"}, "values": [[0, "5"], [1, "6"]]},
    ]
    per_node = oic.evaluate_variance_by_node(series, peak_threshold=40.0, ratio_threshold=1.2)
    assert len(per_node) == 1
    assert per_node[0].node == "gpu-a"
    assert per_node[0].points == 4


def test_evaluate_variance_by_node_filters_dead_nodes():
    series = [
        make_range_series("dead-node", ["50"] * 5),
        make_range_series("live-node", ["2"] * 5),
    ]
    per_node = oic.evaluate_variance_by_node(
        series, peak_threshold=30.0, ratio_threshold=1.2, live_nodes={"live-node"}
    )
    assert [s.node for s in per_node] == ["live-node"]


def test_evaluate_variance_by_node_variance_only_drops_the_peak_branch():
    # A flat-hot master over its core-scaled threshold stays idle under the
    # variance-only rule; the same series without the exemption fires on the
    # absolute branch.  A burst on the master still accuses.
    flat = ["35"] * 10
    series = [make_range_series("master-0", flat), make_range_series("worker-0", ["3"] * 10)]
    per_node = oic.evaluate_variance_by_node(
        series, peak_threshold=30.0, ratio_threshold=2.0, variance_only_nodes={"master-0"}
    )
    by_node = {s.node: s for s in per_node}
    assert by_node["master-0"].peak_exceeded is False
    assert by_node["master-0"].peak_threshold is None
    assert by_node["master-0"].variance_exceeded is False  # flat, not bursty
    assert not oic.summarize_variance(per_node).active

    # Without the exemption the flat-hot master accuses on the peak branch.
    plain = oic.evaluate_variance_by_node(series, peak_threshold=30.0, ratio_threshold=2.0)
    assert {s.node: s for s in plain}["master-0"].peak_exceeded is True

    # A real burst on the master still fires the variance rule alone.
    bursty = [make_range_series("master-0", ["5"] * 10 + ["28"])]
    per_node = oic.evaluate_variance_by_node(
        bursty, peak_threshold=30.0, ratio_threshold=2.0, variance_only_nodes={"master-0"}
    )
    assert per_node[0].peak_exceeded is False
    assert per_node[0].variance_exceeded is True


def test_evaluate_variance_by_node_floor_suppresses_zero_baseline_noise():
    # The GPU rule's floor: a zero-median exporter peaking at 3% (DCGM's
    # idle noise tail) stays idle next to a
    # genuinely bursty sibling, which fires and drives the summary.
    series = [
        make_range_series("noise", ["0"] * 10 + ["3"]),
        make_range_series("bursty", ["5"] * 10 + ["45"]),
    ]
    per_node = oic.evaluate_variance_by_node(
        series, peak_threshold=40.0, ratio_threshold=1.2, floor=5.0
    )
    by_node = {s.node: s for s in per_node}
    assert not by_node["noise"].variance_exceeded  # 3% over a zero median: under the floor
    assert by_node["bursty"].variance_exceeded
    summary = oic.summarize_variance(per_node)
    assert summary.variance_exceeded
    assert summary.node == "bursty"
    assert summary.peak == pytest.approx(45.0)  # pooled max


def test_summarize_variance_empty_is_zeroed():
    stats = oic.summarize_variance([])
    assert stats.points == 0
    assert not stats.active
    assert stats.ratio is None
    assert stats.required_ratio is None
    assert stats.required_peak is None


def test_defaults_catch_the_false_idle_cores():
    """The patterns that motivated the rule: a hot node diluted by a quiet
    cluster, a hibernated cluster with a genuinely quiet uptime, and a
    workload that idles at 1% and loads past 10%."""
    cfg = base_config()
    # A hot node diluted by quiet siblings: median 5%, peak 96.9% - caught
    # by the peak branch.
    hot = oic.evaluate_variance(
        [5.0] * 20 + [96.9],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_variance_ratio,
        floor=oic.CPU_VARIANCE_FLOOR,
        baseline_full=cfg.cpu_peak_threshold / cfg.cpu_variance_ratio,
    )
    assert hot.active
    # Steady ~20% single-node load - stays idle.
    steady = oic.evaluate_variance(
        [19.5, 19.5, 19.5, 19.5],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_variance_ratio,
        floor=oic.CPU_VARIANCE_FLOOR,
        baseline_full=cfg.cpu_peak_threshold / cfg.cpu_variance_ratio,
    )
    assert not steady.active
    # The motivating burst: 1% idle, 11% under load - clears the required
    # ~4.9x (a required peak of 4.9).
    low_idle = oic.evaluate_variance(
        [1.0] * 20 + [11.0],
        peak_threshold=cfg.cpu_peak_threshold,
        ratio_threshold=cfg.cpu_variance_ratio,
        floor=oic.CPU_VARIANCE_FLOOR,
        baseline_full=cfg.cpu_peak_threshold / cfg.cpu_variance_ratio,
    )
    assert low_idle.active


# === MEMORY MOVEMENT RULE ===================================================


def steps(values, start=0.0, step=900.0):
    """(timestamp, value) windows at the query_range step."""
    return [(start + i * step, v) for i, v in enumerate(values)]


@pytest.mark.parametrize(
    ("values", "expected_active", "why"),
    [
        ([40.0] * 8, False, "flat: no movement however high the level"),
        ([40.0, 41.0, 42.0, 43.0], False, "drift: a point a window stays under the bar"),
        ([40.0, 44.0, 48.0, 52.0], True, "ramp: +4/window reads 12 across the span"),
        ([40.0, 47.5, 55.0, 55.0], True, "mid-window jump reads at full strength"),
        ([60.0, 55.0, 50.0, 45.0], False, "a drain never votes"),
        ([30.0], False, "one window: nothing to compare"),
        ([], False, "no data"),
        ([40.0, 50.0], False, "a rise exactly at the bar is not exceeded"),
    ],
)
def test_evaluate_memory_steps(values, expected_active, why):
    stats = oic.evaluate_memory_steps(steps(values), points_threshold=10.0)
    assert stats.active is expected_active, why


def test_memory_steps_mid_window_jump_dilution_math():
    # A 15-point jump landing mid-window: each window average moves half
    # the jump (40 -> 47.5 -> 55), so adjacent steps read +7.5 - under the
    # bar a 2-window rule would enforce - but the span compares a
    # fully-before window with a fully-after one and reads +15.
    stats = oic.evaluate_memory_steps(steps([40.0, 47.5, 55.0, 55.0]), points_threshold=10.0)
    assert stats.max_rise == pytest.approx(15.0)
    assert stats.rise_from == pytest.approx(40.0)
    assert stats.rise_at == 2 * 900  # the fully-after window
    assert stats.max_rise_by_span == pytest.approx({2: 7.5, 3: 15.0, 4: 15.0, 5: 15.0})


def test_memory_steps_calibrate_one_window_wider():
    # Pairs one group wider than the span feed max_rise_by_span only: a
    # gradual rise reaching 10 points four steps apart (a 5-window group)
    # stays idle under the default span but shows up in the calibration
    # table, so one run reports what a wider span would catch.
    points = steps([40.0, 42.5, 45.0, 47.5, 50.0])
    stats = oic.evaluate_memory_steps(points, points_threshold=10.0)
    assert not stats.active
    assert stats.max_rise == pytest.approx(7.5)
    assert stats.max_rise_by_span[5] == pytest.approx(10.0)


def test_memory_steps_gap_guard_slides_past_a_hole():
    # The pair test is on timestamps, not index distance: with a hours-long
    # hole, the pre-hole and post-hole levels never compare, so the windows
    # after the hole form their own sliding group instead of fabricating a
    # step across the hole.
    points = [*steps([40.0, 40.0]), (10_800.0, 70.0), (11_700.0, 70.0)]
    stats = oic.evaluate_memory_steps(points, points_threshold=10.0)
    assert stats.max_rise == 0.0
    assert stats.points == 4


def test_memory_steps_drop_is_debug_only():
    stats = oic.evaluate_memory_steps(steps([60.0, 55.0, 50.0, 45.0]), points_threshold=10.0)
    assert not stats.active
    assert stats.max_rise == 0.0
    assert stats.max_drop == pytest.approx(-15.0)
    assert stats.drop_at == 3 * 900


def test_evaluate_memory_steps_by_node_groups_filters_and_cleans():
    series = [
        make_range_series("live-a", ["30", "30", "45", "45"]),
        make_range_series("live-b", ["20"] * 4),
        make_range_series("dead", ["10", "10", "40", "40"]),
    ]
    per_node = oic.evaluate_memory_steps_by_node(
        series, points_threshold=10.0, live_nodes={"live-a", "live-b"}
    )
    assert [s.node for s in per_node] == ["live-a", "live-b"]  # sorted, dead dropped
    summary = oic.summarize_memory_steps(per_node)
    assert summary.active
    assert summary.node == "live-a"  # the largest rise drives
    assert summary.points == 8  # pooled across nodes
    assert summary.rise_exceeded


def test_evaluate_memory_steps_by_node_keys_on_instance():
    # OCP node series carry no `node` label, only `instance` (host:port) -
    # the same grouping as the CPU variance rule.  Junk samples drop
    # rather than poison the pairs.
    series = [
        {
            "metric": {"instance": "ip-10-0-1-2.ec2.internal:9100"},
            "values": [[0, "30"], [900, "junk"], [1800, "45"]],
        }
    ]
    per_node = oic.evaluate_memory_steps_by_node(series, points_threshold=10.0)
    assert len(per_node) == 1
    assert per_node[0].node == "ip-10-0-1-2.ec2.internal"
    assert per_node[0].points == 2
    assert per_node[0].active  # the surviving pair still reads +15


def test_summarize_memory_steps_empty_is_zeroed():
    stats = oic.summarize_memory_steps([])
    assert stats.points == 0
    assert not stats.active
    assert stats.max_rise == 0.0
    assert stats.rise_at is None

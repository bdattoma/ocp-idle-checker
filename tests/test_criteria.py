"""Unit tests for the five voting criteria (CPU, memory, API, GPU, operators).

Prometheus access is stubbed with FakeProm keyed on the exact query strings,
so a changed query fails the test instead of silently returning no data.  The
oc CLI is stubbed with FakeOc for the operators criterion.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

import pytest

import ocp_idle_check as oic
from helpers import (
    FakeOc,
    FakeProm,
    api_avg_query,
    base_config,
    event_time_before,
    gpu_avg_query,
    gpu_max_query,
    legacy_cpu_query,
    make_dcgm_series,
    make_event,
    make_range_series,
    memory_window_query,
    pod_row,
    rs_row,
    ts_before,
)

WINDOW = 10080


def gpu_node(name: str = "gpu-1") -> oic.GpuNode:
    return oic.GpuNode(name=name, vendor="NVIDIA", gpu_count=4, instance_type="g5.xlarge")


# === CPU ====================================================================


def test_cpu_active_on_peak():
    # Peak 45% crosses the threshold while the ratio stays low, isolating
    # the peak branch of the rule.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["35", "35", "45"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.counted
    assert prom.range_calls == [oic.CPU_RANGE_QUERY]
    entry = outcome.entry
    assert entry["peak"] == 45.0
    assert entry["peak_exceeded"] is True
    assert entry["variance_exceeded"] is False
    assert entry["value"] == "45.00"
    assert entry["source"] == "variance over 15m windows"


def test_cpu_active_on_variance_ratio():
    # Median 5%, peak 28%: no window crossed the peak threshold, but the
    # peak clears the required 25.8% (a 5.2x multiple) the curve demands
    # at a median of 5.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["5", "5", "28"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["variance_exceeded"] is True
    assert outcome.entry["ratio"] == 5.6
    assert outcome.entry["required_ratio"] == pytest.approx(5.16, abs=0.01)
    assert outcome.entry["required_peak"] == pytest.approx(25.79, abs=0.01)


def test_cpu_quiet_cluster_is_idle():
    # Steady quiet load: the peak barely rises above the median, so neither
    # branch fires.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("node-1", ["8", "8", "10"]),
                make_range_series("node-2", ["8", "8", "9"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1", "node-2"}, 2.0)
    assert outcome.result == "IDLE"
    assert outcome.entry["instant_override"] is False
    assert outcome.entry["value"] == "10.00"


def test_cpu_small_burst_on_quiet_median_is_active():
    # Median 2%, peak 16%: the motivating pattern - a bursty workload on a
    # quiet node is active.  The required peak at a 2% median is 15.0%
    # (a 7.5x multiple), so a 5x burst to 10% stays idle while 16% fires.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("node-1", ["2", "2", "16"]),
                make_range_series("node-2", ["2", "2", "10"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1", "node-2"}, 2.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["variance_exceeded"] is True
    assert outcome.entry["required_peak"] == pytest.approx(15.04, abs=0.01)
    per_node = {e["node"]: e for e in outcome.entry["per_node"]}
    assert per_node["node-1"]["variance_exceeded"] is True
    assert per_node["node-2"]["variance_exceeded"] is False


def test_cpu_large_burst_on_quiet_median_is_active():
    # Median 2%, peak 20%: 10x clears the 7.5x requirement.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["2", "2", "20"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 2.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["variance_exceeded"] is True


def test_cpu_steady_busy_and_quiet_nodes_are_idle():
    # Two steady modes pooled together: a steady ~17% node pooled with a
    # steady ~2% node.  Pooled, the median lands between the modes
    # and the busy node's peak over it reads as a 2x+ burst; judged per
    # node, both are steady and the criterion stays IDLE.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("busy-node", ["17"] * 10 + ["19.5"]),
                make_range_series("quiet-node", ["2"] * 10 + ["2.4"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"busy-node", "quiet-node"}, 2.0)
    assert outcome.result == "IDLE"
    assert outcome.entry["variance_exceeded"] is False
    assert outcome.entry["peak"] == 19.5  # pooled max across nodes
    assert outcome.entry["value"] == "19.50"
    # The summary reports the node closest to its own requirement - the
    # busy one (19.5 of a required 53.0) vs the quiet one (2.4 of 15.0) -
    # plus the full per-node detail.
    assert outcome.entry["node"] == "busy-node"
    assert outcome.entry["ratio"] == pytest.approx(1.15)  # 19.5/17, rounded as exported
    per_node = outcome.entry["per_node"]
    assert [e["node"] for e in per_node] == ["busy-node", "quiet-node"]
    assert all(e["variance_exceeded"] is False for e in per_node)


def test_cpu_single_bursty_node_among_steady_siblings_is_active():
    # Per-node cuts both ways: one genuinely bursty node fires on its own
    # variance even when pooled with steady siblings.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("node-1", ["5"] * 10),
                make_range_series("node-2", ["5"] * 10 + ["28"]),
                make_range_series("node-3", ["8"] * 11),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1", "node-2", "node-3"}, 2.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["variance_exceeded"] is True
    assert outcome.entry["node"] == "node-2"  # the driver, not a sibling
    assert outcome.entry["ratio"] == 5.6
    per_node = {e["node"]: e for e in outcome.entry["per_node"]}
    assert per_node["node-2"]["variance_exceeded"] is True
    assert per_node["node-1"]["variance_exceeded"] is False
    assert per_node["node-3"]["variance_exceeded"] is False


def test_cpu_dead_node_series_are_ignored():
    # A deleted node's bursty history must not vote; only live nodes count.
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("dead-node", ["50", "50", "50"]),
                make_range_series("node-1", ["2", "2", "2"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "IDLE"
    assert outcome.entry["peak"] == 2.0


def test_cpu_series_without_node_label_uses_instance():
    # OCP node exporter series carry only `instance`; _series_node strips
    # the port so the live-node filter still matches.
    series = {
        "metric": {"instance": "node-1:9100"},
        "values": [[1700000000, "50"]],
    }
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [series]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"


def test_cpu_legacy_fallback_active():
    prom = FakeProm(instant={legacy_cpu_query(WINDOW): 25.0})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["source"] == "legacy window average"
    assert outcome.entry["window_average"] == 25.0
    assert outcome.entry["value"] == "25.00"


def test_cpu_legacy_fallback_idle():
    prom = FakeProm(instant={legacy_cpu_query(WINDOW): 5.0})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 5.0)
    assert outcome.result == "IDLE"


def test_cpu_instant_override_beats_idle_variance():
    # Steady quiet windows, so the ACTIVE verdict comes from the instant
    # reading alone.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["8", "8", "10"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is True
    assert outcome.entry["instant"] == 50.0


def test_cpu_instant_override_does_not_beat_active():
    # One-directional: a low instant reading must not downgrade an ACTIVE.
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["5", "5", "50"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 1.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is False


def test_cpu_no_data_is_unknown_but_counted():
    outcome = oic.check_cpu(base_config(), FakeProm(), {"node-1"}, None)
    assert outcome.result == "UNKNOWN"
    assert outcome.counted  # bash parity: cpu UNKNOWN still counts in the denominator


def test_cpu_no_windowed_data_busy_instant_is_active():
    # Prometheus down for both windowed queries, snapshot busy: direct
    # evidence of activity, the direction the override already allows.
    outcome = oic.check_cpu(base_config(), FakeProm(), {"node-1"}, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["source"] == "instant"
    assert outcome.entry["instant_override"] is False


def test_cpu_no_windowed_data_quiet_instant_is_unknown():
    # ...and a quiet snapshot is one sample of the window: the override
    # lets an instant accuse but never acquit, and the no-data path is not
    # looser than that.  (Bash voted IDLE here; one sample cannot acquit
    # the window, so this is a deliberate divergence.)
    outcome = oic.check_cpu(base_config(), FakeProm(), {"node-1"}, 5.0)
    assert outcome.result == "UNKNOWN"
    assert outcome.counted
    assert outcome.entry["source"] == "instant"
    assert outcome.entry["value"] == "5.00"


def test_cpu_window_zero_uses_instant_only():
    prom = FakeProm()
    outcome = oic.check_cpu(base_config(time_window_minutes=0), prom, {"node-1"}, 5.0)
    assert prom.range_calls == []  # no range query with no window
    assert outcome.result == "IDLE"
    assert outcome.entry["source"] == "instant"
    assert outcome.entry["value"] == "5.00"


def test_cpu_window_zero_instant_above_threshold():
    outcome = oic.check_cpu(base_config(time_window_minutes=0), FakeProm(), {"node-1"}, 50.0)
    assert outcome.result == "ACTIVE"


# === MEMORY =================================================================


def test_memory_windowed_active():
    prom = FakeProm(instant={memory_window_query(WINDOW): 40.0})
    outcome = oic.check_memory(base_config(), prom, 10.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is False


def test_memory_windowed_idle():
    prom = FakeProm(instant={memory_window_query(WINDOW): 20.0})
    outcome = oic.check_memory(base_config(), prom, 30.0)
    assert outcome.result == "IDLE"
    assert outcome.entry["value"] == "20.00"
    assert outcome.entry["source"] == "window average"


def test_memory_instant_override():
    prom = FakeProm(instant={memory_window_query(WINDOW): 20.0})
    outcome = oic.check_memory(base_config(), prom, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is True


def test_memory_instant_only_when_window_zero():
    outcome = oic.check_memory(base_config(time_window_minutes=0), FakeProm(), 40.0)
    assert outcome.result == "ACTIVE"
    outcome = oic.check_memory(base_config(time_window_minutes=0), FakeProm(), 20.0)
    assert outcome.result == "IDLE"


def test_memory_no_data_is_unknown_but_counted():
    outcome = oic.check_memory(base_config(), FakeProm(), None)
    assert outcome.result == "UNKNOWN"
    assert outcome.counted


def test_memory_no_windowed_data_busy_instant_is_active():
    outcome = oic.check_memory(base_config(), FakeProm(), 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["source"] == "instant"
    assert outcome.entry["instant_override"] is False


def test_memory_no_windowed_data_quiet_instant_is_unknown():
    # Same rule as cpu: a quiet instant is one sample of the window and
    # cannot acquit it (bash voted IDLE here; deliberate divergence).
    outcome = oic.check_memory(base_config(), FakeProm(), 20.0)
    assert outcome.result == "UNKNOWN"
    assert outcome.counted
    assert outcome.entry["source"] == "instant"
    assert outcome.entry["value"] == "20.00"


# === API SERVER =============================================================


def test_api_average_above_threshold():
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 150.0},
        ranges={
            oic.API_RANGE_QUERY: [{"metric": {}, "values": [[i * 900, "10"] for i in range(4)]}]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["value"] == "150.00"


def test_api_quiet_is_idle():
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 5.0},
        ranges={
            oic.API_RANGE_QUERY: [{"metric": {}, "values": [[i * 900, "10"] for i in range(4)]}]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "IDLE"
    assert outcome.entry["variance_active"] is False


def test_api_variance_detector_marks_active():
    # Average 5 req/s is far below 100, but a 15-minute window hit 100 req/s:
    # 10x the median, clearing the required 38.8 (a 3.9x multiple) the
    # curve demands at a median of 10.
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 5.0},
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["10", "10", "100"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["variance_active"] is True
    assert outcome.entry["variance_peak"] == 100.0
    assert outcome.entry["variance_ratio"] == 10.0
    assert outcome.entry["variance_required_ratio"] == pytest.approx(3.88, abs=0.01)
    assert outcome.entry["variance_required_peak"] == pytest.approx(38.78, abs=0.01)


def test_api_variance_on_quiet_median_needs_a_bigger_burst():
    # Median 25 req/s: the curve demands 66.5 req/s (~2.7x), so a burst to
    # 55 is not variance, while a burst to 70 is.
    quiet = FakeProm(
        instant={api_avg_query(WINDOW): 25.0},
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["25", "25", "55"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), quiet)
    assert outcome.result == "IDLE"
    assert outcome.entry["variance_active"] is False
    assert outcome.entry["variance_required_ratio"] == pytest.approx(2.66, abs=0.01)
    loud = FakeProm(
        instant={api_avg_query(WINDOW): 25.0},
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["25", "25", "70"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), loud)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["variance_active"] is True


def test_api_steady_load_stays_idle():
    # Peak 80 is only 1x the median: past the baseline-of-full the required
    # multiple is clamped at the ratio, so the curve demands 160 req/s at a
    # median of 80, and the 80 req/s average is below the 100 threshold.
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 80.0},
        ranges={
            oic.API_RANGE_QUERY: [{"metric": {}, "values": [[i * 900, "80"] for i in range(4)]}]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "IDLE"  # 80 < 100 average threshold


def test_api_steady_load_below_a_raised_threshold_stays_idle():
    # The -a 500 scenario: a flat 400 req/s series sits below a raised
    # request threshold, and the clamped curve demands 2x the median (800
    # req/s), so steady load must not read as a burst.  Unclamped, the law
    # would demand ~339 req/s and flag the series ACTIVE.
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 400.0},
        ranges={
            oic.API_RANGE_QUERY: [{"metric": {}, "values": [[i * 900, "400"] for i in range(4)]}]
        },
    )
    outcome = oic.check_api(base_config(api_threshold=500.0), prom)
    assert outcome.result == "IDLE"
    assert outcome.entry["variance_active"] is False
    assert outcome.entry["variance_required_peak"] == pytest.approx(800.0)


def test_api_variance_active_without_average():
    # The average query failing does not sink the variance detector.
    prom = FakeProm(
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["10", "10", "100"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["value"] is None


def test_api_no_data_is_unknown_and_not_counted():
    outcome = oic.check_api(base_config(), FakeProm())
    assert outcome.result == "UNKNOWN"
    assert not outcome.counted


def test_api_window_zero_uses_5m_average():
    prom = FakeProm(instant={api_avg_query(5): 150.0})
    outcome = oic.check_api(base_config(time_window_minutes=0), prom)
    assert prom.range_calls == []
    assert outcome.result == "ACTIVE"


# === GPU ====================================================================


def test_gpu_na_without_gpu_nodes():
    prom = FakeProm()
    outcome = oic.check_gpu(base_config(), prom, [])
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no GPU nodes"
    assert prom.range_calls == []


def test_gpu_node_listing_failure_is_unknown_but_counted():
    # A failed `oc get nodes` listing is not "no GPU nodes": GPU presence
    # is unknown, and unknown is not dropped from the denominator.
    outcome = oic.check_gpu(base_config(), FakeProm(), None)
    assert outcome.result == "UNKNOWN"
    assert outcome.counted
    assert outcome.entry["result"] == "UNKNOWN"
    assert outcome.entry["reason"] == "node listing failed (oc error or timeout)"


def test_gpu_na_without_time_window():
    outcome = oic.check_gpu(base_config(time_window_minutes=0), FakeProm(), [gpu_node()])
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no time window"


def test_gpu_active_on_peak():
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["5", "5", "50"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"
    assert outcome.counted
    assert outcome.entry["value"] == "50.00"
    assert outcome.entry["source"] == "variance over 15m windows"


def test_gpu_active_on_variance_ratio():
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["0", "0", "30"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"
    assert outcome.entry["peak_exceeded"] is False
    assert outcome.entry["variance_exceeded"] is True  # zero baseline: the floor is the requirement
    assert outcome.entry["ratio"] is None
    assert outcome.entry["required_peak"] == pytest.approx(oic.GPU_VARIANCE_FLOOR)


def test_gpu_zero_baseline_below_the_floor_is_idle():
    # Dense-but-zero DCGM data peaking at 3.3% all week: the zero baseline
    # demands the 5% floor, so the DCGM idle noise tail stays idle.
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["0", "0", "3.3"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "IDLE"
    assert outcome.entry["variance_exceeded"] is False
    assert outcome.entry["required_peak"] == pytest.approx(oic.GPU_VARIANCE_FLOOR)


def test_gpu_quiet_is_idle():
    # Steady quiet utilization: ratio 1.25, well below both branches.
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["8", "8", "10"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "IDLE"


def test_gpu_pools_all_dcgm_series():
    # Every exporter series votes, not just per-node medians: one card's
    # burst among several quiet cards still marks the criterion ACTIVE.
    # (All cards here share an instance, so they pool into one node.)
    prom = FakeProm(
        ranges={
            oic.GPU_RANGE_QUERY: [
                make_dcgm_series(["0", "0", "0"], gpu="0"),
                make_dcgm_series(["0", "0", "0"], gpu="1"),
                make_dcgm_series(["0", "0", "45"], gpu="2"),
            ]
        }
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"


def test_gpu_groups_per_node_and_fires_on_one_bursty_node():
    # Two GPU nodes: cards on the same node pool into that node's windows,
    # and one node's burst fires on its own variance while its sibling stays
    # steady.
    prom = FakeProm(
        ranges={
            oic.GPU_RANGE_QUERY: [
                make_dcgm_series(["5"] * 4, gpu="0", instance="gpu-a:9400"),
                make_dcgm_series(["5"] * 4, gpu="1", instance="gpu-a:9400"),
                make_dcgm_series(["5"] * 4 + ["35"], gpu="0", instance="gpu-b:9400"),
            ]
        }
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node("gpu-a"), gpu_node("gpu-b")])
    assert outcome.result == "ACTIVE"
    assert outcome.entry["node"] == "gpu-b"  # the bursty node drives the summary
    per_node = {e["node"]: e for e in outcome.entry["per_node"]}
    assert per_node["gpu-b"]["variance_exceeded"] is True
    assert per_node["gpu-a"]["variance_exceeded"] is False


def test_gpu_deleted_node_samples_still_vote():
    # Unlike cpu, the GPU rule has no live filter: a deleted GPU node's
    # samples are still activity, so its history fires here even though the
    # node is absent from the gpu_nodes list.
    prom = FakeProm(
        ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["0", "0", "60"], instance="old-gpu:9400")]}
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node("new-gpu")])
    assert outcome.result == "ACTIVE"


def test_gpu_aggregate_fallback():
    # No query_range matrix: whole-window max/avg aggregates decide.  Peak
    # 30 over a 4% average clears the required 22.6% the curve demands
    # at that baseline.
    prom = FakeProm(
        instant_all={
            gpu_max_query(WINDOW): [
                {"metric": {"gpu": "0"}, "value": [1700000000, "30"]},
                {"metric": {"gpu": "1"}, "value": [1700000000, "10"]},
            ],
            gpu_avg_query(WINDOW): [
                {"metric": {"gpu": "0"}, "value": [1700000000, "5"]},
                {"metric": {"gpu": "1"}, "value": [1700000000, "3"]},
            ],
        }
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"
    assert outcome.entry["source"] == "window aggregates fallback"
    assert outcome.entry["peak"] == 30.0
    assert outcome.entry["baseline"] == 4.0  # mean of the per-series averages
    assert outcome.entry["ratio"] == 7.5  # 30/4, rounded as exported
    assert outcome.entry["required_ratio"] == pytest.approx(5.65, abs=0.01)
    assert outcome.entry["required_peak"] == pytest.approx(22.61, abs=0.01)


def test_gpu_aggregate_fallback_quiet():
    prom = FakeProm(
        instant_all={
            gpu_max_query(WINDOW): [{"metric": {"gpu": "0"}, "value": [1700000000, "5"]}],
            gpu_avg_query(WINDOW): [{"metric": {"gpu": "0"}, "value": [1700000000, "3"]}],
        }
    )
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "IDLE"


def test_gpu_na_without_dcgm_metrics():
    outcome = oic.check_gpu(base_config(), FakeProm(), [gpu_node()])
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no DCGM metrics"


# === OPERATORS ==============================================================

# Fixed clock for the operator tests: ages are exact and the event window
# boundaries are testable at 47/49 hours.
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

RS_KEY = ("opendatahub", "odh-operator-controller-manager-abc", "odh-operator-5d4c3b")


def old_operator_pod(namespace: str = "opendatahub") -> str:
    return pod_row(*RS_KEY[:2], "ReplicaSet", RS_KEY[2], ts_before(NOW, days=12))


def old_operator_rs() -> str:
    return rs_row(RS_KEY[0], RS_KEY[2], ts_before(NOW, days=12))


def reconciled_event(
    namespace: str = "opendatahub",
    hours: float = 1.0,
    timestamp: str | None = None,
    event_time: str | None = None,
) -> dict:
    return make_event(
        namespace,
        now=NOW,
        hours=hours,
        timestamp=timestamp,
        event_time=event_time,
        reason="Reconciled",
        kind="DataScienceCluster",
        involved="dsc-sample",
        message="reconciliation complete",
    )


def test_operators_old_and_quiet_is_idle():
    oc = FakeOc(pods=[old_operator_pod()], replicasets=[old_operator_rs()], events=[])
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.counted
    assert outcome.entry["age_days"] == 12.0
    assert outcome.entry["events"] == 0  # below the threshold of 5
    assert outcome.entry["namespaces"] == ["opendatahub"]
    assert outcome.entry["event_window_hours"] == 48
    assert outcome.entry["units"] == [
        {
            "namespace": "opendatahub",
            "kind": "replicaset",
            "name": "odh-operator-5d4c3b",
            "age_days": 12.0,
        }
    ]
    assert oc.calls == ["pods", "replicasets", "events"]


def test_operators_young_controllers_mark_active():
    pod = pod_row(*RS_KEY[:2], "ReplicaSet", RS_KEY[2], ts_before(NOW, days=2))
    rs = rs_row(RS_KEY[0], RS_KEY[2], ts_before(NOW, days=2))
    outcome = oic.check_operators(base_config(), FakeOc(pods=[pod], replicasets=[rs]), now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["age_days"] == 2.0


def test_operators_median_young_majority_is_active():
    # Two of three controllers redeployed two days ago; one stale survivor
    # is 40 days old.  The old oldest-pod rule called this IDLE.
    pods = [
        pod_row(
            "ns-a",
            f"{name}-controller-manager",
            "ReplicaSet",
            f"{name}-rs",
            ts_before(NOW, days=age),
        )
        for name, age in (("a", 2), ("b", 2), ("c", 40))
    ]
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods), now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["age_days"] == 2.0


def test_operators_median_old_majority_stays_idle():
    # One fresh unit among old ones: a single recent restart must not mark
    # the cluster active.  Ages 30, 30, 1 -> median 30.
    pods = [
        pod_row(
            "ns-a",
            f"{name}-controller-manager",
            "ReplicaSet",
            f"{name}-rs",
            ts_before(NOW, days=age),
        )
        for name, age in (("a", 30), ("b", 30), ("c", 1))
    ]
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 30.0


def test_operators_even_sample_averages_the_middle_ages():
    # Ages 2, 2, 30, 30: the median is 16, above the threshold even though
    # half the stack is fresh.
    pods = [
        pod_row(
            "ns-a",
            f"{name}-controller-manager",
            "ReplicaSet",
            f"{name}-rs",
            ts_before(NOW, days=age),
        )
        for name, age in (("a", 2), ("b", 2), ("c", 30), ("d", 30))
    ]
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 16.0


def test_operators_replicaset_age_survives_drain():
    # The ReplicaSet is 30 days old but its pod was recreated yesterday by
    # a node drain: the unit's age is the ReplicaSet's, not the pod's.
    pod = pod_row(*RS_KEY[:2], "ReplicaSet", RS_KEY[2], ts_before(NOW, days=1))
    rs = rs_row(RS_KEY[0], RS_KEY[2], ts_before(NOW, days=30))
    outcome = oic.check_operators(base_config(), FakeOc(pods=[pod], replicasets=[rs]), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 30.0


def test_operators_bare_pod_falls_back_to_pod_age():
    # A controller pod with no owner references is aged by its own creation
    # time; no ReplicaSet listing is needed for it.
    oc = FakeOc(
        pods=[pod_row("team-a", "standalone-operator", "", "", ts_before(NOW, days=2))], events=[]
    )
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["age_days"] == 2.0
    assert oc.calls == ["pods", "events"]


def test_operators_missing_replicaset_falls_back_to_pod_age():
    # A ReplicaSet deleted mid-rollout leaves its pods behind; the oldest
    # replica's age stands in, and unrelated ReplicaSets (here: an old
    # scaled-to-zero revision) are not read.
    pods = [
        pod_row(
            RS_KEY[0],
            f"odh-operator-controller-manager-{sfx}",
            "ReplicaSet",
            "odh-operator-gone",
            ts_before(NOW, days=age),
        )
        for sfx, age in (("abc", 30), ("def", 1))
    ]
    rs = rs_row(RS_KEY[0], "unrelated-old-revision", ts_before(NOW, days=5))
    outcome = oic.check_operators(base_config(), FakeOc(pods=pods, replicasets=[rs]), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["age_days"] == 30.0


def test_operators_dedupes_replicas_of_one_replicaset():
    # Three replicas of one ReplicaSet are a single unit; the median must
    # not be skewed by replica count.
    pods = [
        pod_row(
            RS_KEY[0],
            f"odh-operator-controller-manager-{sfx}",
            "ReplicaSet",
            RS_KEY[2],
            ts_before(NOW, days=12),
        )
        for sfx in ("abc", "def", "ghi")
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=pods, replicasets=[old_operator_rs()]), now=NOW
    )
    assert len(outcome.entry["units"]) == 1
    assert outcome.entry["age_days"] == 12.0


def test_operators_event_window_is_48_hours():
    # 47 hours old counts, 49 does not - by legacy timestamp or by eventTime -
    # and an event with no timestamps at all is treated as outside the window.
    events = [
        reconciled_event(hours=47),
        reconciled_event(hours=49),
        reconciled_event(timestamp=""),
        reconciled_event(timestamp="", event_time=event_time_before(NOW, hours=47)),
        reconciled_event(timestamp="", event_time=event_time_before(NOW, hours=49)),
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.entry["events"] == 2
    assert outcome.result == "IDLE"


def test_operators_events_api_events_count_at_event_time():
    # An event recorded through the newer events API carries only eventTime -
    # microsecond precision, both legacy timestamps null.  The bash checker
    # saw these through the tabular printer's LAST SEEN fallback, so dropping
    # them biased the count toward zero and the verdict toward IDLE.
    events = [
        reconciled_event(timestamp="", event_time=event_time_before(NOW, hours=1)),
        # A workload-kind events-API event lands in the exclusion, not the
        # count, like its legacy-shaped sibling.
        make_event(
            "opendatahub",
            now=NOW,
            reason="Created",
            kind="Pod",
            message="created pod odh-xyz",
            timestamp="",
            event_time=event_time_before(NOW, hours=1),
        ),
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.entry["events"] == 1
    assert outcome.entry["events_excluded_workload"] == 1
    assert outcome.result == "IDLE"


def test_operators_multiline_message_events_count():
    # A message is free text, and about one event in seven on a live-cluster
    # sample carried embedded newlines (kubelet pod-sandbox blobs, long
    # reconciler output).  In a fixed-column listing each newline split the
    # row into fragments the parser dropped, biasing the count toward zero;
    # the JSON listing carries the message intact, so the event counts - as
    # it did for the bash checker's tabular grep.
    events = [
        make_event(
            "opendatahub",
            now=NOW,
            reason="Reconciled",
            kind="DataScienceCluster",
            involved="dsc-sample",
            message="reconciliation\ncomplete: created thing",
        )
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.entry["events"] == 1


def test_operators_recent_events_mark_active():
    events = [reconciled_event(hours=1) for _ in range(5)]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.result == "ACTIVE"
    assert outcome.entry["events"] == 5


def test_operators_workload_lifecycle_events_are_excluded():
    # Events about pods, replica sets, and deployments are the noise a node
    # drain manufactures; they match the regex but must not count toward
    # the threshold.  The excluded tally is exported for calibration.
    events = [
        make_event(
            "opendatahub", now=NOW, reason="Created", kind="Pod", message="Created pod odh-xyz"
        ),
        make_event(
            "opendatahub",
            now=NOW,
            reason="SuccessfulCreate",
            kind="ReplicaSet",
            message="created pod odh-xyz-abc",
        ),
        make_event(
            "opendatahub",
            now=NOW,
            reason="ScalingReplicaSet",
            kind="Deployment",
            message="scaled up replica set odh-xyz",
        ),
        reconciled_event(),
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.result == "IDLE"
    assert outcome.entry["events"] == 1
    assert outcome.entry["events_excluded_workload"] == 3


def test_operators_platform_namespaces_are_ignored():
    # openshift-* and kube-* hold the platform's own controllers, whose
    # pods and events would swamp the criterion.
    oc = FakeOc(
        pods=[
            pod_row(
                "openshift-operators",
                "cluster-automation-operator-abc",
                "ReplicaSet",
                "cao-rs",
                ts_before(NOW, days=1),
            ),
            pod_row("kube-system", "some-operator-xyz", "", "", ts_before(NOW, days=1)),
        ],
        events=[reconciled_event("openshift-ingress-operator")],
    )
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "N/A"
    assert not outcome.counted
    assert outcome.entry["reason"] == "no controller pods in any scanned namespace"


def test_operators_configured_namespaces_override_the_exclusions():
    # A namespace on the always-include list counts even when it matches an
    # excluded prefix (an ODH install inside openshift-*, say).
    cfg = base_config(operator_namespaces="openshift-odh-custom")
    pod = pod_row(
        "openshift-odh-custom", "my-operator-1", "ReplicaSet", "my-rs", ts_before(NOW, days=12)
    )
    outcome = oic.check_operators(cfg, FakeOc(pods=[pod]), now=NOW)
    assert outcome.result == "IDLE"
    assert outcome.entry["namespaces"] == ["openshift-odh-custom"]


def test_operators_na_makes_no_further_oc_calls():
    oc = FakeOc(pods=[], events=[])
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "N/A"
    assert not outcome.counted
    # The pod listing alone rules out the criterion; no ReplicaSet or event
    # listings follow.
    assert oc.calls == ["pods"]


def test_operators_pod_listing_failure_is_unknown_not_na():
    # A timed-out pod listing is not "no controller pods": the criterion
    # must not masquerade as a measured N/A (run 25 saw this listing time
    # out at the 10 s point-call budget).
    outcome = oic.check_operators(base_config(), FakeOc(pods_failed=True), now=NOW)
    assert outcome.result == "UNKNOWN"
    assert not outcome.counted
    assert outcome.entry["result"] == "UNKNOWN"
    assert outcome.entry["reason"] == "pod listing failed (oc error or timeout)"


def test_operators_event_listing_failure_is_unknown_not_idle():
    # Zero events on a failed listing would vote IDLE for old, unverified
    # operators; the criterion goes UNKNOWN instead.
    oc = FakeOc(pods=[old_operator_pod()], replicasets=[old_operator_rs()], events_failed=True)
    outcome = oic.check_operators(base_config(), oc, now=NOW)
    assert outcome.result == "UNKNOWN"
    assert not outcome.counted
    assert outcome.entry["reason"] == "event listing failed (oc error or timeout)"
    assert oc.calls == ["pods", "replicasets", "events"]


def test_operators_replicaset_listing_failure_is_unknown():
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], replicasets_failed=True), now=NOW
    )
    assert outcome.result == "UNKNOWN"
    assert not outcome.counted
    assert outcome.entry["reason"] == "ReplicaSet listing failed (oc error or timeout)"


def test_operators_scans_beyond_the_configured_namespaces():
    # Controllers running in user namespaces - strimzi, prometheus-operator,
    # istio - are operator activity too; the scan covers them.
    pod = pod_row(
        "team-a", "strimzi-cluster-operator-abc", "ReplicaSet", "strimzi-rs", ts_before(NOW, days=1)
    )
    outcome = oic.check_operators(base_config(), FakeOc(pods=[pod]), now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["namespaces"] == ["team-a"]


def test_operators_events_sum_across_scanned_namespaces():
    # Events count from every scanned namespace, not just the ones holding
    # matching pods; strimzi reconciles Kafka custom resources, not pods.
    events = [reconciled_event() for _ in range(2)]
    events += [
        make_event(
            "team-a",
            now=NOW,
            reason="Reconciled",
            kind="Kafka",
            involved="my-cluster",
            message="reconciliation complete",
        )
        for _ in range(3)
    ]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.result == "ACTIVE"
    assert outcome.entry["events"] == 5


# === THRESHOLD RATIO ========================================================


def test_cpu_threshold_ratio_peak_branch():
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["35", "35", "45"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == 1.5


def test_cpu_threshold_ratio_variance_branch():
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["5", "5", "28"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(1.09, abs=0.01)


def test_cpu_threshold_ratio_idle_below_one():
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["8", "8", "10"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 2.0)
    assert outcome.result == "IDLE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(0.33, abs=0.01)


def test_cpu_threshold_ratio_legacy_fallback():
    prom = FakeProm(instant={legacy_cpu_query(WINDOW): 25.0})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, None)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(25 / 15, abs=0.01)


def test_cpu_threshold_ratio_instant_accuses():
    outcome = oic.check_cpu(base_config(), FakeProm(), {"node-1"}, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(3.33, abs=0.01)


def test_cpu_threshold_ratio_instant_override_counts():
    prom = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["8", "8", "10"])]})
    outcome = oic.check_cpu(base_config(), prom, {"node-1"}, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is True
    assert outcome.entry["threshold_ratio"] == pytest.approx(3.33, abs=0.01)


def test_cpu_threshold_ratio_unknown_is_none():
    outcome = oic.check_cpu(base_config(), FakeProm(), {"node-1"}, 5.0)
    assert outcome.result == "UNKNOWN"
    assert outcome.entry["threshold_ratio"] is None


def test_cpu_threshold_ratio_max_across_nodes():
    prom = FakeProm(
        ranges={
            oic.CPU_RANGE_QUERY: [
                make_range_series("node-1", ["2", "2", "16"]),
                make_range_series("node-2", ["2", "2", "10"]),
            ]
        }
    )
    outcome = oic.check_cpu(base_config(), prom, {"node-1", "node-2"}, 2.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(1.06, abs=0.01)


def test_memory_threshold_ratio_windowed():
    prom = FakeProm(instant={memory_window_query(WINDOW): 20.0})
    outcome = oic.check_memory(base_config(), prom, None)
    assert outcome.result == "IDLE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(20 / 35, abs=0.01)


def test_memory_threshold_ratio_instant_override_counts():
    prom = FakeProm(instant={memory_window_query(WINDOW): 20.0})
    outcome = oic.check_memory(base_config(), prom, 50.0)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["instant_override"] is True
    assert outcome.entry["threshold_ratio"] == pytest.approx(50 / 35, abs=0.01)


def test_api_threshold_ratio_variance_branch():
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 5.0},
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["10", "10", "100"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(2.58, abs=0.01)


def test_api_threshold_ratio_zero_baseline_is_infinite():
    prom = FakeProm(
        instant={api_avg_query(WINDOW): 5.0},
        ranges={
            oic.API_RANGE_QUERY: [
                {"metric": {}, "values": [[i * 900, v] for i, v in enumerate(["0", "0", "0", "5"])]}
            ]
        },
    )
    outcome = oic.check_api(base_config(), prom)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == math.inf


def test_gpu_threshold_ratio():
    prom = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["5", "5", "50"])]})
    outcome = oic.check_gpu(base_config(), prom, [gpu_node()])
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(1.94, abs=0.01)


def test_gpu_threshold_ratio_na_is_none():
    outcome = oic.check_gpu(base_config(), FakeProm(), [gpu_node()])
    assert outcome.result == "N/A"
    assert outcome.entry["threshold_ratio"] is None


def test_operators_threshold_ratio_young_controllers():
    pod = pod_row(*RS_KEY[:2], "ReplicaSet", RS_KEY[2], ts_before(NOW, days=2))
    rs = rs_row(RS_KEY[0], RS_KEY[2], ts_before(NOW, days=2))
    outcome = oic.check_operators(base_config(), FakeOc(pods=[pod], replicasets=[rs]), now=NOW)
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == 3.5


def test_operators_threshold_ratio_events():
    events = [reconciled_event() for _ in range(6)]
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], events=events), now=NOW
    )
    assert outcome.result == "ACTIVE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(1.2)


def test_operators_threshold_ratio_idle():
    outcome = oic.check_operators(
        base_config(), FakeOc(pods=[old_operator_pod()], replicasets=[old_operator_rs()]), now=NOW
    )
    assert outcome.result == "IDLE"
    assert outcome.entry["threshold_ratio"] == pytest.approx(7 / 12, abs=0.01)


def test_operators_threshold_ratio_unknown_is_none():
    outcome = oic.check_operators(base_config(), FakeOc(pods_failed=True), now=NOW)
    assert outcome.result == "UNKNOWN"
    assert outcome.entry["threshold_ratio"] is None


def test_threshold_ratio_separates_active_from_idle():
    # Invariant: ACTIVE implies heat >= 1.0; IDLE implies heat < 1.0. Samples
    # stay clear of the exact boundary (strict vs non-strict comparisons).
    peak = FakeProm(ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["35", "35", "45"])]})
    variance = FakeProm(
        ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["5", "5", "28"])]}
    )
    idle_windows = FakeProm(
        ranges={oic.CPU_RANGE_QUERY: [make_range_series("node-1", ["8", "8", "10"])]}
    )
    gpu_active = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["5", "5", "50"])]})
    gpu_idle = FakeProm(ranges={oic.GPU_RANGE_QUERY: [make_dcgm_series(["8", "8", "10"])]})
    busy_mem = FakeProm(instant={memory_window_query(WINDOW): 40.0})
    quiet_mem = FakeProm(instant={memory_window_query(WINDOW): 20.0})
    samples = [
        oic.check_cpu(base_config(), peak, {"node-1"}, None),
        oic.check_cpu(base_config(), variance, {"node-1"}, None),
        oic.check_cpu(base_config(), idle_windows, {"node-1"}, 2.0),
        oic.check_memory(base_config(), busy_mem, 10.0),
        oic.check_memory(base_config(), quiet_mem, 30.0),
        oic.check_gpu(base_config(), gpu_active, [gpu_node()]),
        oic.check_gpu(base_config(), gpu_idle, [gpu_node()]),
    ]
    for outcome in samples:
        heat = outcome.entry["threshold_ratio"]
        assert heat is not None
        if outcome.result == "ACTIVE":
            assert heat >= 1.0
        else:
            assert outcome.result == "IDLE"
            assert heat < 1.0


def test_parse_k8s_timestamp():
    assert oic.parse_k8s_timestamp("2026-10-01T12:00:00Z") == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert oic.parse_k8s_timestamp("") is None
    assert oic.parse_k8s_timestamp("not-a-timestamp") is None
    # Naive values are read as UTC, never left to poison the arithmetic.
    assert oic.parse_k8s_timestamp("2026-10-01T12:00:00") == datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_node_top_percentages_maps_and_skips_malformed():
    lines = [
        "node-1 250m 2% 3212Mi 5%",
        "gpu-node-1 500m 3% 8000Mi 6%",
        "short line",
        "node-2 100m junk% 2000Mi 7%",
    ]
    # Malformed rows are dropped whole; later duplicates win, like a dict build.
    assert oic.node_top_percentages(lines) == {
        "node-1": (2.0, 5.0),
        "gpu-node-1": (3.0, 6.0),
    }


def test_instant_by_instance_and_average_matching():
    series = [
        {"metric": {"instance": "gpu-node-1:9100"}, "value": [1700000000, "7"]},
        {"metric": {"instance": "gpu-node-10:9100"}, "value": [1700000000, "9"]},
        {"metric": {"instance": "other:9100"}, "value": [1700000000, "50"]},
        {"metric": {}, "value": [1700000000, "junk"]},
    ]
    by_instance = oic.instant_by_instance(series)
    assert by_instance == {"gpu-node-1:9100": 7.0, "gpu-node-10:9100": 9.0, "other:9100": 50.0}
    # The prefix quirk is inherited from the original per-node queries:
    # `instance=~"gpu-node-1.*"` also matches gpu-node-10's series, so the
    # mean pools both. An unmatched node has no value.
    gpu_node_1 = re.compile(r"gpu-node-1.*")
    assert oic.average_matching(by_instance, gpu_node_1) == 8.0
    assert oic.average_matching(by_instance, re.compile(r"missing.*")) is None
    assert oic.instant_by_instance(None) == {}


def test_instance_alternation_is_promql_escaped():
    # The alternation is embedded in a double-quoted PromQL string literal,
    # where a lone backslash is an invalid escape sequence and fails the whole
    # query (run 25 returned null windowed GPU usage on every cluster this way).
    name = "ip-10-0-1-5.ec2.internal"
    pattern = oic._instance_alternation([name])
    assert pattern == "ip\\\\-10\\\\-0\\\\-1\\\\-5\\\\.ec2\\\\.internal.*"
    # PromQL unescaping (\\ -> \) must restore the re.escape pattern that the
    # Python-side matching uses, so both sides select the same series.
    assert pattern.replace("\\\\", "\\") == re.escape(name) + ".*"


def test_cluster_wide_listings_carry_their_own_timeout(monkeypatch):
    # The cluster-wide listings can be large on busy clusters (run 25 saw
    # the pods and events listings time out at the 10 s point-call budget);
    # they must pass the listings' own, larger budget to Oc.run.  The nodes
    # JSON is small but metadata-fat, so it rides the same budget.
    seen: list[tuple[tuple[str, ...], float]] = []

    def fake_run(self, args, timeout=oic.OC_TIMEOUT):
        seen.append((tuple(args[:3]), timeout))
        return ""

    monkeypatch.setattr(oic.Oc, "run", fake_run)
    oc = oic.Oc()
    oc.pods_all()
    oc.replicasets_all()
    oc.events_all()
    oc.nodes()
    assert seen == [
        (("get", "pods", "-A"), oic.OC_LIST_TIMEOUT),
        (("get", "rs", "-A"), oic.OC_LIST_TIMEOUT),
        (("get", "events", "-A"), oic.OC_LIST_TIMEOUT),
        (("get", "nodes", "-o"), oic.OC_LIST_TIMEOUT),
    ]


def test_nodes_listing_failure_is_none_not_empty(monkeypatch):
    # A failed or malformed nodes listing is None (unknown inventory), not
    # [] (known-empty): GPU reads UNKNOWN on it and main leaves CPU series
    # unfiltered.  The failure is cached, like the other listings'.
    returns: list[str | None] = [None, "{not json"]

    def fake_run(self, args, timeout=oic.OC_TIMEOUT):
        return returns.pop(0)

    monkeypatch.setattr(oic.Oc, "run", fake_run)
    oc = oic.Oc()
    assert oc.nodes() is None
    assert oc.nodes() is None  # cached: the malformed retry never runs
    assert returns == ["{not json"]

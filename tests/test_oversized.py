"""Unit tests for the oversized classification (active cluster, idle nodes)."""

from __future__ import annotations

import ocp_idle_check as oic


def variance_node(node: str, peak: float, peak_exceeded: bool = False) -> dict:
    """A per-node entry as _variance_entry renders it."""
    return {"node": node, "peak": peak, "peak_exceeded": peak_exceeded}


def oversized_kwargs(**overrides) -> dict:
    """evaluate_oversized inputs for an ACTIVE cluster with one quiet worker."""
    kwargs = dict(
        status="ACTIVE",
        quorum_driven=False,
        cpu_entry={"per_node": [variance_node("worker-1", 12.5)]},
        gpu_entry={"result": "N/A", "reason": "no GPU nodes"},
        gpu_node_names=set(),
        worker_nodes={"worker-1"},
    )
    kwargs.update(overrides)
    return kwargs


def test_oversized_false_when_cluster_not_active():
    out = oic.evaluate_oversized(**oversized_kwargs(status="IDLE"))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_false_when_no_criterion_fired():
    # A quorum-driven ACTIVE has no evidence of use, so no right-sizing
    # claim either.
    out = oic.evaluate_oversized(**oversized_kwargs(quorum_driven=True))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_false_when_node_listing_failed():
    out = oic.evaluate_oversized(**oversized_kwargs(worker_nodes=None, gpu_node_names=None))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_false_without_per_node_cpu_data():
    # Instant-only CPU (empty matrix): no per-node peaks to judge.
    cpu_entry = {"result": "IDLE", "instant": 8.0}
    out = oic.evaluate_oversized(**oversized_kwargs(cpu_entry=cpu_entry))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_true_lists_level_idle_workers():
    out = oic.evaluate_oversized(**oversized_kwargs())
    assert out == {
        "result": True,
        "gpu_peak": None,
        "nodes": [{"node": "worker-1", "cpu_peak": 12.5, "gpu_node": False}],
    }


def test_oversized_ignores_variance_firings():
    # A node that only ever low-level-bursted (variance exceeded, peak under
    # the threshold) is still level-idle - that is the whole point.
    cpu_entry = {"per_node": [{**variance_node("worker-1", 11.0), "variance_exceeded": True}]}
    out = oic.evaluate_oversized(**oversized_kwargs(cpu_entry=cpu_entry))
    assert out["result"] is True


def test_oversized_skips_nodes_that_fired_the_peak_branch():
    cpu_entry = {
        "per_node": [variance_node("worker-1", 12.5), variance_node("worker-2", 44.0, True)]
    }
    out = oic.evaluate_oversized(
        **oversized_kwargs(cpu_entry=cpu_entry, worker_nodes={"worker-1", "worker-2"})
    )
    assert out["result"] is True
    assert [e["node"] for e in out["nodes"]] == ["worker-1"]


def test_oversized_excludes_control_plane_and_infra_nodes():
    # A quiet master or infra node is expected, not right-sizing evidence;
    # a SNO (its only node control-plane) can therefore never be oversized.
    cpu_entry = {"per_node": [variance_node("master-0", 12.5), variance_node("infra-0", 9.0)]}
    out = oic.evaluate_oversized(**oversized_kwargs(cpu_entry=cpu_entry, worker_nodes=set()))
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_gpu_node_needs_untouched_gpus():
    # DCGM series cannot be attributed to node names, so a GPU node is only
    # acquirable when no GPU series anywhere ever met the variance floor.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    busy_gpu = {"per_node": [variance_node("10.130.2.24", 55.0, True)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry=busy_gpu,
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out == {"result": False, "gpu_peak": None, "nodes": []}

    idle_gpu = {"peak": 0.0, "per_node": [variance_node("10.130.2.24", 0.0)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry=idle_gpu,
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out["nodes"] == [{"node": "gpu-0", "cpu_peak": 5.0, "gpu_node": True}]
    # The acquittal's receipt: the hottest series cluster-wide, not the
    # node's own peak (which series cannot be attributed to).
    assert out["gpu_peak"] == 0.0


def test_oversized_gpu_series_meeting_the_floor_blocks_acquittal():
    # The VRAM case: utilization is a weak removal signal for GPU nodes -
    # VRAM capacity is often why the GPU was picked, and a memory-resident
    # workload computes little - so a series sitting well under the peak
    # threshold still blocks acquittal once it meets the variance floor.
    # At the floor exactly it has met it: the noise band is strictly below.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    for series_peak in (12.0, oic.GPU_VARIANCE_FLOOR):
        touched_gpu = {"peak": series_peak, "per_node": [variance_node("10.130.2.24", series_peak)]}
        out = oic.evaluate_oversized(
            **oversized_kwargs(
                cpu_entry=cpu_entry,
                gpu_entry=touched_gpu,
                gpu_node_names={"gpu-0"},
                worker_nodes={"gpu-0"},
            )
        )
        assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_gpu_node_without_gpu_data_is_not_acquitted():
    # GPU nodes present but no DCGM metrics: the GPU leg cannot be judged,
    # so the node is not named.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry={"result": "N/A", "reason": "no DCGM metrics"},
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out == {"result": False, "gpu_peak": None, "nodes": []}


def test_oversized_gpu_pooled_fallback_entry_counts():
    # The aggregate fallback carries the pooled peak at the top level; the
    # floor check reads it the same way as the per-node peaks.
    cpu_entry = {"per_node": [variance_node("gpu-0", 5.0)]}
    out = oic.evaluate_oversized(
        **oversized_kwargs(
            cpu_entry=cpu_entry,
            gpu_entry={"peak": 3.0, "peak_exceeded": False},
            gpu_node_names={"gpu-0"},
            worker_nodes={"gpu-0"},
        )
    )
    assert out["nodes"] == [{"node": "gpu-0", "cpu_peak": 5.0, "gpu_node": True}]
    assert out["gpu_peak"] == 3.0

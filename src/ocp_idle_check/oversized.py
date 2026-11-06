"""The oversized classification: informational, never votes, never changes the exit code."""

from __future__ import annotations

from typing import Any

from .config import GPU_VARIANCE_FLOOR


def _gpu_untouched(entry: dict[str, Any]) -> bool | None:
    """Whether no GPU series anywhere ever met the variance floor - the
    minimum level the burst rule demands, just over DCGM's idle noise tail -
    meaning the GPU was never touched.  Utilization is a weak removal
    signal for GPU nodes: VRAM capacity is often why a particular GPU was
    picked, and a memory-resident workload computes little, so the
    acquittal bar sits at the floor rather than the peak threshold.  None
    when the entry carries no GPU level data at all (no DCGM metrics, no
    time window)."""
    per_node = entry.get("per_node") or []
    if per_node:
        return not any((e.get("peak") or 0.0) >= GPU_VARIANCE_FLOOR for e in per_node)
    peak = entry.get("peak")
    if peak is not None:
        return float(peak) < GPU_VARIANCE_FLOOR
    return None


def evaluate_oversized(
    *,
    status: str,
    quorum_driven: bool,
    cpu_entry: dict[str, Any],
    gpu_entry: dict[str, Any],
    gpu_node_names: set[str] | None,
    worker_nodes: set[str] | None,
) -> dict[str, Any]:
    """Whether an ACTIVE cluster carries at least one level-idle worker node:
    a node whose per-node CPU peak never crossed the peak threshold over the
    whole window, so it would have been judged inactive on its own without
    the variance check.  Variance firings are ignored by design - a node that
    only ever low-level-bursted still counts.

    GPU nodes additionally need their GPUs never touched: no GPU series
    anywhere ever met the variance floor, well under the peak threshold.
    DCGM series key by exporter pod IP, which does not join to node names,
    so one touched exporter blocks acquittal of all of them rather than
    risk naming a used GPU node as removable - and the bar is the floor
    because VRAM capacity is often why a particular GPU was picked and a
    memory-resident workload computes little, so utilization far under the
    peak threshold still means the GPU is in use.

    `gpu_peak` carries the hottest DCGM series cluster-wide whenever a named
    node is a GPU node - the acquittal's receipt, under the variance floor
    by construction, and not any one node's own peak (series
    cannot be attributed to node names).  Null everywhere else.

    A false result says only that: the nodes list is the evidence when it
    fires, and which gate stopped it otherwise is diagnostic detail that
    stays in the code, not the report."""
    if status != "ACTIVE":
        return {"result": False, "gpu_peak": None, "nodes": []}
    if quorum_driven:
        # Nothing fired - an UNKNOWN-driven ACTIVE is headed for the idle
        # pile, not the right-sizing one.
        return {"result": False, "gpu_peak": None, "nodes": []}
    if worker_nodes is None or gpu_node_names is None:
        # A failed node listing leaves liveness, roles, and GPU presence all
        # unknown - not the same as an empty set of candidates.
        return {"result": False, "gpu_peak": None, "nodes": []}
    cpu_per_node = cpu_entry.get("per_node") or []
    if not cpu_per_node:
        # Legacy fallback or instant-only CPU: no per-node peaks to judge.
        return {"result": False, "gpu_peak": None, "nodes": []}
    gpu_untouched = _gpu_untouched(gpu_entry)
    nodes: list[dict[str, Any]] = []
    for e in cpu_per_node:
        if e.get("peak_exceeded"):
            continue
        name = str(e.get("node") or "")
        if name not in worker_nodes:
            continue
        if name in gpu_node_names and not gpu_untouched:
            continue
        nodes.append({"node": name, "cpu_peak": e.get("peak"), "gpu_node": name in gpu_node_names})
    if not nodes:
        return {"result": False, "gpu_peak": None, "nodes": []}
    # A named GPU node implies the cluster-wide acquittal, so the pooled
    # peak - the hottest series anywhere - is its receipt.
    gpu_peak = gpu_entry.get("peak") if any(n["gpu_node"] for n in nodes) else None
    return {"result": True, "gpu_peak": gpu_peak, "nodes": nodes}

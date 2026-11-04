"""Node facts from oc listings and `oc adm top nodes`: GPU/worker sets, instants."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GpuNode:
    name: str
    vendor: str
    gpu_count: int
    instance_type: str


def gpu_nodes_from_items(items: list[dict[str, Any]]) -> list[GpuNode]:
    """Nodes with nvidia.com/gpu or amd.com/gpu capacity (as in the bash
    script's get_all_gpu_node_data)."""
    gpu_nodes: list[GpuNode] = []
    for item in items:
        meta = item.get("metadata", {})
        capacity = item.get("status", {}).get("capacity", {})
        labels = meta.get("labels", {})
        nvidia = capacity.get("nvidia.com/gpu")
        amd = capacity.get("amd.com/gpu")
        if nvidia is None and amd is None:
            continue
        if nvidia is not None:
            vendor, count = "NVIDIA", nvidia
        else:
            vendor, count = "AMD", amd
        try:
            gpu_count = int(count)
        except (TypeError, ValueError):
            gpu_count = 0
        gpu_nodes.append(
            GpuNode(
                name=str(meta.get("name", "")),
                vendor=vendor,
                gpu_count=gpu_count,
                instance_type=str(labels.get("node.kubernetes.io/instance-type", "")),
            )
        )
    return gpu_nodes


def node_cpu_cores(items: list[dict[str, Any]]) -> dict[str, float]:
    """Node name -> allocatable CPU cores, for the core-scaled peak
    threshold.  Kubernetes reports cores as "64" or millicores "64000m";
    unparsable values are skipped (the node then keeps the unscaled
    default threshold)."""
    cores: dict[str, float] = {}
    for item in items:
        name = str(item.get("metadata", {}).get("name", ""))
        raw = str((item.get("status", {}).get("capacity", {}) or {}).get("cpu", ""))
        if not name or not raw:
            continue
        try:
            cores[name] = float(raw[:-1]) / 1000.0 if raw.endswith("m") else float(raw)
        except ValueError:
            continue
    return cores


# Role labels that mark a node as control-plane capacity.  The CPU rule
# judges these by variance alone when the cluster has workers (see
# check_cpu): a busy control plane cannot be right-sized away, and its
# steady load is platform overhead, not workload activity.
MASTER_ROLE_LABELS = (
    "node-role.kubernetes.io/master",
    "node-role.kubernetes.io/control-plane",
)

# Role labels that mark a node as platform capacity rather than schedulable
# worker capacity.  A quiet node with one of these is expected, not
# right-sizing evidence: control-plane nodes cannot be removed, and infra
# nodes run required platform services.
NON_WORKER_ROLE_LABELS = (*MASTER_ROLE_LABELS, "node-role.kubernetes.io/infra")


def master_nodes_from_items(items: list[dict[str, Any]]) -> set[str]:
    """Names of nodes with master or control-plane role labels."""
    names: set[str] = set()
    for item in items:
        labels = item.get("metadata", {}).get("labels", {})
        if any(role in labels for role in MASTER_ROLE_LABELS):
            names.add(str(item.get("metadata", {}).get("name", "")))
    return names


def worker_nodes_from_items(items: list[dict[str, Any]]) -> set[str]:
    """Names of nodes without control-plane or infra role labels."""
    names: set[str] = set()
    for item in items:
        labels = item.get("metadata", {}).get("labels", {})
        if any(role in labels for role in NON_WORKER_ROLE_LABELS):
            continue
        names.add(str(item.get("metadata", {}).get("name", "")))
    return names


def node_instance_type(item: dict[str, Any]) -> str:
    labels = item.get("metadata", {}).get("labels", {})
    return str(labels.get("node.kubernetes.io/instance-type", ""))


def cluster_short_name(server: str) -> str:
    """First DNS label after "api." in the server URL (for machine naming)."""
    match = re.search(r"api\.([^:]+)", server)
    if not match:
        return ""
    return match.group(1).split(".")[0]


def instant_averages(top_lines: list[str]) -> tuple[float | None, float | None]:
    """Average CPU% (column 3) and MEM% (column 5) from `oc adm top nodes`."""
    cpu_values: list[float] = []
    mem_values: list[float] = []
    for line in top_lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            cpu_values.append(float(fields[2].rstrip("%")))
            mem_values.append(float(fields[4].rstrip("%")))
        except ValueError:
            continue
    cpu_avg = sum(cpu_values) / len(cpu_values) if cpu_values else None
    mem_avg = sum(mem_values) / len(mem_values) if mem_values else None
    return cpu_avg, mem_avg


def node_top_percentages(top_lines: list[str]) -> dict[str, tuple[float, float]]:
    """Per-node (CPU%, MEM%) from `oc adm top nodes` lines, keyed by node name.

    Parses the same columns as `instant_averages` (CPU% = column 3,
    MEM% = column 5); malformed rows are skipped.
    """
    stats: dict[str, tuple[float, float]] = {}
    for line in top_lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            stats[fields[0]] = (float(fields[2].rstrip("%")), float(fields[4].rstrip("%")))
        except ValueError:
            continue
    return stats


def fmt2(value: float | None) -> str | None:
    """Two-decimal string, matching the bash script's printf %.2f output."""
    return f"{value:.2f}" if value is not None else None


def _round2(value: float | None) -> float | None:
    """Two-decimal rounding for report fields, passing None through."""
    return round(value, 2) if value is not None else None

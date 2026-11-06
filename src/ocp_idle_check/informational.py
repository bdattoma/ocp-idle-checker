"""The non-voting informational sections of the verbose report."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import RECENT_ACTIVITY_RE, Config
from .criteria import event_text, event_timestamp
from .log import log_info
from .nodes import GpuNode, cluster_short_name, fmt2, node_instance_type
from .oc import Oc
from .prometheus import PrometheusClient


def instant_by_instance(series: list[dict[str, Any]] | None) -> dict[str, float]:
    """Reduce instant-query series to {instance label: value}; junk skipped."""
    out: dict[str, float] = {}
    for entry in series or []:
        instance = str(entry.get("metric", {}).get("instance", ""))
        try:
            value = float(entry["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            out[instance] = value
    return out


def average_matching(by_instance: dict[str, float], pattern: re.Pattern[str]) -> float | None:
    """Mean of the values whose instance label matches `pattern`.

    Prometheus `=~` matchers are fully anchored, so the Python equivalent
    of a node's `instance=~"<name>.*"` selector is `pattern.fullmatch`.
    """
    values = [value for instance, value in by_instance.items() if pattern.fullmatch(instance)]
    return sum(values) / len(values) if values else None


def _node_instance_re(name: str) -> re.Pattern[str]:
    """The regex for one node's `instance=~"<name>.*"` selector; the trailing
    `.*` matches the port suffix, and fullmatch mirrors Prometheus' fully
    anchored `=~` matching (see average_matching)."""
    return re.compile(f"{re.escape(name)}.*")


def _instance_alternation(names: list[str]) -> str:
    """PromQL alternation for the `instance=~` selector of several nodes, so
    one batched query can cover them all.

    The alternation lands inside a double-quoted PromQL string literal, where
    a lone backslash is an invalid escape sequence and fails the whole query;
    doubling them makes the server apply the re.escape pattern verbatim (RE2
    reads \\- and \\. as the literal characters)."""
    return "|".join(_node_instance_re(name).pattern.replace("\\", "\\\\") for name in names)


def windowed_cpu_by_node(
    cfg: Config, prom: PrometheusClient, names: list[str]
) -> dict[str, float | None]:
    """Windowed average CPU percent per oc node name.  One batched query over
    all `names` (`avg by (instance)` keeps the series separable so they map
    back per node) instead of a round trip per node; nodes with no matching
    series map to None."""
    pattern = _instance_alternation(names)
    query = (
        '(1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",'
        f'instance=~"{pattern}"}}[{cfg.time_window_minutes}m]))) * 100'
    )
    by_instance = instant_by_instance(prom.query_all(query))
    return {name: average_matching(by_instance, _node_instance_re(name)) for name in names}


def gpu_usage_section(
    cfg: Config, oc: Oc, prom: PrometheusClient, gpu_nodes: list[GpuNode]
) -> dict[str, Any]:
    """GPU node CPU/memory usage for the report (node resources, not GPU
    utilization - that now lives in the gpu criterion).

    Per-node instant numbers come from the single cached `adm top nodes`
    snapshot, and the windowed numbers from one batched query per metric
    over all GPU nodes (windowed_cpu_by_node for CPU; the memory query
    below batches the same way) instead of two round trips per node.
    """
    top = oc.top_node_map()
    cpu_current: list[float] = []
    mem_current: list[float] = []
    for node in gpu_nodes:
        stats = top.get(node.name)
        if stats is not None:
            cpu_current.append(stats[0])
            mem_current.append(stats[1])

    cpu_windowed: list[float] = []
    mem_windowed: list[float] = []
    if cfg.time_window_minutes > 0 and gpu_nodes:
        names = [node.name for node in gpu_nodes]
        cpu_by_node = windowed_cpu_by_node(cfg, prom, names)
        pattern = _instance_alternation(names)
        mem_by_instance = instant_by_instance(
            prom.query_all(
                "(1 - avg_over_time((avg by (instance) (node_memory_MemAvailable_bytes"
                f'{{instance=~"{pattern}"}}) / avg by (instance) (node_memory_MemTotal_bytes'
                f'{{instance=~"{pattern}"}}))[{cfg.time_window_minutes}m:])) * 100'
            )
        )
        for node in gpu_nodes:
            cpu = cpu_by_node[node.name]
            if cpu is not None:
                cpu_windowed.append(cpu)
            mem = average_matching(mem_by_instance, _node_instance_re(node.name))
            if mem is not None:
                mem_windowed.append(mem)

    def _avg(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        "cpu_current": fmt2(_avg(cpu_current)),
        "memory_current": fmt2(_avg(mem_current)),
        "cpu_windowed": fmt2(_avg(cpu_windowed)),
        "memory_windowed": fmt2(_avg(mem_windowed)),
    }


def ml_node_report(
    cfg: Config, oc: Oc, prom: PrometheusClient, items: list[dict[str, Any]] | None
) -> bool:
    """ML node usage display (informational, never votes).

    The instance-type pattern match runs in every mode because the return
    value feeds the "expensive nodes are idle" warning. The per-node usage
    numbers - one cached `adm top nodes` snapshot plus one batched windowed
    query over all matched nodes - are only gathered in verbose mode, the
    only mode that displays them.  A failed node listing (None) reports
    nothing and returns False - unknown is not absent.
    """
    if items is None:
        log_info("Node listing failed; ML/GPU node check skipped")
        return False
    pattern = re.compile(cfg.ml_node_pattern)
    ml_nodes = [item for item in items if pattern.search(node_instance_type(item))]
    if not ml_nodes:
        log_info("No ML/GPU instance-type nodes found")
        return False

    log_info(f"Found {len(ml_nodes)} ML/GPU instance-type nodes:")
    if not cfg.verbose:
        return True

    top = oc.top_node_map()
    windowed_cpu: dict[str, float | None] = {}
    if cfg.time_window_minutes > 0:
        names = [str(item.get("metadata", {}).get("name", "")) for item in ml_nodes]
        windowed_cpu = windowed_cpu_by_node(cfg, prom, names)
    for item in ml_nodes:
        name = str(item.get("metadata", {}).get("name", ""))
        instance = node_instance_type(item)
        cpu, mem = top.get(name, (None, None))
        windowed = windowed_cpu.get(name)
        log_info(
            f"  {name} ({instance}): instant cpu={fmt2(cpu)}% "
            f"mem={fmt2(mem)}%, windowed cpu={fmt2(windowed)}%"
        )
    return True


def recent_activity_report(oc: Oc, event_minutes: int, now: datetime | None = None) -> None:
    """Verbose-only recent pod activity display."""
    now = now or datetime.now(UTC)
    items = oc.events_all()
    if items is None:
        log_info(
            f"Pod-related events in the last {event_minutes} minutes: "
            "unknown (event listing failed)"
        )
        return
    cutoff = now - timedelta(minutes=event_minutes)
    recent: list[str] = []
    for item in items:
        timestamp = event_timestamp(item)
        if timestamp is None or timestamp < cutoff:
            continue
        line = event_text(item)
        if RECENT_ACTIVITY_RE.search(line):
            recent.append(line)
    log_info(f"Pod-related events in the last {event_minutes} minutes: {len(recent)}")
    for line in recent[-5:]:
        log_info(f"  {line}")


def gpu_machines_report(oc: Oc, server: str) -> None:
    """Verbose-only GPU machine display for clusters without GPU nodes."""
    short = cluster_short_name(server)
    if not short:
        return
    pattern = re.compile(f"{re.escape(short)}-.*-gpu-")
    machines = [
        m for m in oc.machines() if pattern.search(str(m.get("metadata", {}).get("name", "")))
    ]
    if machines:
        log_info(f"GPU machines exist in openshift-machine-api: {len(machines)}")
    else:
        log_info("No GPU machines found in openshift-machine-api")

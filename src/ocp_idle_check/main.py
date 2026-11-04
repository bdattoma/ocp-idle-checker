"""The run: concurrent criteria, verdict, oversized, reports, exit code."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from .config import parse_args
from .criteria import (
    CRITERIA_KEYS,
    MAX_COUNTED_UNKNOWNS,
    CheckOutcome,
    check_api,
    check_cpu,
    check_gpu,
    check_memory,
    check_operators,
    compute_verdict,
    counted_unknowns,
    criteria_summary,
    format_threshold_ratio,
    threshold_focus,
    utilization_veto,
)
from .informational import (
    gpu_machines_report,
    gpu_usage_section,
    ml_node_report,
    recent_activity_report,
)
from .log import GREEN, NC, RED, YELLOW, log_error, log_info, log_warn, set_verbose
from .nodes import (
    fmt2,
    gpu_nodes_from_items,
    instant_averages,
    master_nodes_from_items,
    node_cpu_cores,
    node_top_percentages,
    worker_nodes_from_items,
)
from .oc import Oc
from .oversized import evaluate_oversized
from .prometheus import PrometheusClient
from .report import export_csv, export_json


def run(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    set_verbose(cfg.verbose)

    if cfg.verbose:
        print("=" * 40, file=sys.stderr)
        print("  OpenShift Cluster Idle Detection", file=sys.stderr)
        print("=" * 40, file=sys.stderr)
        log_info(f"Time window: {cfg.time_window_minutes} minutes")
        log_info(f"Event history: {cfg.event_history_minutes} minutes")

    if shutil.which("oc") is None:
        log_error("oc command not found")
        return 2
    oc = Oc(verbose=cfg.verbose)
    if not oc.logged_in():
        log_error("Not logged in to an OpenShift cluster (oc whoami failed)")
        return 2

    prom = PrometheusClient(oc, token=cfg.token, verbose=cfg.verbose)
    server = oc.whoami_server()
    log_info(f"Cluster: {server}")
    log_info(f"Prometheus token: {'provided' if cfg.token else 'resolved via oc'}")

    node_items = oc.nodes()
    if node_items is None:
        # A failed node listing is not an empty cluster.  GPU presence is
        # unknown (the criterion reports UNKNOWN, counted), and the CPU
        # variance rule runs unfiltered - live_nodes=None keeps series from
        # deleted nodes, the conservative direction.
        live_nodes = None
        gpu_nodes = None
        node_cores = None
        variance_only_nodes: set[str] = set()
        log_warn("Node listing failed (oc error or timeout)")
    else:
        live_nodes = {str(item.get("metadata", {}).get("name", "")) for item in node_items}
        gpu_nodes = gpu_nodes_from_items(node_items)
        node_cores = node_cpu_cores(node_items)
        log_info(f"Nodes: {len(live_nodes)} ({len(gpu_nodes)} with GPUs)")
        # CPU judges control-plane nodes by variance alone when the cluster
        # has workers: a busy control plane cannot be right-sized away, so
        # its steady load is platform overhead, not workload activity.  A
        # cluster with no workers keeps the full rule - there the masters
        # are the workload.
        variance_only_nodes = (
            master_nodes_from_items(node_items) & live_nodes
            if worker_nodes_from_items(node_items)
            else set()
        )
        if variance_only_nodes:
            log_info(f"Variance-only CPU nodes (masters): {len(variance_only_nodes)}")
    top_lines = oc.top_nodes()
    instant_cpu, instant_mem = instant_averages(top_lines)
    if variance_only_nodes:
        # The instant accusation is worker-only too: masters' steady load
        # must not drag the pooled cluster average over the threshold.
        worker_cpu = [
            cpu
            for node, (cpu, _) in node_top_percentages(top_lines).items()
            if node not in variance_only_nodes
        ]
        if worker_cpu:
            instant_cpu = sum(worker_cpu) / len(worker_cpu)

    log_info(f"Instant cluster CPU: {fmt2(instant_cpu)}%, memory: {fmt2(instant_mem)}%")

    # --- CHECK 1-5: the five criteria, run concurrently ---
    # They are independent I/O-bound checks; only the operators check spawns
    # oc commands, and the Prometheus client resolves its endpoint and token
    # exactly once under its init lock. Results are consumed in fixed order
    # so the log output matches the old sequential run line for line, and an
    # exception re-raises at the same check it would have in that run.
    now = datetime.now(UTC)
    checks: list[tuple[str, str, Callable[[], CheckOutcome]]] = [
        (
            "CPU Utilization",
            "CPU",
            lambda: check_cpu(cfg, prom, live_nodes, instant_cpu, node_cores, variance_only_nodes),
        ),
        ("Memory Utilization", "Memory", lambda: check_memory(cfg, prom, instant_mem, live_nodes)),
        ("API Server Activity", "API Server", lambda: check_api(cfg, prom)),
        ("GPU Utilization", "GPU", lambda: check_gpu(cfg, prom, gpu_nodes)),
        ("Operators", "Operators", lambda: check_operators(cfg, oc, now)),
    ]
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = [pool.submit(run_check) for _, _, run_check in checks]
        outcomes: list[CheckOutcome] = []
        for number, ((title, label, _), future) in enumerate(zip(checks, futures, strict=True), 1):
            log_info(f"--- CHECK {number}: {title} ---")
            outcome = future.result()
            log_info(f"{label} Result: {outcome.result}")
            outcomes.append(outcome)
    cpu_outcome, memory_outcome, api_outcome, gpu_outcome, operators_outcome = outcomes

    # --- Informational (never votes) ---
    ml_found = False
    if cfg.check_ml_nodes:
        ml_found = ml_node_report(cfg, oc, prom, node_items)
    if cfg.verbose:
        recent_activity_report(oc, cfg.event_history_minutes)
        if gpu_nodes is not None and not gpu_nodes:
            gpu_machines_report(oc, server)

    # --- GPU report section (informational usage numbers) ---
    flavors = sorted({f"{n.vendor}({n.instance_type})" for n in gpu_nodes or []})
    gpu_section: dict[str, Any] = {
        "has_gpu_nodes": bool(gpu_nodes) if gpu_nodes is not None else None,
        "node_count": len(gpu_nodes) if gpu_nodes is not None else None,
        "flavors": ",".join(flavors) if flavors else None,
        "node_age": None,
        "usage": gpu_usage_section(cfg, oc, prom, gpu_nodes)
        if gpu_nodes
        else {
            "cpu_current": None,
            "memory_current": None,
            "cpu_windowed": None,
            "memory_windowed": None,
        },
    }

    # --- Verdict ---
    outcomes = [cpu_outcome, memory_outcome, api_outcome, gpu_outcome, operators_outcome]
    total, met, threshold, status, exit_code = compute_verdict(outcomes)
    unknowns = counted_unknowns(outcomes)
    if len(unknowns) > MAX_COUNTED_UNKNOWNS:
        # Too many UNKNOWNs (counted) to judge on: error rather than issue a
        # verdict built on too little data.
        log_error(
            f"Too many UNKNOWN criteria ({', '.join(unknowns)}) - not enough data for a verdict"
        )
        status, exit_code = "ERROR", 2
        focus = None
    else:
        focus = threshold_focus(outcomes, status)
        # Scoped utilization veto - see utilization_veto for the rationale.
        if status == "IDLE" and utilization_veto(
            node_items is None, cpu_outcome.result, gpu_outcome.result
        ):
            busy = "CPU" if cpu_outcome.result == "ACTIVE" else "GPU"
            log_warn(
                f"Node listing failed and the {busy} criterion is ACTIVE - "
                "utilization veto overrides the idle quorum"
            )
            status, exit_code = "ACTIVE", 0
            focus = None
    oversized = evaluate_oversized(
        status=status,
        quorum_driven=bool(focus and focus["quorum_driven"]),
        cpu_entry=cpu_outcome.entry,
        gpu_entry=gpu_outcome.entry,
        gpu_node_names={n.name for n in gpu_nodes} if gpu_nodes is not None else None,
        worker_nodes=worker_nodes_from_items(node_items) if node_items is not None else None,
    )

    # `criteria` keeps each criterion's headline fields so renderers that
    # print it verbatim (the Jenkins report's Metrics column) stay compact;
    # the diagnostics behind those fields are not exported.
    criteria: dict[str, Any] = {"total": total, "met": met, "threshold": threshold}
    for key, outcome in zip(CRITERIA_KEYS, outcomes, strict=True):
        criteria[key] = criteria_summary(key, outcome.entry)
    criteria["driver"] = focus

    report: dict[str, Any] = {
        "timestamp": now.isoformat(timespec="seconds"),
        "timestamp_human": now.strftime("%a %b %d %H:%M:%S UTC %Y"),
        "cluster": server,
        "status": status,
        "exit_code": exit_code,
        "configuration": {
            "time_window_minutes": cfg.time_window_minutes,
            "cpu_threshold": cfg.cpu_idle_threshold,
            "memory_threshold": cfg.memory_idle_threshold,
            "api_threshold": cfg.api_threshold,
            "operator_age_threshold_days": cfg.operator_age_days,
            "operator_event_window_hours": cfg.operator_event_hours,
            "operator_exclude_prefixes": cfg.operator_exclude_prefixes,
            "cpu_peak_threshold": cfg.cpu_peak_threshold,
            "cpu_variance_ratio": cfg.cpu_variance_ratio,
            "gpu_peak_threshold": cfg.gpu_peak_threshold,
            "gpu_variance_ratio": cfg.gpu_variance_ratio,
            "api_variance_ratio": cfg.api_variance_ratio,
            "memory_variance_points": cfg.memory_variance_points,
            "memory_variance_windows": cfg.memory_variance_windows,
        },
        "criteria": criteria,
        "gpu": gpu_section,
        "oversized": oversized,
    }
    if status == "ERROR":
        report["error"] = f"too many UNKNOWN criteria: {', '.join(unknowns)}"

    # --- Summary ---
    if cfg.verbose:
        print("-" * 40, file=sys.stderr)
        log_info(f"Idle criteria met: {met} / {total} (threshold: {threshold})")
        for name, outcome in zip(CRITERIA_KEYS, outcomes, strict=True):
            log_info(f"  {name}: {outcome.result}")
        if focus is not None:
            ratio = format_threshold_ratio(focus["threshold_ratio"])
            if status == "IDLE":
                log_info(f"Closest to active: {focus['label']} at {ratio} of threshold")
            elif focus["quorum_driven"]:
                log_info(
                    f"Most active: {focus['label']} at {ratio} of threshold "
                    "- no criterion fired; UNKNOWN votes deprived the quorum"
                )
            else:
                log_info(f"Most active: {focus['label']} at {ratio} of threshold")
        if oversized["result"]:
            for node in oversized["nodes"]:
                log_info(f"Level-idle node: {node['node']} (CPU peak {node['cpu_peak']}%)")
    # node_items None = listing failed = GPU/ML presence unknown; "unknown"
    # is not "no expensive nodes", so the cleanup warning fires on it too.
    if status == "IDLE" and (gpu_nodes or ml_found or node_items is None):
        log_warn("Expensive GPU/ML nodes are idle - review before cleanup")

    if cfg.verbose:
        if status == "IDLE":
            print(f"{GREEN}========================================{NC}", file=sys.stderr)
            print(f"{GREEN}  Cluster Status: IDLE{NC}", file=sys.stderr)
            print(f"{GREEN}========================================{NC}", file=sys.stderr)
        elif status == "ERROR":
            print(f"{RED}========================================{NC}", file=sys.stderr)
            print(f"{RED}  Cluster Status: ERROR{NC}", file=sys.stderr)
            print(f"{RED}========================================{NC}", file=sys.stderr)
        else:
            print(f"{YELLOW}========================================{NC}", file=sys.stderr)
            print(f"{YELLOW}  Cluster Status: ACTIVE{NC}", file=sys.stderr)
            print(f"{YELLOW}========================================{NC}", file=sys.stderr)

    # Quiet-mode result lines (stdout, matching the bash script's format).
    print(f"CPU: {cpu_outcome.result}")
    print(f"Memory: {memory_outcome.result}")
    if api_outcome.result != "UNKNOWN":
        print(f"API Server: {api_outcome.result}")
    print(f"GPU: {gpu_outcome.result}")
    print(f"Operators: {operators_outcome.result}")
    # The oversized classification is informational: it prints only when it
    # fires, so output for every other cluster stays byte-identical.
    if oversized["result"]:
        count = len(oversized["nodes"])
        plural = "" if count == 1 else "s"
        print(f"Oversized: true ({count} level-idle node{plural})")
    # The STATUS line keeps its "STATUS: <verdict>" prefix - parsers key on
    # it - and gains the boundary criterion as an annotation.
    note = ""
    if focus is not None:
        ratio = format_threshold_ratio(focus["threshold_ratio"])
        if status == "IDLE":
            note = f" (closest to active: {focus['label']} at {ratio} of threshold)"
        elif focus["quorum_driven"]:
            note = f" (UNKNOWN-driven; {focus['label']} at {ratio} of threshold)"
        else:
            note = f" ({focus['label']} at {ratio} of threshold)"
    elif status == "ERROR":
        note = f" (too many UNKNOWN criteria: {', '.join(unknowns)})"
    print(f"STATUS: {status}{note}")

    if cfg.csv_path is not None:
        export_csv(report, cfg.csv_path)
        log_info(f"CSV exported to {cfg.csv_path}")
    if cfg.json_path is not None:
        export_json(report, cfg.json_path)
        log_info(f"JSON exported to {cfg.json_path}")

    return exit_code

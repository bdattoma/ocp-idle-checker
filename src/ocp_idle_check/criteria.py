"""The five idle criteria, the 80% verdict, and the too-many-UNKNOWNs gate."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import (
    API_RANGE_QUERY,
    API_SHORT_RATE_QUERY,
    API_WINDOW_RATE_QUERY,
    CPU_RANGE_QUERY,
    CPU_VARIANCE_FLOOR,
    GPU_RANGE_QUERY,
    GPU_VARIANCE_FLOOR,
    MEMORY_RANGE_QUERY,
    OPERATOR_EVENT_RE,
    OPERATOR_EVENT_THRESHOLD,
    OPERATOR_WORKLOAD_KINDS,
    POD_RE,
    VARIANCE_SOURCE,
    Config,
    cpu_peak_threshold,
)
from .log import log_warn
from .memory_steps import (
    MemoryStepStats,
    _memory_steps_entry,
    evaluate_memory_steps_by_node,
    summarize_memory_steps,
)
from .nodes import GpuNode, fmt2
from .oc import Oc
from .prometheus import PrometheusClient, _series_points
from .variance import (
    VarianceStats,
    _round2,
    _threshold_ratio,
    _variance_entry,
    _variance_numbers,
    _variance_threshold_ratio,
    evaluate_variance,
    evaluate_variance_by_node,
    summarize_variance,
    variance_required_peak,
)


@dataclass(frozen=True)
class CheckOutcome:
    """One criterion's verdict plus its detail entry; the report projects the
    entry's headline fields into the summary `criteria` section."""

    result: str  # IDLE | ACTIVE | UNKNOWN | N/A
    entry: dict[str, Any]
    counted: bool  # counts toward the 80% denominator
    # UNKNOWN declared for absent windowed data (a quiet instant that cannot
    # acquit the window), not a failed read: it stays counted but does not
    # count toward the too-many-UNKNOWNs gate.
    no_data: bool = False


CRITERIA_KEYS = ("cpu", "memory", "api_server", "gpu", "operators")
CRITERIA_LABELS = {
    "cpu": "CPU",
    "memory": "Memory",
    "api_server": "API Server",
    "gpu": "GPU",
    "operators": "Operators",
}

# The fields each criterion's entry keeps in the report's `criteria` section.
# Renderers that print `criteria` verbatim - the Jenkins report's Metrics
# column - must stay compact, so the entry is projected down to its headline
# fields; the diagnostics behind them are not exported.
CRITERIA_SUMMARY_FIELDS: dict[str, frozenset[str]] = {
    "cpu": frozenset({"result", "value", "threshold_ratio"}),
    "memory": frozenset({"result", "value", "threshold_ratio"}),
    "api_server": frozenset({"result", "value", "threshold_ratio"}),
    "gpu": frozenset({"result", "value", "threshold_ratio"}),
    "operators": frozenset({"result", "value", "threshold_ratio", "age_days", "events"}),
}


def criteria_summary(key: str, entry: dict[str, Any]) -> dict[str, Any]:
    """The headline view of a criterion's entry for the report's `criteria`
    section - everything a renderer printing `criteria` verbatim should see."""
    keep = CRITERIA_SUMMARY_FIELDS[key]
    return {k: v for k, v in entry.items() if k in keep}


def format_threshold_ratio(heat: float) -> str:
    """A threshold ratio as a percentage of the level that fires it; the
    floorless zero-baseline fire (no finite ratio) reads as 999%+."""
    if math.isinf(heat):
        return "999%+"
    return f"{heat * 100:.0f}%"


def window_bounds(cfg: Config) -> tuple[int, int]:
    """(start, end) unix seconds spanning the configured time window."""
    end_s = int(time.time())
    return end_s - cfg.time_window_minutes * 60, end_s


def check_cpu(
    cfg: Config,
    prom: PrometheusClient,
    live_nodes: set[str] | None,
    instant_cpu: float | None,
    node_cores: dict[str, float] | None = None,
    variance_only_nodes: set[str] | None = None,
) -> CheckOutcome:
    """CPU criterion: variance rule on 15-minute windows, with the
    one-directional instant override.  An unavailable range matrix is not
    smoothed over with the legacy window average: it falls to the instant
    accusation or UNKNOWN.

    `variance_only_nodes` names nodes judged by the variance rule alone -
    the control-plane nodes of a cluster that has workers (the caller
    passes an empty set otherwise).  Their steady load is platform
    overhead that cannot be right-sized away, so neither the absolute
    peak threshold nor the instant accusation may accuse on their
    behalf; only a burst against their own baseline does.  The caller
    keeps the instant itself worker-only for the same reason."""
    entry: dict[str, Any] = {"result": "UNKNOWN", "value": None, "threshold_ratio": None}
    stats: VarianceStats | None = None
    per_node: list[VarianceStats] = []
    read_failed = False

    if cfg.time_window_minutes > 0:
        start_s, end_s = window_bounds(cfg)
        series = prom.query_range(CPU_RANGE_QUERY, start_s, end_s)
        if series is None:
            # The read itself failed (request error or non-success status),
            # unlike an empty result - but a busy instant can still accuse,
            # so the branches below decide the verdict; only a quiet
            # no-evidence outcome becomes a failed-read UNKNOWN.
            read_failed = True
            entry["reason"] = "range query failed (Prometheus error or timeout)"
        else:
            # Each node's windows are judged against that node's own trough,
            # with series from deleted nodes dropped: their history is real but
            # says nothing about the cluster as it stands, and a steady busy
            # node pooled with a quiet sibling is two steady nodes, not a burst.
            # The absolute peak threshold scales with the node's core count -
            # it is hard to max out a large machine - and the curve's handoff
            # point moves with it.  An empty matrix is not smoothed over with
            # the legacy window average: it means no granular evidence came
            # back, and the no-data paths below decide.
            per_node = evaluate_variance_by_node(
                series,
                peak_threshold=cfg.cpu_peak_threshold,
                ratio_threshold=cfg.cpu_variance_ratio,
                floor=CPU_VARIANCE_FLOOR,
                baseline_full=cfg.cpu_peak_threshold / cfg.cpu_variance_ratio,
                live_nodes=live_nodes,
                baseline_mode="trough",
                node_peak_thresholds=(
                    {n: cpu_peak_threshold(c) for n, c in node_cores.items()}
                    if node_cores
                    else None
                ),
                variance_only_nodes=variance_only_nodes,
            )
            if per_node:
                stats = summarize_variance(per_node)
    if variance_only_nodes:
        entry["variance_only_nodes"] = sorted(variance_only_nodes)

    active = False
    source = None
    if stats is not None:
        active = stats.active
        source = VARIANCE_SOURCE
        entry.update(_variance_entry(stats, per_node))
    elif instant_cpu is not None and instant_cpu >= cfg.cpu_idle_threshold:
        # No windowed data, and the snapshot is busy: direct evidence of
        # activity, the direction the override below already allows.
        active = True
        source = "instant"
    elif instant_cpu is not None and cfg.time_window_minutes <= 0:
        # Windowless run: the instant is the whole question here, not a
        # stand-in for missing window evidence.
        active = False
        source = "instant"
    else:
        # A window was configured but no windowed data came back, and no
        # instant is busy: either no instant at all, or a quiet one - and
        # one quiet sample cannot acquit the window.  The override below
        # lets an instant accuse but never acquit; the no-data path is not
        # looser than that.  UNKNOWN, still counted (bash parity for the
        # no-instant-at-all case): a declared no-data verdict when the query
        # came back empty, a failed read - gate-eligible - when it did not.
        entry["value"] = fmt2(instant_cpu)
        entry["instant"] = instant_cpu
        if instant_cpu is not None:
            entry["source"] = "instant"
        return CheckOutcome("UNKNOWN", entry, counted=True, no_data=not read_failed)

    # One-directional instant override: a live spike beats an idle verdict.
    instant_override = False
    if not active and instant_cpu is not None and instant_cpu >= cfg.cpu_idle_threshold:
        active = True
        instant_override = True

    # Threshold ratio: the reading closest to firing, as a fraction of the
    # level that fires it.  Every firing path counts - the hottest node's
    # windows against the peak threshold and its own required peak, and the
    # instant accusation - so the max over nodes and paths is the heat.
    heat: float | None = None
    if per_node:
        heat = max(_variance_threshold_ratio(s, cfg.cpu_peak_threshold) for s in per_node)
    if instant_cpu is not None:
        instant_heat = _threshold_ratio(instant_cpu, cfg.cpu_idle_threshold)
        heat = instant_heat if heat is None else max(heat, instant_heat)
    entry["threshold_ratio"] = _round2(heat)

    value = stats.peak if stats is not None else instant_cpu
    entry["result"] = "ACTIVE" if active else "IDLE"
    entry["value"] = fmt2(value)
    entry["instant"] = instant_cpu
    entry["instant_override"] = instant_override
    entry["source"] = source
    return CheckOutcome(entry["result"], entry, counted=True)


def check_memory(
    cfg: Config,
    prom: PrometheusClient,
    instant_mem: float | None,
    live_nodes: set[str] | None,
) -> CheckOutcome:
    """Memory criterion: a movement rule on per-node 15-minute window
    averages.

    ACTIVE when any live node's windowed memory rose more than
    cfg.memory_variance_points across cfg.memory_variance_windows
    consecutive windows - a level shift in absolute points rather than a
    ratio, because a fixed allocation is fixed points whatever the
    baseline and the slow page-cache drift that pads every idle cluster's
    window average moves too gradually to clear the bar.  The instant
    reading no longer overrides an IDLE verdict: single-snapshot
    deviations of 5+ points land on idle and active cluster nodes at the
    same rate, so the snapshot rides the report as information.  Only
    with no windowed data at all does the instant vote, in the bash
    direction - busy votes ACTIVE, quiet reads UNKNOWN - because one
    sample can accuse the window, never acquit it (see check_cpu).  The
    legacy window average is not used as a fallback: no matrix means no
    granular evidence, and the no-data paths decide.
    """
    entry: dict[str, Any] = {"result": "UNKNOWN", "value": None, "threshold_ratio": None}
    stats: MemoryStepStats | None = None
    per_node: list[MemoryStepStats] = []
    read_failed = False

    if cfg.time_window_minutes > 0:
        start_s, end_s = window_bounds(cfg)
        series = prom.query_range(MEMORY_RANGE_QUERY, start_s, end_s)
        if series is None:
            # The read itself failed, unlike an empty result (see check_cpu):
            # the branches below still decide - a busy instant can accuse -
            # and only a quiet no-evidence outcome becomes a failed-read
            # UNKNOWN, gate-eligible.
            read_failed = True
            entry["reason"] = "range query failed (Prometheus error or timeout)"
        else:
            # Movement is judged per node and only on live ones, like the CPU
            # variance rule: a deleted node's refill says nothing about the
            # cluster as it stands.
            per_node = evaluate_memory_steps_by_node(
                series,
                points_threshold=cfg.memory_variance_points,
                span_windows=cfg.memory_variance_windows,
                live_nodes=live_nodes,
            )

    active = False
    source: str | None = None
    if per_node:
        stats = summarize_memory_steps(per_node)
        active = stats.rise_exceeded
        source = VARIANCE_SOURCE
        entry.update(_memory_steps_entry(stats, per_node))
    elif instant_mem is not None and instant_mem >= cfg.memory_idle_threshold:
        # No windowed data, and the snapshot is busy: direct evidence of
        # activity - the only direction an instant may vote.
        active = True
        source = "instant"
    elif instant_mem is not None and cfg.time_window_minutes <= 0:
        # Windowless run: the instant is the whole question here, not a
        # stand-in for missing window evidence.
        source = "instant"
    else:
        # A window was configured but no windowed data came back, and no
        # instant is busy: either no instant at all, or a quiet one, which
        # cannot acquit the window (see check_cpu).  UNKNOWN, still counted:
        # a declared no-data verdict when the query came back empty, a
        # failed read - gate-eligible - when it did not.
        entry["value"] = fmt2(instant_mem)
        entry["instant"] = instant_mem
        if instant_mem is not None:
            entry["source"] = "instant"
        return CheckOutcome("UNKNOWN", entry, counted=True, no_data=not read_failed)

    # Threshold ratio: the reading closest to firing, as a fraction of the
    # level that fires it - the hottest node's rise against the points
    # threshold, or on the no-data path the instant accusation.  The
    # instant does not count on the windowed paths: it cannot fire there,
    # so a ratio at or past 1 must mean something did.
    heat: float | None = None
    if per_node:
        heat = max(_threshold_ratio(s.max_rise, cfg.memory_variance_points) for s in per_node)
    elif instant_mem is not None:
        heat = _threshold_ratio(instant_mem, cfg.memory_idle_threshold)
    entry["threshold_ratio"] = _round2(heat)

    value = stats.max_rise if stats is not None else instant_mem
    entry["result"] = "ACTIVE" if active else "IDLE"
    entry["value"] = fmt2(value)
    entry["instant"] = instant_mem
    entry["source"] = source
    return CheckOutcome(entry["result"], entry, counted=True)


def check_api(cfg: Config, prom: PrometheusClient) -> CheckOutcome:
    """API criterion: window-average rate against the threshold, plus a
    variance detector (peak vs median of 15-minute windows)."""
    entry: dict[str, Any] = {"result": "UNKNOWN", "value": None, "threshold_ratio": None}
    if cfg.time_window_minutes > 0:
        avg = prom.query(API_WINDOW_RATE_QUERY.format(minutes=cfg.time_window_minutes))
        start_s, end_s = window_bounds(cfg)
        series = prom.query_range(API_RANGE_QUERY, start_s, end_s)
        variance_points = _series_points(series)
        stats: VarianceStats | None = None
        if variance_points:
            # Only the variance branch applies here; the absolute-threshold
            # branch is the windowed average checked above.
            stats = evaluate_variance(
                variance_points,
                ratio_threshold=cfg.api_variance_ratio,
                baseline_full=cfg.api_threshold / cfg.api_variance_ratio,
            )
    else:
        avg = prom.query(API_SHORT_RATE_QUERY)
        stats = None

    if avg is None and stats is None:
        return CheckOutcome("UNKNOWN", entry, counted=False)

    active = (avg is not None and avg >= cfg.api_threshold) or (
        stats is not None and stats.variance_exceeded
    )
    # Threshold ratio: the windowed average against the request threshold,
    # and the variance peak against its baseline-scaled requirement (the
    # API rule has no absolute peak branch).
    heat = _threshold_ratio(avg, cfg.api_threshold) if avg is not None else None
    if stats is not None:
        entry.update(_variance_numbers(stats, prefix="variance_"))
        entry["variance_active"] = stats.variance_exceeded
        variance_heat = _variance_threshold_ratio(stats, None)
        heat = variance_heat if heat is None else max(heat, variance_heat)
    entry["result"] = "ACTIVE" if active else "IDLE"
    entry["value"] = fmt2(avg)
    entry["threshold_ratio"] = _round2(heat)
    return CheckOutcome(entry["result"], entry, counted=True)


def check_gpu(cfg: Config, prom: PrometheusClient, gpu_nodes: list[GpuNode] | None) -> CheckOutcome:
    """GPU criterion: variance rule on DCGM GPU utilization.  N/A (not
    counted) without GPU nodes or DCGM metrics; UNKNOWN (counted) when the
    node listing itself failed - a failed listing is not an empty one."""
    entry: dict[str, Any] = {"result": "N/A", "value": None, "threshold_ratio": None}
    if gpu_nodes is None:
        entry["result"] = "UNKNOWN"
        entry["reason"] = "node listing failed (oc error or timeout)"
        return CheckOutcome("UNKNOWN", entry, counted=True)
    if not gpu_nodes:
        entry["reason"] = "no GPU nodes"
        return CheckOutcome("N/A", entry, counted=False)
    if cfg.time_window_minutes <= 0:
        entry["reason"] = "no time window"
        return CheckOutcome("N/A", entry, counted=False)

    start_s, end_s = window_bounds(cfg)
    series = prom.query_range(GPU_RANGE_QUERY, start_s, end_s)
    # Grouped per node (per exporter) but with no live filter, unlike cpu:
    # a deleted node's DCGM samples are still activity.
    per_node = evaluate_variance_by_node(
        series,
        peak_threshold=cfg.gpu_peak_threshold,
        ratio_threshold=cfg.gpu_variance_ratio,
        floor=GPU_VARIANCE_FLOOR,
        baseline_full=cfg.gpu_peak_threshold / cfg.gpu_variance_ratio,
        baseline_mode="trough",
    )

    stats: VarianceStats | None = None
    source = VARIANCE_SOURCE
    if per_node:
        stats = summarize_variance(per_node)
    else:
        # Aggregate fallback: whole-window max/avg per series.  All exporter
        # series are pooled - a deleted node's samples are still activity.
        peaks = prom.query_values(
            f"max_over_time(DCGM_FI_DEV_GPU_UTIL[{cfg.time_window_minutes}m])"
        )
        avgs = prom.query_values(f"avg_over_time(DCGM_FI_DEV_GPU_UTIL[{cfg.time_window_minutes}m])")
        if peaks:
            peak = max(peaks)
            baseline = sum(avgs) / len(avgs) if avgs else 0.0
            ratio = peak / baseline if baseline > 0 else None
            required_peak = variance_required_peak(
                baseline,
                cfg.gpu_variance_ratio,
                GPU_VARIANCE_FLOOR,
                cfg.gpu_peak_threshold / cfg.gpu_variance_ratio,
            )
            stats = VarianceStats(
                points=len(peaks) + len(avgs),
                peak=peak,
                baseline=baseline,
                ratio=ratio,
                required_ratio=required_peak / baseline if baseline > 0 else None,
                required_peak=required_peak,
                peak_exceeded=peak > cfg.gpu_peak_threshold,
                variance_exceeded=peak > required_peak,
            )
            source = "window aggregates fallback"
        else:
            entry["reason"] = "no DCGM metrics"
            return CheckOutcome("N/A", entry, counted=False)

    # Threshold ratio: the hottest node's peak against the peak threshold
    # and its own required peak (the fallback path has one pooled
    # evaluation instead).
    heat = (
        max(_variance_threshold_ratio(s, cfg.gpu_peak_threshold) for s in per_node)
        if per_node
        else _variance_threshold_ratio(stats, cfg.gpu_peak_threshold)
    )
    entry["result"] = "ACTIVE" if stats.active else "IDLE"
    entry["value"] = fmt2(stats.peak)
    entry.update(_variance_entry(stats, per_node))
    entry["source"] = source
    entry["threshold_ratio"] = _round2(heat)
    return CheckOutcome(entry["result"], entry, counted=True)


def parse_k8s_timestamp(ts: str) -> datetime | None:
    """Parse a Kubernetes timestamp ("2026-10-01T12:34:56Z"); None when
    absent or malformed.  Naive values (oc never emits them, but they are
    never trusted either) are read as UTC so callers' arithmetic stays
    well-defined."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)  # 3.11+: "Z" is accepted directly
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def event_timestamp(item: dict[str, Any]) -> datetime | None:
    """An event's effective timestamp: lastTimestamp, else firstTimestamp,
    else eventTime - the tabular printer's LAST SEEN computation, which is
    what the bash checker grepped.  The legacy core/v1 recorder sets the
    first two; an event recorded through the newer events API carries only
    eventTime (microsecond precision)."""
    return (
        parse_k8s_timestamp(item.get("lastTimestamp"))
        or parse_k8s_timestamp(item.get("firstTimestamp"))
        or parse_k8s_timestamp(item.get("eventTime"))
    )


def event_text(item: dict[str, Any]) -> str:
    """An event's matchable text on one line: type, reason, object kind,
    object name, and message - the fields the event regexes run over, and
    what the recent-activity report prints.  Whitespace is collapsed so a
    message's embedded newlines cannot break the line apart."""
    involved = item.get("involvedObject") or {}
    parts = (
        item.get("type"),
        item.get("reason"),
        involved.get("kind"),
        involved.get("name"),
        item.get("message"),
    )
    return " ".join(" ".join(str(part) for part in parts if part).split())


def check_operators(cfg: Config, oc: Oc, now: datetime | None = None) -> CheckOutcome:
    """Operators criterion: controller age and recent reconciliation events.

    Scope is every namespace on the cluster except the excluded platform
    prefixes, plus the configured namespaces (an always-include override, so
    an ODH namespace that happens to match a prefix still counts).  A unit
    is a pod matching POD_RE, aged by its ReplicaSet's creation time when it
    has one - a rollout resets both, a drain only the pod, and the
    ReplicaSet age is the "last deliberate change" signal - and by the pod's
    own creation time otherwise; replicas of one ReplicaSet are a single
    unit.  The criterion votes ACTIVE when the MEDIAN unit age is below the
    threshold (a majority redeployed recently; the old oldest-pod rule was
    blind to a partial rollout masked by one stale survivor) or when
    reconciliation events inside the event window reach
    OPERATOR_EVENT_THRESHOLD.  Events about workload machinery (pods,
    deployments, ...) are excluded from that count: node drains manufacture
    them by the dozen, and the age half already treats drains as noise.
    An event recorded through the newer events API carries only eventTime;
    it counts at that, the same LAST SEEN fallback the tabular printer -
    and so the bash checker - applied.

    A failed cluster-wide listing (oc error or timeout) is UNKNOWN, never an
    empty result: a failed pod listing must not read as "no controller
    pods", and a failed event listing must not read as a quiet cluster.
    """
    now = now or datetime.now(UTC)
    entry: dict[str, Any] = {"result": "N/A", "age_days": 0, "threshold_ratio": None}

    always = {ns.strip() for ns in cfg.operator_namespaces.split(",") if ns.strip()}
    excludes = [p.strip() for p in cfg.operator_exclude_prefixes.split(",") if p.strip()]

    def in_scope(namespace: str) -> bool:
        return namespace in always or not namespace.startswith(tuple(excludes))

    pods = oc.pods_all()
    if pods is None:
        entry["result"] = "UNKNOWN"
        entry["reason"] = "pod listing failed (oc error or timeout)"
        return CheckOutcome("UNKNOWN", entry, counted=False)

    # Unit key: (namespace, kind, name).  A "replicaset" key collapses the
    # replicas of one ReplicaSet into a single unit.
    units: dict[tuple[str, str, str], datetime | None] = {}
    rs_wanted = False
    for line in pods:
        fields = line.split("\t")
        if len(fields) != 5:
            # Unreachable unless oc's jsonpath output changes shape; without
            # the warning it would read as "no controller pods".
            log_warn(f"skipping malformed pod row ({len(fields)} fields): {line!r}")
            continue
        namespace, pod, owner_kind, owner_name, created_raw = fields
        if not in_scope(namespace) or not POD_RE.search(pod):
            continue
        pod_created = parse_k8s_timestamp(created_raw)
        if owner_kind == "ReplicaSet" and owner_name:
            rs_wanted = True
            key = (namespace, "replicaset", owner_name)
            # Provisional pod age: it stands until the ReplicaSet's own
            # creation time is looked up, and remains the fallback for a
            # ReplicaSet deleted mid-rollout whose pods outlive it - the
            # oldest replica is the closest proxy for the missing age.
            if pod_created is None:
                units.setdefault(key, None)
            elif units.get(key) is None or pod_created < units[key]:
                units[key] = pod_created
        elif pod_created is not None:
            units[(namespace, "pod", pod)] = pod_created

    if not units:
        entry["reason"] = "no controller pods in any scanned namespace"
        return CheckOutcome("N/A", entry, counted=False)

    if rs_wanted:
        # Only ReplicaSets owning a live in-scope pod are read: the cluster
        # also holds old scaled-to-zero revisions whose ages would read
        # "install date", not "last change".
        rs_rows = oc.replicasets_all()
        if rs_rows is None:
            entry["result"] = "UNKNOWN"
            entry["reason"] = "ReplicaSet listing failed (oc error or timeout)"
            return CheckOutcome("UNKNOWN", entry, counted=False)
        for line in rs_rows:
            fields = line.split("\t")
            if len(fields) != 3:
                # Unreachable unless oc's jsonpath output changes shape; the
                # unit keeps its pod-age fallback rather than failing loudly.
                log_warn(f"skipping malformed ReplicaSet row ({len(fields)} fields): {line!r}")
                continue
            rs_created = parse_k8s_timestamp(fields[2])
            if rs_created is None:
                continue
            key = (fields[0], "replicaset", fields[1])
            if key in units:
                units[key] = rs_created

    units = {key: created for key, created in units.items() if created is not None}
    if not units:
        entry["reason"] = "no controller pods with a readable creation time"
        return CheckOutcome("N/A", entry, counted=False)

    event_items = oc.events_all()
    if event_items is None:
        # Reading a failed listing as zero events would vote IDLE for old,
        # unverified operators.
        entry["result"] = "UNKNOWN"
        entry["reason"] = "event listing failed (oc error or timeout)"
        return CheckOutcome("UNKNOWN", entry, counted=False)
    events = 0
    excluded_workload = 0
    window_start = now - timedelta(hours=cfg.operator_event_hours)
    for item in event_items:
        namespace = (item.get("metadata") or {}).get("namespace") or ""
        if not in_scope(namespace):
            continue
        timestamp = event_timestamp(item)
        if timestamp is None or timestamp < window_start:
            continue
        if not OPERATOR_EVENT_RE.search(event_text(item)):
            continue
        kind = (item.get("involvedObject") or {}).get("kind")
        if kind in OPERATOR_WORKLOAD_KINDS:
            excluded_workload += 1
        else:
            events += 1

    ages_days = {key: (now - created).total_seconds() / 86400 for key, created in units.items()}
    median_age = statistics.median(ages_days.values())
    idle = median_age >= cfg.operator_age_days and events < OPERATOR_EVENT_THRESHOLD
    entry["result"] = "IDLE" if idle else "ACTIVE"
    # Threshold ratio: the event count over its threshold, and - inverted -
    # the age threshold over the median age, because the age rule fires
    # when controllers are younger than the threshold (younger is hotter).
    # A median age of exactly zero (everything created this instant) is
    # hotter than any finite ratio.
    if median_age > 0:
        age_heat = _threshold_ratio(cfg.operator_age_days, median_age)
    else:
        age_heat = math.inf if cfg.operator_age_days > 0 else 0.0
    events_heat = _threshold_ratio(events, OPERATOR_EVENT_THRESHOLD)
    entry["threshold_ratio"] = _round2(max(age_heat, events_heat))
    entry["age_days"] = round(median_age, 2)
    entry["units"] = [
        {"namespace": ns, "kind": kind, "name": name, "age_days": round(age, 2)}
        for (ns, kind, name), age in sorted(ages_days.items(), key=lambda kv: kv[1], reverse=True)
    ]
    entry["namespaces"] = sorted({ns for ns, _, _ in units})
    entry["events"] = events
    entry["events_excluded_workload"] = excluded_workload
    entry["event_window_hours"] = cfg.operator_event_hours
    return CheckOutcome(entry["result"], entry, counted=True)


def compute_verdict(outcomes: list[CheckOutcome]) -> tuple[int, int, int, str, int]:
    """The 80% rule: IDLE when the IDLE votes reach int(total * 0.80).

    Uncounted criteria (N/A / UNKNOWN-with-decrement) shrink the
    denominator; cpu and memory UNKNOWN still count, matching the bash
    script's bookkeeping.
    """
    total = sum(1 for outcome in outcomes if outcome.counted)
    met = sum(1 for outcome in outcomes if outcome.result == "IDLE")
    threshold = int(total * 0.80)
    status = "IDLE" if met >= threshold else "ACTIVE"
    # Bash parity: the script exits 1 for IDLE (a warning - resources may be
    # wasted) and 0 for ACTIVE (success), despite its header comment saying
    # otherwise; Jenkins keys on this via warnWhenRcNotZero.
    exit_code = 1 if status == "IDLE" else 0
    return total, met, threshold, status, exit_code


# A single counted UNKNOWN is survivable (it consumes the one miss the 80%
# rule tolerates); two mean the verdict would rest on too little data, so
# the run errors instead of issuing IDLE or ACTIVE.  Only failed reads
# count: a no-data UNKNOWN (a quiet instant that cannot acquit the window)
# is an artificial verdict declared to avoid voting, not missing evidence
# of a failure, so it never trips the gate.
MAX_COUNTED_UNKNOWNS = 1


def counted_unknowns(outcomes: list[CheckOutcome]) -> list[str]:
    """Labels of the counted UNKNOWNs that count toward the gate: failed
    reads only, not the declared no-data UNKNOWNs."""
    return [
        CRITERIA_LABELS[key]
        for key, outcome in zip(CRITERIA_KEYS, outcomes, strict=True)
        if outcome.counted and outcome.result == "UNKNOWN" and not outcome.no_data
    ]


def utilization_veto(node_listing_failed: bool, cpu_result: str, gpu_result: str) -> bool:
    """The scoped utilization veto: when the node listing failed, an ACTIVE
    CPU or GPU criterion cannot be outvoted by the idle quorum.

    A failed listing does not merely cost the GPU its vote (N/A shrinks the
    denominator) - it breaks the quorum's premise that the criteria are
    independent signals, since they all degrade together through the same
    unreadable cluster.  In that state sustained utilization is disqualifying
    no matter how quiet memory, the API server, and the operators are.  With
    a healthy node listing the quorum stands unchanged: a single busy
    criterion can still be outvoted there (run 40 of the fleet produced 16
    IDLE verdicts with an ACTIVE CPU criterion, all with empty node
    inventories; a full-inventory replay kept two of those IDLE by design).
    """
    return node_listing_failed and "ACTIVE" in (cpu_result, gpu_result)


def threshold_focus(outcomes: list[CheckOutcome], status: str) -> dict[str, Any] | None:
    """The criterion sitting at the verdict's boundary, as a threshold ratio:
    for an IDLE verdict the hottest IDLE vote - the one whose flip would eat
    the quorum - and for an ACTIVE verdict the hottest criterion overall.
    `quorum_driven` marks an ACTIVE verdict where nothing fired: UNKNOWN
    votes deprived the quorum.  None when no criterion has a ratio (every
    vote UNKNOWN or N/A)."""
    candidates = [
        (key, outcome)
        for key, outcome in zip(CRITERIA_KEYS, outcomes, strict=True)
        if outcome.entry.get("threshold_ratio") is not None
        and (status == "ACTIVE" or outcome.result == "IDLE")
    ]
    if not candidates:
        return None
    key, outcome = max(candidates, key=lambda item: item[1].entry["threshold_ratio"])
    heat = outcome.entry["threshold_ratio"]
    return {
        "criterion": key,
        "label": CRITERIA_LABELS[key],
        "threshold_ratio": heat,
        "quorum_driven": status == "ACTIVE" and heat < 1.0,
    }

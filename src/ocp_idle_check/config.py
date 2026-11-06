"""Fleet-calibration defaults, PromQL/oc literals, and the CLI (Config + parse_args)."""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from pathlib import Path

# Matches the effective value every real caller passes (-w 10080); the old
# bash default of 10 minutes made the windowed checks near-instantaneous and
# would leave the variance rule with a single data point.
DEFAULT_TIME_WINDOW_MINUTES = 10080  # 7 days

CPU_IDLE_THRESHOLD = 15.0  # percent; instant override + no-data instant vote
MEMORY_IDLE_THRESHOLD = 35.0  # percent
APISERVER_IDLE_THRESHOLD = 100.0  # requests/sec
OPERATOR_IDLE_AGE_DAYS = 7
OPERATOR_NAMESPACES = "opendatahub,redhat-ods-operator,redhat-ods-applications"
OPERATOR_EXCLUDE_PREFIXES = "openshift-,kube-,open-cluster-management-"
OPERATOR_EVENT_THRESHOLD = 5  # reconciliation events within the window
OPERATOR_EVENT_WINDOW_HOURS = 48
EVENT_TIME_MINUTES = 60
ML_NODE_PATTERN = "p5|p4d|g5"

# Variance rule defaults - fleet-calibration knobs, ideal values TBD.
CPU_PEAK_THRESHOLD = 30.0  # percent; any 15-min window above this = ACTIVE
CPU_VARIANCE_RATIO = 2.0  # burst multiple required at the handoff (peak threshold / ratio);
# 2x because the baseline is now the trough, and a peak twice the workload's
# floor is the least that reads as a burst rather than the same workload
CPU_VARIANCE_FLOOR = 1.0  # percent; minimum peak the burst rule demands on a zero baseline
GPU_PEAK_THRESHOLD = 40.0
GPU_VARIANCE_RATIO = 2.0  # burst multiple required at the handoff (peak threshold / ratio)
GPU_VARIANCE_FLOOR = 5.0  # percent; DCGM's idle noise tail reads 1-4% on a GPU doing nothing
API_VARIANCE_RATIO = 1.2  # burst multiple required at the handoff (100 req/s / ratio = 83.3)

# Core-scaled CPU peak threshold: it is hard to max out a large machine (a
# steady 10% on 160 cores is 16 busy cores - a tenant, not noise), so the
# absolute branch eases from 30% at 10 vCPUs to 8% at 100 vCPUs along a power
# law and never goes below 8% - below that point only the variance (trough)
# rule can accuse.  Unknown core counts keep the unscaled default.
CPU_PEAK_THRESHOLD_LARGE = 8.0  # percent at CPU_PEAK_THRESHOLD_LARGE_VCPUS and above
CPU_PEAK_THRESHOLD_LARGE_VCPUS = 100.0
CPU_PEAK_THRESHOLD_SMALL_VCPUS = 10.0  # at this size and below, the full 30%

_CPU_PEAK_EXPONENT = math.log(CPU_PEAK_THRESHOLD_LARGE / CPU_PEAK_THRESHOLD) / math.log(
    CPU_PEAK_THRESHOLD_LARGE_VCPUS / CPU_PEAK_THRESHOLD_SMALL_VCPUS
)


def cpu_peak_threshold(cores: float | None) -> float:
    """The CPU peak threshold for a node with `cores` vCPUs (see the
    constants above); None or non-positive counts keep the default."""
    if not cores or cores <= CPU_PEAK_THRESHOLD_SMALL_VCPUS:
        return CPU_PEAK_THRESHOLD
    if cores >= CPU_PEAK_THRESHOLD_LARGE_VCPUS:
        return CPU_PEAK_THRESHOLD_LARGE
    return CPU_PEAK_THRESHOLD * (cores / CPU_PEAK_THRESHOLD_SMALL_VCPUS) ** _CPU_PEAK_EXPONENT


# Memory movement rule - see check_memory.  Absolute percentage points of
# rise between window averages, not the shared ratio law: a fixed
# allocation is fixed points whatever the baseline, and a rise spread over
# more than the window span reads as drift, not activity.
# 5, not 10: the fleet's rises are flat drift noise bin-by-bin below 5
# points and step up sharply at 10, and the 5-10 band is where the old
# bash level rule was catching real allocations the movement rule missed.
MEMORY_VARIANCE_POINTS = 5.0  # points of rise within the span = ACTIVE
MEMORY_VARIANCE_WINDOWS = 4  # consecutive 15-min windows a rise may span (2 are needed at a min to perform the calculation)

# The shared variance law, in each criterion's native unit: the required
# multiple is 10x at a baseline of 0.2 (VARIANCE_ANCHOR_*) and falls to the
# ratio at baseline_full - the criterion's absolute threshold divided by
# the ratio (25% CPU, ~33.3% GPU, ~83.3 req/s API at the default 1.2) -
# following a power law in between, so quiet baselines need proportionally
# bigger bursts before they count as bursty.  The handoff is exact by
# construction: at baseline_full the required peak is
# ratio x threshold/ratio = the threshold itself, so the variance branch
# hands the verdict to the absolute branch (window peak over the peak
# threshold, API windowed average over the request threshold) precisely
# there.  Past the handoff the power law keeps easing the multiple below
# the ratio; the absolute branch owns those peaks anyway.  A floor - the
# CPU/GPU constants above; the API rule has none - is the minimum required
# peak and stands in for the whole curve on a zero baseline.
VARIANCE_ANCHOR_MULTIPLE = 10.0  # required multiple at VARIANCE_ANCHOR_BASELINE
VARIANCE_ANCHOR_BASELINE = 0.2  # the curve's quiet-end anchor point
VARIANCE_DEFAULT_BASELINE_FULL = 100.0 / 1.2  # the API criterion's handoff point

VARIANCE_WINDOW_MINUTES = 15  # the "15 min period" of the variance rule
STEP_SECONDS = 900  # query_range step: one point per 15-min window
VARIANCE_SOURCE = f"variance over {VARIANCE_WINDOW_MINUTES}m windows"  # criteria `source` label

# Timeouts (seconds).  The bash original used 10 s for every oc call, but it
# queried the operators check namespace by namespace; the cluster-wide
# listings carry every pod, ReplicaSet and event on the cluster and can be
# large on busy ones (run 25 saw the pods and events listings time out at
# 10 s), so they get their own budget.
OC_TIMEOUT = 10
OC_LIST_TIMEOUT = 60  # the big listings: cluster-wide `oc get ... -A`, and the nodes JSON
ROUTE_TIMEOUT = 5
QUERY_TIMEOUT = 60
RANGE_TIMEOUT = 180

PROM_URL_OVERRIDE = "OCP_IDLE_PROMETHEUS_URL"

# PromQL templates.  The `by (node, instance)` on the CPU and memory range
# queries is required: OCP node series carry no `node` label, only
# `instance`.
CPU_RANGE_QUERY = (
    '(1 - avg by (node, instance) (rate(node_cpu_seconds_total{mode="idle"}'
    f"[{VARIANCE_WINDOW_MINUTES}m]))) * 100"
)
GPU_RANGE_QUERY = f"avg_over_time(DCGM_FI_DEV_GPU_UTIL[{VARIANCE_WINDOW_MINUTES}m])"
API_RANGE_QUERY = f"sum(rate(apiserver_request_total[{VARIANCE_WINDOW_MINUTES}m]))"
# Per-node memory utilization windows: the same 15-minute subquery pattern
# as the aggregate below, but grouped per node so the movement rule can
# judge each node's windows against each other.
MEMORY_RANGE_QUERY = (
    "(1 - avg by (node, instance) (avg_over_time("
    "(node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"
    f"[{VARIANCE_WINDOW_MINUTES}m:]))) * 100"
)

# Windowed-average queries, parameterized by the configured time window via
# str.format (`{minutes}`; the doubled braces in the CPU query are format
# escapes for the PromQL label): the API criterion's rate queries (the short
# 5m one for windowless runs).
API_WINDOW_RATE_QUERY = "sum(rate(apiserver_request_total[{minutes}m]))"
API_SHORT_RATE_QUERY = "sum(rate(apiserver_request_total[5m]))"

POD_RE = re.compile(r"controller-manager|operator|dashboard")
OPERATOR_EVENT_RE = re.compile(r"reconcil|created|updated|scaled", re.IGNORECASE)
RECENT_ACTIVITY_RE = re.compile(r"Pod|Deployment|ReplicaSet|Job")

# Events about workload machinery (a pod being scheduled, a replica set
# scaling) are the noise a node drain manufactures by the dozen; the age
# half of the operators rule already treats drains as non-events, so the
# event half must not count them either.
OPERATOR_WORKLOAD_KINDS = frozenset(
    {"Pod", "ReplicaSet", "Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob", "Node"}
)

# Cluster-wide listings for the operators criterion, fetched as jsonpath
# with explicit tab separators: custom-columns collapses empty fields into
# space runs, which would shift the positional fields of a bare-pod row.
# Events are the exception - see Oc.events_all - because a message is free
# text, and no fixed-column format survives an embedded newline or tab.
PODS_ALL_JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}'
    '{.metadata.ownerReferences[0].kind}{"\\t"}{.metadata.ownerReferences[0].name}{"\\t"}'
    '{.metadata.creationTimestamp}{"\\n"}{end}'
)
REPLICASETS_ALL_JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}'
    '{.metadata.creationTimestamp}{"\\n"}{end}'
)


@dataclass(frozen=True)
class Config:
    """All knobs, as parsed from the command line."""

    time_window_minutes: int
    cpu_idle_threshold: float
    memory_idle_threshold: float
    api_threshold: float
    operator_age_days: int
    operator_namespaces: str
    operator_exclude_prefixes: str
    operator_event_hours: int
    event_history_minutes: int
    check_ml_nodes: bool
    ml_node_pattern: str
    cpu_peak_threshold: float
    cpu_variance_ratio: float
    gpu_peak_threshold: float
    gpu_variance_ratio: float
    api_variance_ratio: float
    memory_variance_points: float
    memory_variance_windows: int
    verbose: bool
    token: str
    csv_path: Path | None
    json_path: Path | None


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="ocp-idle-check.sh",
        description="Detect whether an OpenShift cluster is idle.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "-w",
        "--window",
        type=int,
        default=DEFAULT_TIME_WINDOW_MINUTES,
        metavar="MINUTES",
        help="time window for metric averages (default: %(default)s = 7 days)",
    )
    parser.add_argument(
        "-c",
        "--cpu-threshold",
        type=float,
        default=CPU_IDLE_THRESHOLD,
        metavar="N",
        help="CPU idle threshold in percent (default: %(default)s)",
    )
    parser.add_argument(
        "-m",
        "--mem-threshold",
        type=float,
        default=MEMORY_IDLE_THRESHOLD,
        metavar="N",
        help="memory idle threshold in percent (default: %(default)s)",
    )
    parser.add_argument(
        "-a",
        "--api-threshold",
        type=float,
        default=APISERVER_IDLE_THRESHOLD,
        metavar="N",
        help="API server idle threshold in requests/sec (default: %(default)s)",
    )
    parser.add_argument(
        "-e",
        "--events",
        type=int,
        default=EVENT_TIME_MINUTES,
        metavar="MINUTES",
        help="event history window for informational output (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--operator-age",
        type=int,
        default=OPERATOR_IDLE_AGE_DAYS,
        metavar="DAYS",
        help="operator idle age threshold in days (default: %(default)s)",
    )
    parser.add_argument(
        "--operator-namespaces",
        default=OPERATOR_NAMESPACES,
        metavar="NS",
        help="comma-separated namespaces always included in the operator scan "
        "(the scan covers every namespace except the excluded prefixes)",
    )
    parser.add_argument(
        "--operator-exclude-prefixes",
        default=OPERATOR_EXCLUDE_PREFIXES,
        metavar="PREFIX,...",
        help="namespace prefixes skipped by the operator scan - the platform's own "
        "controllers (default: %(default)s)",
    )
    parser.add_argument(
        "--operator-event-hours",
        type=int,
        default=OPERATOR_EVENT_WINDOW_HOURS,
        metavar="HOURS",
        help="operator reconciliation events must be newer than this to count (default: %(default)s)",
    )
    parser.add_argument(
        "--csv", type=Path, metavar="FILE", help="append a result row to a CSV file"
    )
    parser.add_argument("--json", type=Path, metavar="FILE", help="write a JSON report")
    parser.add_argument("--token", default="", metavar="TOKEN", help="Prometheus bearer token")
    parser.add_argument(
        "--cpu-peak-threshold",
        type=float,
        default=CPU_PEAK_THRESHOLD,
        metavar="N",
        help="CPU variance rule: any 15-min window above this percent = ACTIVE (default: %(default)s)",
    )
    parser.add_argument(
        "--cpu-variance-ratio",
        type=float,
        default=CPU_VARIANCE_RATIO,
        metavar="N",
        help="CPU variance rule: burst multiple the peak must clear at the handoff (peak threshold / ratio = 25%%); quieter baselines require more (default: %(default)s)",
    )
    parser.add_argument(
        "--gpu-peak-threshold",
        type=float,
        default=GPU_PEAK_THRESHOLD,
        metavar="N",
        help="GPU variance rule: any 15-min window above this percent = ACTIVE (default: %(default)s)",
    )
    parser.add_argument(
        "--gpu-variance-ratio",
        type=float,
        default=GPU_VARIANCE_RATIO,
        metavar="N",
        help="GPU variance rule: burst multiple the peak must clear at the handoff (peak threshold / ratio = ~33%%); quieter baselines require more (default: %(default)s)",
    )
    parser.add_argument(
        "--api-variance-ratio",
        type=float,
        default=API_VARIANCE_RATIO,
        metavar="N",
        help="API variance rule: burst multiple the peak must clear at the handoff (100 req/s / ratio = ~83 req/s); quieter baselines require more (default: %(default)s)",
    )
    parser.add_argument(
        "--memory-variance-points",
        type=float,
        default=MEMORY_VARIANCE_POINTS,
        metavar="N",
        help="memory variance rule: rise in percentage points between 15-min window "
        "averages that reads as activity (default: %(default)s)",
    )
    parser.add_argument(
        "--memory-variance-windows",
        type=int,
        default=MEMORY_VARIANCE_WINDOWS,
        metavar="N",
        help="memory variance rule: consecutive 15-min windows a rise may span "
        "(default: %(default)s)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="minimal output")
    parser.add_argument("--no-ml-check", action="store_true", help="skip the ML node check")
    args = parser.parse_args(argv)

    return Config(
        time_window_minutes=args.window,
        cpu_idle_threshold=args.cpu_threshold,
        memory_idle_threshold=args.mem_threshold,
        api_threshold=args.api_threshold,
        operator_age_days=args.operator_age,
        operator_namespaces=args.operator_namespaces,
        operator_exclude_prefixes=args.operator_exclude_prefixes,
        operator_event_hours=args.operator_event_hours,
        event_history_minutes=args.events,
        check_ml_nodes=not args.no_ml_check,
        ml_node_pattern=ML_NODE_PATTERN,
        cpu_peak_threshold=args.cpu_peak_threshold,
        cpu_variance_ratio=args.cpu_variance_ratio,
        gpu_peak_threshold=args.gpu_peak_threshold,
        gpu_variance_ratio=args.gpu_variance_ratio,
        api_variance_ratio=args.api_variance_ratio,
        memory_variance_points=args.memory_variance_points,
        memory_variance_windows=args.memory_variance_windows,
        verbose=not args.quiet,
        token=args.token,
        csv_path=args.csv,
        json_path=args.json,
    )

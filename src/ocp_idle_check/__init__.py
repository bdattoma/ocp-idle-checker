"""OpenShift cluster idle detection.

Python port of ocp-idle-check.sh (stdlib only).  The implementation is the
ocp_idle_check package under src/; ocp-idle-check.sh is a thin bash shim that
runs it via `python -m ocp_idle_check` with src/ on PYTHONPATH, so existing
callers - the Jenkins job ("bash ocp-idle-check.sh ..."), cluster-monitor's
vendored copy, and the README examples - keep working unchanged.
A cluster is IDLE when at least 80% of the applicable criteria vote IDLE:

  cpu        variance rule on per-node 15-minute CPU averages taken from
             the Prometheus query_range result (aka the matrix), evaluated
             per node - each node's windows against that node's own median,
             so a steady busy node pooled with a quiet sibling is two steady
             nodes, not a burst: ACTIVE when any node's window averaged above
             the peak threshold, or when a node's window peak clears a
             baseline-scaled required peak.
             No legacy fallback: an unavailable matrix is not smoothed
             over with the old window average.  An instant reading above the
             idle threshold can still override an IDLE result (one-directional,
             as in the bash version); with no windowed data at all, a busy
             instant votes ACTIVE but a quiet one reads UNKNOWN - one
             sample can accuse the window, not acquit it.
  memory     a movement rule on the same per-node 15-minute windows: ACTIVE
             when any live node's window average rose more than a threshold
             of percentage points across a sliding group of consecutive
             windows (10 points within 4 windows by default, roughly 45-60
             minutes) - a level shift in absolute points, not a ratio: a
             fixed allocation is fixed points whatever the baseline, and
             the page-cache drift that pads every idle cluster's average
             moves too slowly to clear the bar.  No legacy fallback here
             either.  The instant
             reading no longer overrides an IDLE verdict (single-snapshot
             deviations land on idle and active clusters at the same rate),
             so with no windowed data at all a busy instant votes ACTIVE
             but a quiet one reads UNKNOWN - the no-data rule cpu keeps.
  api_server legacy window-average request rate against the threshold, plus
             a variance detector: any 15-minute window clearing a
             baseline-scaled required peak also counts as ACTIVE.
  gpu        the same per-node variance rule as cpu, on DCGM GPU
             utilization (DCGM_FI_DEV_GPU_UTIL).  N/A (not counted) on
             clusters without GPU nodes or without DCGM metrics; UNKNOWN
             (counted) when the node listing itself fails.
  operators  controller age and reconciliation events across all non-platform
             namespaces: ACTIVE when the median controller is younger than
             the age threshold (controllers are aged by their ReplicaSet,
             so a node drain does not reset the clock) or when
             reconciliation events inside the event window reach the
             threshold.  N/A (not counted) when no controller pods exist
             anywhere on the cluster.

Every criterion also exports a threshold ratio - its hottest reading as a
fraction of the level that fires it, so >= 1 means it fired - and the
verdict reports the boundary criterion: for an IDLE verdict the hottest
IDLE vote (the one whose flip would eat the quorum), for an ACTIVE verdict
the hottest criterion overall.  It rides the JSON report (criteria.driver
plus each criterion's threshold_ratio), two CSV columns, and an annotation
on the quiet STATUS line, whose "STATUS: <verdict>" prefix stays intact.

A single counted UNKNOWN criterion is survivable (it consumes the one
miss the 80% rule tolerates); two or more failed reads (UNKNOWN from a
failed query or listing, not the declared no-data UNKNOWN of a quiet
instant) mean the verdict would rest on too little data, so the run
errors: status ERROR, exit 2, and a STATUS line naming the UNKNOWN
criteria.

The variance parameters (peak threshold, ratio) are fleet calibration
knobs exposed as command-line flags; the defaults are provisional.  All
three variance rules share one law, in each criterion's native unit
(percent for CPU/GPU, req/s for the API server): the peak must clear 10x
the baseline at a baseline of 1, falling to ratio_threshold times the
baseline at half the criterion's absolute threshold (15% CPU, 20% GPU,
50 req/s API), with the required multiple following a power law in
between and never dropping below ratio_threshold, so quiet baselines
need proportionally bigger bursts before they count as bursty while a
flat series well above that point never does.  The clamp is where the
two branches meet: at half the threshold the required peak equals the
absolute threshold, so the variance rule governs below and the absolute
branch above.  The CPU
(1%) and GPU (5%) rules carry an absolute floor - the minimum required
peak, and the whole requirement on a zero baseline, where DCGM's 1-4%
idle noise tail would otherwise read as an unbounded burst.  The API
rule has no floor: a zero baseline there demands only a positive peak.
"Baseline" is the
median of the 15-minute windows on the query_range path and the window
mean on the aggregate fallback path.

The memory rule stands outside that law on purpose.  Its signal is
change, not level - a rise of absolute percentage points between window
averages - because a fixed allocation is fixed points whatever the
baseline, and the fleet's memory baselines (p50 24%, p90 51%) would make
any ratio demand unreachable peaks.  Its two knobs are the points of
rise and the number of consecutive windows the rise may cross; a rise
spread over more than that span reads as drift, not activity.

The five criteria run concurrently in a thread pool (they are independent
I/O-bound checks); the log output is consumed in a fixed order so it reads
exactly like the old sequential run.  The informational sections avoid
per-node fan-out: per-node instant usage comes from the single cached
`oc adm top nodes` snapshot, and windowed per-node usage from one batched
Prometheus query per metric.

Exit codes (bash parity):
  1 = cluster is IDLE (warning - resources may be wasted)
  0 = cluster is ACTIVE (success - resources are being used)
  2 = error (cannot determine state)

OCP_IDLE_PROMETHEUS_URL overrides the thanos-querier route lookup; it exists
for hermetic tests and manual debugging.

The package layout: config (defaults + CLI), log, oc, prometheus, nodes,
variance, memory_steps, criteria, oversized, informational, report, main.
This __init__ re-exports the public surface so `import ocp_idle_check`
keeps working as it did when everything lived in one file.
"""

from __future__ import annotations

import shutil  # tests patch ocp_idle_check.shutil.which

from .config import (
    API_RANGE_QUERY,
    API_SHORT_RATE_QUERY,
    API_VARIANCE_RATIO,
    APISERVER_IDLE_THRESHOLD,
    CPU_IDLE_THRESHOLD,
    CPU_PEAK_THRESHOLD,
    CPU_RANGE_QUERY,
    CPU_VARIANCE_FLOOR,
    CPU_VARIANCE_RATIO,
    DEFAULT_TIME_WINDOW_MINUTES,
    EVENT_TIME_MINUTES,
    GPU_PEAK_THRESHOLD,
    GPU_RANGE_QUERY,
    GPU_VARIANCE_FLOOR,
    GPU_VARIANCE_RATIO,
    MEMORY_IDLE_THRESHOLD,
    MEMORY_RANGE_QUERY,
    MEMORY_VARIANCE_POINTS,
    MEMORY_VARIANCE_WINDOWS,
    ML_NODE_PATTERN,
    OC_LIST_TIMEOUT,
    OC_TIMEOUT,
    OPERATOR_EVENT_RE,
    OPERATOR_EVENT_THRESHOLD,
    OPERATOR_EVENT_WINDOW_HOURS,
    OPERATOR_EXCLUDE_PREFIXES,
    OPERATOR_IDLE_AGE_DAYS,
    OPERATOR_NAMESPACES,
    OPERATOR_WORKLOAD_KINDS,
    POD_RE,
    PODS_ALL_JSONPATH,
    PROM_URL_OVERRIDE,
    QUERY_TIMEOUT,
    RANGE_TIMEOUT,
    RECENT_ACTIVITY_RE,
    REPLICASETS_ALL_JSONPATH,
    ROUTE_TIMEOUT,
    STEP_SECONDS,
    VARIANCE_ANCHOR_BASELINE,
    VARIANCE_ANCHOR_MULTIPLE,
    VARIANCE_DEFAULT_BASELINE_FULL,
    VARIANCE_SOURCE,
    VARIANCE_WINDOW_MINUTES,
    Config,
    cpu_peak_threshold,
    parse_args,
)
from .criteria import (
    CRITERIA_KEYS,
    CRITERIA_LABELS,
    CRITERIA_SUMMARY_FIELDS,
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
    event_text,
    event_timestamp,
    format_threshold_ratio,
    parse_k8s_timestamp,
    threshold_focus,
    utilization_veto,
    window_bounds,
)
from .informational import (
    _instance_alternation,
    _node_instance_re,
    average_matching,
    gpu_machines_report,
    gpu_usage_section,
    instant_by_instance,
    ml_node_report,
    recent_activity_report,
    windowed_cpu_by_node,
)
from .log import BLUE, GREEN, NC, RED, YELLOW, log_error, log_info, log_ok, log_warn, set_verbose
from .main import run
from .memory_steps import (
    MemoryStepStats,
    _memory_steps_entry,
    evaluate_memory_steps,
    evaluate_memory_steps_by_node,
    summarize_memory_steps,
)
from .nodes import (
    GpuNode,
    cluster_short_name,
    fmt2,
    gpu_nodes_from_items,
    instant_averages,
    master_nodes_from_items,
    node_instance_type,
    node_top_percentages,
    worker_nodes_from_items,
)
from .oc import Oc
from .oversized import evaluate_oversized
from .prometheus import PrometheusClient, _series_node, _series_points, _to_floats
from .report import CSV_HEADER, export_csv, export_json
from .variance import (
    VarianceStats,
    _threshold_ratio,
    _variance_entry,
    _variance_numbers,
    _variance_severity,
    _variance_threshold_ratio,
    evaluate_variance,
    evaluate_variance_by_node,
    summarize_variance,
    variance_curve_exponent,
    variance_required_peak,
)

__all__ = [
    "APISERVER_IDLE_THRESHOLD",
    "API_RANGE_QUERY",
    "API_SHORT_RATE_QUERY",
    "API_VARIANCE_RATIO",
    "BLUE",
    "CPU_IDLE_THRESHOLD",
    "CPU_PEAK_THRESHOLD",
    "CPU_RANGE_QUERY",
    "CPU_VARIANCE_FLOOR",
    "CPU_VARIANCE_RATIO",
    "CRITERIA_KEYS",
    "CRITERIA_LABELS",
    "CRITERIA_SUMMARY_FIELDS",
    "CSV_HEADER",
    "DEFAULT_TIME_WINDOW_MINUTES",
    "EVENT_TIME_MINUTES",
    "GPU_PEAK_THRESHOLD",
    "GPU_RANGE_QUERY",
    "GPU_VARIANCE_FLOOR",
    "GPU_VARIANCE_RATIO",
    "GREEN",
    "MAX_COUNTED_UNKNOWNS",
    "MEMORY_IDLE_THRESHOLD",
    "MEMORY_RANGE_QUERY",
    "MEMORY_VARIANCE_POINTS",
    "MEMORY_VARIANCE_WINDOWS",
    "ML_NODE_PATTERN",
    "NC",
    "OC_LIST_TIMEOUT",
    "OC_TIMEOUT",
    "OPERATOR_EVENT_RE",
    "OPERATOR_EVENT_THRESHOLD",
    "OPERATOR_EVENT_WINDOW_HOURS",
    "OPERATOR_EXCLUDE_PREFIXES",
    "OPERATOR_IDLE_AGE_DAYS",
    "OPERATOR_NAMESPACES",
    "OPERATOR_WORKLOAD_KINDS",
    "PODS_ALL_JSONPATH",
    "POD_RE",
    "PROM_URL_OVERRIDE",
    "QUERY_TIMEOUT",
    "RANGE_TIMEOUT",
    "RECENT_ACTIVITY_RE",
    "RED",
    "REPLICASETS_ALL_JSONPATH",
    "ROUTE_TIMEOUT",
    "STEP_SECONDS",
    "VARIANCE_ANCHOR_BASELINE",
    "VARIANCE_ANCHOR_MULTIPLE",
    "VARIANCE_DEFAULT_BASELINE_FULL",
    "VARIANCE_SOURCE",
    "VARIANCE_WINDOW_MINUTES",
    "YELLOW",
    "CheckOutcome",
    "Config",
    "GpuNode",
    "MemoryStepStats",
    "Oc",
    "PrometheusClient",
    "VarianceStats",
    "_instance_alternation",
    "_memory_steps_entry",
    "_node_instance_re",
    "_series_node",
    "_series_points",
    "_threshold_ratio",
    "_to_floats",
    "_variance_entry",
    "_variance_numbers",
    "_variance_severity",
    "_variance_threshold_ratio",
    "average_matching",
    "check_api",
    "check_cpu",
    "check_gpu",
    "check_memory",
    "check_operators",
    "cluster_short_name",
    "compute_verdict",
    "counted_unknowns",
    "cpu_peak_threshold",
    "criteria_summary",
    "evaluate_memory_steps",
    "evaluate_memory_steps_by_node",
    "evaluate_oversized",
    "evaluate_variance",
    "evaluate_variance_by_node",
    "event_text",
    "event_timestamp",
    "export_csv",
    "export_json",
    "fmt2",
    "format_threshold_ratio",
    "gpu_machines_report",
    "gpu_nodes_from_items",
    "gpu_usage_section",
    "instant_averages",
    "instant_by_instance",
    "log_error",
    "log_info",
    "log_ok",
    "log_warn",
    "master_nodes_from_items",
    "ml_node_report",
    "node_instance_type",
    "node_top_percentages",
    "parse_args",
    "parse_k8s_timestamp",
    "recent_activity_report",
    "run",
    "set_verbose",
    "shutil",
    "summarize_memory_steps",
    "summarize_variance",
    "threshold_focus",
    "utilization_veto",
    "variance_curve_exponent",
    "variance_required_peak",
    "window_bounds",
    "windowed_cpu_by_node",
    "worker_nodes_from_items",
]

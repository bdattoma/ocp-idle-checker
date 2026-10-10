# OpenShift Cluster Idle Detection Script

Detects if your OpenShift cluster is idle based on CPU, memory, API server activity, GPU utilization, and operator age. Supports time-windowed metrics and GPU node detection.

`ocp-idle-check.sh` is a thin bash shim that execs the Python implementation (`ocp_idle_check.py`, stdlib only), so existing callers — the Jenkins job, cluster-monitor's vendored copy — keep working unchanged.

## Quick Start

```bash
# Default check (7-day window)
./ocp-idle-check.sh

# Quiet mode with export
./ocp-idle-check.sh -q --csv results.csv --json results.json

# Custom thresholds
./ocp-idle-check.sh -w 1440 -c 15 -m 35 -a 50
```

## Exit Codes

- **0** = Cluster is ACTIVE (success - resources are being used)
- **1** = Cluster is IDLE (warning - resources may be wasted)
- **2** = Error

## Idle Criteria (80% must pass)

Each criterion is evaluated independently and can be `IDLE`, `ACTIVE`, `UNKNOWN`, or `N/A`. The cluster is declared **IDLE** when the number of `IDLE` results reaches `int(total_criteria × 0.80)`.

### Active Rules

| Criterion | Votes ACTIVE when |
|-----------|-------------------|
| **CPU** | any node's 15-minute window > 30%, or any node's window peak clearing a baseline-scaled required peak (10× its own median at a 1% median, 2× at 50%; 1% floor on a zero median); an instant `oc adm top nodes` reading ≥ 15% can also flip an otherwise-IDLE result; with no windowed data at all, a busy instant votes ACTIVE but a quiet one reads UNKNOWN |
| **Memory** | the time-windowed average ≥ 35%, or an instant snapshot ≥ 35% (one-directional override); with no windowed data at all, a busy instant votes ACTIVE but a quiet one reads UNKNOWN |
| **API Server** | the time-windowed average ≥ 100 req/sec, or any 15-minute window clearing a baseline-scaled required peak (10× the median at a 1 req/s median, 2× at 50 req/s) |
| **GPU** | any node's 15-minute window > 40%, or any node's window peak clearing a baseline-scaled required peak (DCGM GPU utilization; 5% floor on a zero median) |
| **Operators** | the median controller age is < 7 days, or ≥ 5 reconciliation events in the last 48 hours |

A criterion votes IDLE when none of its ACTIVE conditions hold. Some criteria can instead be N/A — not counted in the denominator (see [Conditional Criteria](#conditional-criteria)).

### Variance Detection (CPU & GPU)

The CPU and GPU criteria take per-node 15-minute window averages from the Prometheus `query_range` matrix and evaluate **each node against its own history**: a steady busy node pooled with a quiet sibling is two steady nodes, not a burst, so pooling the cluster's windows into one median (the old behavior) is gone. The criterion votes ACTIVE when any node is individually bursty:

- **Peak rule**: any node's 15-minute window averaged above the peak threshold (30% for CPU, 40% for GPU), **or**
- **Variance rule**: a node's window peak clears a **required peak** derived from that node's own median window.

The required peak follows one power law shared by all three variance rules, in each criterion's native unit (percent for CPU/GPU, req/s for the API server): **10× the baseline at a baseline of 1**, falling to **2× at a baseline of 50**, so quiet baselines need proportionally bigger bursts before they count as bursty while a steady node sitting near its own median never fires:

| node median | required multiple | required peak |
|---|---|---|
| 0% | — | the floor: 1% (CPU) / 5% (GPU) |
| 1% | 10× | 10% |
| 2% | 7.5× | 15.0% |
| 5% | 5.2× | 25.8% |
| 10% | 3.9× | 38.8% |
| 20% | 2.9× | 58.3% |
| 50% | 2× | 100% |

The multiple is `variance-ratio` (default 2×) at a baseline of 50 and above; below, the variance rule is the sensitive branch. Above each criterion's normal threshold nothing needs the variance rule at all — a window over the peak threshold fires the peak rule (CPU/GPU), and an API median large enough to shallow the curve implies a windowed average over the request threshold — so the curve carries no cap. The CPU and GPU rules carry an absolute floor — 1% and 5% — that is the minimum required peak and the whole requirement on a zero median: DCGM's 1-4% idle noise tail (kernel-residency blips from context init, ECC scrubbing, and health probes, not real use) on a zero-median exporter stays idle under the 5% GPU floor. The API rule has no floor; a zero median there demands only a positive request rate. Fleet calibration put the CPU peak threshold at 30%: the 30-40% max-peak band is sustained workloads the variance rule cannot see (a steady node sits near its own median), idle exemplars peak below 20%, and the highest one-off blip below 40% sits at 26.9%.

The criteria JSON detail exports each node's peak, median, ratio, required multiple, and required peak (`per_node`), with the cluster-level summary reporting the pooled peak plus the baseline/ratio of the node that came closest to (or furthest past) its own requirement (`node`, `required_ratio`, `required_peak`).

CPU falls back to the legacy window-average rule when the range matrix is unavailable, and an instant `oc adm top nodes` reading above the idle threshold can still override an IDLE result (one-directional, as in the bash version). When there is no windowed data at all, the instant snapshot can only accuse, never acquit: a busy instant votes ACTIVE, but a quiet one reads UNKNOWN (counted) — one sample is evidence of activity, not proof the whole window was quiet. Windowless runs (`--window 0`) still judge on the instant alone, since there the instant is the whole question. The GPU rule deliberately keeps series from deleted GPU nodes — their samples are still activity — and falls back to whole-window max/avg aggregates when the matrix is unavailable.

### Variance Detection (API Server)

The API server criterion keeps the legacy window-average threshold (100 req/sec) and adds a second detector: any 15-minute window clearing a baseline-scaled required peak also counts as ACTIVE. The detector shares the CPU/GPU law in req/s: 10× the median at a 1 req/s median, 2× at 50 req/s, power law in between, no floor. Unlike CPU/GPU there is no peak branch; the absolute-threshold branch is the windowed average checked alongside it, and it owns the high end — a median above ~200 req/s implies a windowed average above the request threshold.

### Operators

The scan is cluster-wide: every namespace counts except those matching the excluded prefixes (`openshift-`, `kube-`, `open-cluster-management-` — the platform's own controllers), and `--operator-namespaces` acts as an always-include override for namespaces that would otherwise be excluded. A controller is any pod whose name matches `controller-manager|operator|dashboard`.

Each controller is aged by its **ReplicaSet**, not its pod: a node drain recreates pods without meaning the operator was reinstalled, so the ReplicaSet's creation timestamp — which only moves on a real rollout — is joined via the pod's owner references. Pods without a ReplicaSet owner (or whose ReplicaSet has been deleted mid-rollout) fall back to their own pod age.

The criterion votes on the **median** age across controllers, so one stale survivor cannot mask a fleet-wide reinstall and one fresh restart cannot mark a quiet cluster active. Reconciliation activity is counted over a 48-hour window; events about workload objects (pods, replica sets, deployments, and the like — the noise a churning workload or a node drain manufactures) are excluded from that count and exported separately as `events_excluded_workload` for calibration.

### Instant Override (Memory)

Memory uses the windowed average with a one-directional instant override:

| Windowed average | Instant snapshot | Result |
|-----------------|-----------------|--------|
| IDLE            | IDLE            | **IDLE** ✓ |
| IDLE            | ACTIVE          | **ACTIVE** — spike detected |
| ACTIVE          | any             | **ACTIVE** |
| N/A             | IDLE            | **UNKNOWN** — one sample cannot acquit the window |
| N/A             | ACTIVE          | **ACTIVE** |

Windowless runs (`--window 0`) keep the old behavior and judge on the instant alone: with no window configured, the instant is the whole question, not a stand-in for missing window evidence.

### Conditional Criteria

Some criteria are **skipped** (not counted in the total) when the required data is unavailable, which affects the 80% threshold denominator:

- **API Server**: skipped if both the windowed average and the variance detector have no data
- **GPU**: skipped on clusters without GPU nodes or without DCGM metrics; when the `oc get nodes` listing itself fails, the criterion instead reads UNKNOWN (counted) — a failed listing is not an empty cluster
- **Operators**: skipped when no controller pods are found in any scanned namespace

CPU and memory `UNKNOWN` still count toward the denominator (they never help the IDLE count, only the total), and so does a GPU `UNKNOWN` from a failed node listing.

## Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `-w, --window MINUTES` | Time window for averages | 10080 (7 days) |
| `-c, --cpu-threshold N` | CPU idle threshold (%) — instant override and legacy fallback | 15 |
| `-m, --mem-threshold N` | Memory idle threshold (%) | 35 |
| `-a, --api-threshold N` | API requests/sec threshold | 100 |
| `-o, --operator-age N` | Operator age threshold (days) | 7 |
| `--operator-namespaces NS` | Namespaces always included in the operator scan (the scan covers every namespace except the excluded prefixes) | opendatahub,redhat-ods-operator,redhat-ods-applications |
| `--operator-exclude-prefixes PREFIX,...` | Namespace prefixes excluded from the operator scan | openshift-,kube-,open-cluster-management- |
| `--operator-event-hours HOURS` | Reconciliation event window for the operators criterion | 48 |
| `--cpu-peak-threshold N` | CPU: any 15-min window above this % = ACTIVE | 30 |
| `--cpu-variance-ratio N` | CPU: burst multiple the peak must clear at a baseline of 50 percent; quieter baselines require more | 2 |
| `--gpu-peak-threshold N` | GPU: any 15-min window above this % = ACTIVE | 40 |
| `--gpu-variance-ratio N` | GPU: burst multiple the peak must clear at a baseline of 50 percent; quieter baselines require more | 2 |
| `--api-variance-ratio N` | API: burst multiple the peak must clear at a baseline of 50 req/s; quieter baselines require more | 2 |
| `--csv FILE` | Export to CSV | - |
| `--json FILE` | Export to JSON | - |
| `--token TOKEN` | Prometheus bearer token (resolved via `oc` when omitted) | - |
| `-e, --events MINUTES` | Event history window for informational output | 60 |
| `-q, --quiet` | Minimal output | false |
| `--no-ml-check` | Skip the ML/GPU node check | false |
| `--debug-probe` | Accepted for compatibility; criteria detail is always exported now | false |

The variance parameters are fleet-calibration knobs; the defaults are provisional.

## Features

### Time-Windowed Metrics
Queries Prometheus to get average CPU/Memory over the last N minutes instead of just current instant values. The default window of 7 days matches what every real caller passes; a short window leaves the variance rule with too few 15-minute windows to establish a baseline.

```bash
# Check if idle over the last day
./ocp-idle-check.sh -w 1440
```

### GPU Node Detection
Automatically detects GPU nodes (NVIDIA/AMD) and reports:
- GPU count and type
- Instance flavor (e.g., g4dn.xlarge, p5.48xlarge)
- CPU and memory usage (current + windowed)
- DCGM GPU utilization as a voting criterion (see above)

When the `oc get nodes` listing itself fails, GPU presence is unknown: the JSON report carries `has_gpu_nodes: null` and `node_count: null`, the CSV row says `unknown` rather than `false`, and the GPU criterion reads UNKNOWN (counted).

### Performance
The five criteria are independent I/O-bound checks and run concurrently in a thread pool; log output is consumed in a fixed order, so it reads exactly like a sequential run. The informational sections avoid per-node fan-out entirely: per-node instant usage is parsed from the single cached `oc adm top nodes` snapshot, and windowed per-node usage comes from one batched Prometheus query per metric instead of two round trips per node. The ML/GPU node usage display only gathers numbers in verbose mode, the only mode that shows them.

### Export Results
Export to CSV (append mode) or JSON for automation and historical tracking.

**CSV Format:**
```csv
timestamp,cluster,status,cpu_result,cpu_value,memory_result,...,gpu_result,gpu_value,operators_result,...,has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,...
```

**JSON Format:**
```json
{
  "status": "IDLE",
  "criteria": {
    "cpu": {
      "result": "IDLE",
      "peak": 9.0,
      "baseline": 3.0,
      "ratio": 3.0,
      "required_ratio": 6.36,
      "required_peak": 19.09,
      "node": "node-1",
      "per_node": [
        {"node": "node-1", "peak": 9.0, "baseline": 3.0, "ratio": 3.0, "required_ratio": 6.36, "required_peak": 19.09, "points": 673, "peak_exceeded": false, "variance_exceeded": false}
      ]
    },
    "gpu": { "result": "ACTIVE", "peak": 80.0 },
    ...
  },
  "gpu": {
    "has_gpu_nodes": true,
    "node_count": 1,
    "flavors": "NVIDIA(g4dn.xlarge)",
    "usage": { "cpu_current": "7.00", "cpu_windowed": "6.58", ... }
  }
}
```

## Use Cases

### AWS Capacity Block Monitoring
```bash
# Check if expensive ML hardware is idle
./ocp-idle-check.sh -w 1440 -c 5

if [ $? -eq 1 ]; then
    echo "Cluster idle, consider releasing reservation"
fi
```

### Scheduled Monitoring
```bash
# Cron: check hourly, log to CSV
0 * * * * /path/to/ocp-idle-check.sh -q --csv /var/log/ocp-idle-history.csv
```

## Dependencies

**Required:**
- `oc` (OpenShift CLI) - logged in to cluster
- `python3` (3.12+; standard library only, no pip installs)
- Prometheus/Thanos - for time-windowed metrics

The entrypoint shim looks for `python3.12` first and falls back to the bare
`python3` name.  This matters on the Jenkins agent image, where the bare
`python3` is 3.9 while `/usr/bin/python3.12` is installed alongside it.

For development (tests, linting) use [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Output Modes

### Verbose (default)
Shows full details: node lists, metrics, operator pods, GPU information, ML/GPU node usage, recent events.

### Quiet (`-q`)
Minimal output:
```
CPU: IDLE
Memory: ACTIVE
API Server: IDLE
GPU: N/A
Operators: IDLE
STATUS: IDLE
```

## Configuration

Defaults live at the top of `ocp_idle_check.py`; prefer overriding them with the command-line options above.

## Troubleshooting

**"oc command not found"**
- Install the OpenShift CLI and log in: `oc login`

**"Not logged in to an OpenShift cluster"**
- Run `oc whoami` to check your session

**Prometheus queries return no data**
- Check Prometheus/Thanos is available: `oc get pods -n openshift-monitoring`
- `OCP_IDLE_PROMETHEUS_URL` overrides the thanos-querier route lookup (for debugging)

**Script hangs**
- Built-in timeouts on all `oc` commands (10s, or 60s for the big cluster-wide listings — the `-A` jsonpath scans, the events JSON, and the nodes JSON) and Prometheus queries (60s instant / 180s range)
- If persistent, check cluster API responsiveness

## Development

```bash
uv sync                # create the venv (Python 3.12+)
uv run pytest          # hermetic test suite
uv run ruff check      # lint
uv run ruff format --check
```

The tests in `tests/` are hermetic — no cluster, no `oc` login, no network. The oc CLI is stubbed with strict argv matching (an undeclared `oc` invocation fails the test instead of silently returning empty output) and Prometheus is a local HTTP server keyed on the exact PromQL strings, so a changed query fails the tests instead of silently degrading on a real cluster.

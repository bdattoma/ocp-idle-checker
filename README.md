# OpenShift Cluster Idle Detection Script

Detects if your OpenShift cluster is idle based on CPU, memory, API server activity, GPU utilization, and operator age. Supports time-windowed metrics, GPU node detection, and an informational oversized classification.

`ocp-idle-check.sh` is a thin bash shim that runs the Python implementation (stdlib only) via `python -m ocp_idle_check` with `src/` on `PYTHONPATH`, so existing callers — the Jenkins job, cluster-monitor's vendored copy — keep working unchanged. Installed copies can use the `ocp-idle-check` console script or `python -m ocp_idle_check` directly.

The package splits along the same lines as the checks themselves: `config` (defaults and the CLI), `log`, `oc` (the oc CLI wrapper), `prometheus`, `nodes` (node facts and instant readings), `variance` (the shared burst rule), `memory_steps` (the memory movement rule), `criteria` (the five checks, the 80% verdict, and the UNKNOWN gate), `oversized`, `informational` (the non-voting report sections), `report` (CSV/JSON export), and `main`. The package `__init__` re-exports the public surface, so `import ocp_idle_check` works as it did when everything lived in one file.

## Quick Start

```bash
# Default check (7-day window)
./ocp-idle-check.sh

# Quiet mode with export
./ocp-idle-check.sh -q --csv results.csv --json results.json

# Custom thresholds
./ocp-idle-check.sh -w 1440 -c 15 -a 50
```

## Exit Codes

- **1** = Cluster is IDLE (warning - resources may be wasted)
- **0** = Cluster is ACTIVE (success - resources are being used)
- **2** = Error (including: more than one counted UNKNOWN from failed reads — not enough data for a verdict)

## Idle Criteria (80% must pass)

Each criterion is evaluated independently and can be `IDLE`, `ACTIVE`, `UNKNOWN`, or `N/A`. The cluster is declared **IDLE** when the number of `IDLE` results reaches `int(total_criteria × 0.80)`.

### Active Rules

| Criterion | Votes ACTIVE when |
|-----------|-------------------|
| **CPU** | any node's 15-minute window > the core-scaled peak threshold (30% at ≤ 10 vCPUs easing to 8% at ≥ 100, never below 8%; all 15-min windows from each node's total window (default: 7 days) are considered), or any node bursting well above its own trough (10× at a 0.2% trough, easing to 2× at 15%; 1% floor on a zero trough); an instant `oc adm top nodes` reading ≥ 15% can also flip an otherwise-IDLE result; with no windowed data at all, a busy instant votes ACTIVE but a quiet one reads UNKNOWN |
| **Memory** | any live node's windowed memory rises more than 5 percentage points between two windows up to 4 consecutive 15-minute windows apart (2 windows are enough — the 4 is a max span, not a required count); with no windowed data at all, a busy instant votes ACTIVE but a quiet one reads UNKNOWN |
| **API Server** | the time-windowed average ≥ 100 req/sec, or any 15-minute window bursting well above the median (10× at a 0.2 req/s median, easing to 1.2× at ~83.3 req/s) |
| **GPU** | any node's 15-minute window > 40%, or any node bursting well above its own trough (DCGM GPU utilization; 5% floor on a zero trough) |
| **Operators** | the median controller age is < 7 days, or ≥ 5 reconciliation events in the last 48 hours |

A criterion votes IDLE when none of its ACTIVE conditions hold. Some criteria can instead be N/A — not counted in the denominator (see [Conditional Criteria](#conditional-criteria)).

### Checks at a glance

| Check | Unit | Judged on | Absolute threshold | Variance threshold | Zero-baseline floor | No-data behavior |
|---|---|---|---|---|---|---|
| **CPU** | % per node | 15-min window averages from Prometheus | any window > 30% (`--cpu-peak-threshold`), scaled down by core count to as low as 8% | window peak > trough-scaled required peak (see table below) | 1% | busy instant (≥ 15%) votes ACTIVE; quiet instant reads UNKNOWN (counted) |
| **Memory** | % per node | 15-min window averages | — (levels never vote) | rise > 5pp (`--memory-variance-points`) across 4 windows (`--memory-variance-windows`) | — | busy instant (≥ 35%) votes ACTIVE; quiet instant reads UNKNOWN (counted) |
| **GPU** | % per exporter | 15-min DCGM windows | any window > 40% (`--gpu-peak-threshold`) | same law as CPU (unscaled threshold) | 5% | no GPU nodes or no DCGM → N/A (not counted); failed node listing → UNKNOWN (counted) |
| **API Server** | req/s cluster-wide | 15-min windows + whole-window average | windowed average ≥ 100 req/s (`--api-threshold`) | same law as CPU, floorless | none | both detectors dry → N/A (not counted) |
| **Operators** | days / events | ReplicaSet ages, 48h reconciliation events | median age < 7 days (`--operator-age`) or ≥ 5 events (`--operator-event-hours` window) | — | — | no controllers found → N/A (not counted) |

The CPU row's instant override — an `oc adm top nodes` reading ≥ 15% flipping an otherwise-IDLE verdict — also applies when the windowed data is healthy; memory has no such override.

### Variance Detection (CPU & GPU)

The CPU and GPU criteria judge **each node against its own history** instead of pooling the cluster into one average — a busy node blended into nine quiet ones used to read idle. Each node's 15-minute window averages are compared against that node's own trough, and the criterion votes ACTIVE when any single node looks busy:

- **Peak rule**: any node's 15-minute window averaged above the peak threshold (core-scaled 30%–8% for CPU, 40% for GPU), **or**
- **Variance rule**: a node jumped well above its own trough — a burst.

How big the jump must be depends on how quiet the node usually is: **10× the node's trough when the trough is 0.2**, easing along a power law to **2× at the handoff — the criterion's absolute threshold divided by 2** — 15% for CPU, 20% for GPU (the API server keeps the same law from its median, handing off at ~83.3 req/s with a 1.2× ratio). The idea: a node idling at 1% that jumps to 5% is activity; a node sitting steadily at 15% is not. The handoff is exact by construction: at a baseline of the threshold ÷ 2 the required peak is 2 × (threshold ÷ 2) = the threshold itself, so precisely there the simpler peak rule takes over (past it the curve keeps easing, but the peak rule owns those baselines anyway):

| series trough | CPU multiple → required peak (%) | GPU multiple → required peak (%) |
|---|---|---|
| 0 | — | — |
| 0.2 | 10× → 2 | 10× → 2 |
| 1 | 5.5× → 5.5 | 5.7× → 5.7 |
| 2 | 4.2× → 8.5 | 4.5× → 8.9 |
| 5 | 3.0× → 15.1 | 3.2× → 16.2 |
| 10 | 2.3× → 23.3 | 2.5× → 25.5 |
| at the handoff (CPU 15 / GPU 20) | 2× → 30 = peak threshold | 2× → 40 = peak threshold |

(The API server's variance rule measures from the series median instead — a req/s series has no meaningful floor — with the same shape, 10× at a 0.2 req/s median easing to 1.2× at ~83.3 req/s, floorless.)

On a zero trough the floor is the whole requirement: 1% for CPU, 5% for GPU, and none for the API rule — any positive request rate fires. The GPU floor sits just over DCGM's 1–4% idle noise (background kernel and health-probe blips, not real use), so an untouched card stays idle while a real burst over zero fires. (Memory is not in this table on purpose — it is judged by rise in percentage points, not by the variance curve; see [Memory Movement](#memory-movement).)

An instant `oc adm top nodes` reading above 15% can still flip an otherwise-IDLE result (as in the bash version), but a quiet instant proves nothing: with no windowed data at all, a busy instant votes ACTIVE while a quiet one reads UNKNOWN (counted) — one sample is evidence of activity, not proof the whole window was quiet. There is no legacy 7-day-average fallback anymore. Windowless runs (`--window 0`) judge on the instant alone, since there the instant is the whole question. The GPU rule deliberately keeps series from deleted GPU nodes — their samples are still activity — and falls back to whole-window max/avg aggregates when the matrix is unavailable.

### Variance Detection (API Server)

The API server criterion keeps the legacy window-average threshold (100 req/sec) and adds a burst detector with the same shape in req/s: 10× the median at a 0.2 req/s median, easing to 1.2× at ~83.3 req/s (the request threshold ÷ 1.2), no floor. There is no peak branch; the windowed average covers the high end — a median above ~83 req/s implies an average over the request threshold.

### Operators

The scan is cluster-wide: every namespace counts except those matching the excluded prefixes (`openshift-`, `kube-`, `open-cluster-management-` — the platform's own controllers), and `--operator-namespaces` acts as an always-include override for namespaces that would otherwise be excluded. A controller is any pod whose name matches `controller-manager|operator|dashboard`.

Each controller is aged by its **ReplicaSet**, not its pod: a node drain recreates pods without meaning the operator was reinstalled, so the ReplicaSet's creation timestamp — which only moves on a real rollout — is joined via the pod's owner references. Pods without a ReplicaSet owner (or whose ReplicaSet has been deleted mid-rollout) fall back to their own pod age.

The criterion votes on the **median** age across controllers, so one stale survivor cannot mask a fleet-wide reinstall and one fresh restart cannot mark a quiet cluster active. Reconciliation activity is counted over a 48-hour window; events about workload objects (pods, replica sets, deployments, and the like — the noise a churning workload or a node drain manufactures) are excluded from that count.

### Memory Movement

Memory is judged by movement, not level — memory that stays allocated keeps its level forever, so a high-baseline cluster sat over the old 35% level threshold permanently. The criterion reuses the CPU rule's per-node 15-minute windows and votes ACTIVE when any live node's windowed memory rose more than 5 percentage points (`--memory-variance-points`) across a sliding group of 4 consecutive windows (`--memory-variance-windows`) — roughly an hour. Something rising that fast only happens when something is actively allocating.

- **Rises are read at full strength.** The span always contains a fully-before and a fully-after window, so a jump landing mid-window cannot be diluted away.
- **Drift stays excused.** Growth spread over more than the span (page cache and the like) reads as drift, not activity.
- **Drops and data holes never vote.** Decreases are debug-only, and pairs are admitted by timestamp gap, so a scrape hole cannot fabricate a step.
- **The instant no longer overrides.** With no windowed data at all, a busy instant votes ACTIVE but a quiet one reads UNKNOWN — the same no-data rule the CPU criterion keeps. Windowless runs (`--window 0`) judge on the instant alone; there is no legacy window-average fallback for memory either.

### Oversized (informational)

An ACTIVE cluster is **oversized** when it carries a worker node that would have been judged inactive on its own — its per-node CPU peak never crossed the peak threshold over the whole window (variance firings are ignored, so a node that only ever low-level-bursted still counts). The classification never votes and never changes the exit code, and it names only nodes whose removal is actually on the table:

- **Control-plane and infra nodes are never named** — a quiet master is expected, not right-sizing evidence, so a SNO can never be oversized.
- **GPU nodes need their GPUs never touched.** DCGM series cannot be attributed to node names, so a GPU node is only acquirable when no GPU series anywhere ever met the variance floor (5%): one touched exporter blocks acquittal of all of them. The bar is the floor rather than the peak threshold because VRAM capacity is often why a particular GPU was picked.
- **Undecidable runs stay quiet.** An ACTIVE verdict caused by missing data rather than any criterion firing, a failed node listing, or CPU with no per-node window data reports false rather than guessing.

The JSON report gains a top-level `oversized` section listing the level-idle nodes with their CPU peaks — plus `gpu_peak`, the hottest series cluster-wide, when a GPU node is named — and stdout prints one line, `Oversized: true (N level-idle nodes)`, only when it fires. The CSV is unchanged.

### Conditional Criteria

Some criteria are **skipped** (not counted in the total) when the required data is unavailable, which affects the 80% threshold denominator:

- **API Server**: skipped if both the windowed average and the variance detector have no data
- **GPU**: skipped on clusters without GPU nodes or without DCGM metrics; when the `oc get nodes` listing itself fails, the criterion instead reads UNKNOWN (counted) — a failed listing is not an empty cluster
- **Operators**: skipped when no controller pods are found in any scanned namespace

CPU and memory `UNKNOWN` still count toward the denominator (they never help the IDLE count, only the total), and so does a GPU `UNKNOWN` from a failed node listing. A single counted `UNKNOWN` is survivable — it just consumes the one miss the 80% rule tolerates. Two or more counted `UNKNOWN`s **from failed reads** (a failed query or node listing) mean the verdict would rest on too little data, so the run errors instead: status `ERROR`, exit code 2, and a STATUS line naming the UNKNOWN criteria (`STATUS: ERROR (too many UNKNOWN criteria: CPU, Memory)`). The no-data `UNKNOWN` of a quiet instant is different — an artificial verdict declared to avoid voting, not a failed read — so it never trips the gate.

### Utilization Veto (failed node listing)

When the `oc get nodes` listing fails, an **ACTIVE** CPU or GPU criterion vetoes an IDLE quorum: the verdict becomes ACTIVE regardless of the vote count. The quorum's tolerance for one dissenting vote assumes the criteria are independent signals, but a failed listing degrades them together — the GPU loses its vote to the same failure, the per-node oversized analysis is skipped, and the denominator shrinks *because of* the outage. In that state sustained utilization is disqualifying. With a healthy node listing the quorum stands unchanged: a single busy criterion can still be outvoted there. Run 40 produced 16 IDLE verdicts with an ACTIVE CPU criterion, but the raw reports show healthy node listings for all of them (gpu `N/A` from no GPU nodes, not `UNKNOWN` from a failed listing) — the IDLE came from the denominator shrinking instead (operators `N/A`, api `UNKNOWN` uncounted) while the `int()` floor let 2-of-3 (67%) pass as 80%. No run to date (37, 40, or the Oct 30 fleet run) has recorded a failed node listing, so this veto has not yet fired on real data; it guards a failure mode the fleet runs merely haven't hit.

### Threshold Ratio (how close to the cutoff)

Every criterion exports a `threshold_ratio`: its hottest reading as a fraction of the level that fires it, so ≥ 1.0 means that reading fired the criterion. The ratio is dimensionless — percent over percent, requests over requests — which makes the criteria comparable:

| Criterion | Hottest reading over |
|---|---|
| **CPU** | the hottest node's window peak over the peak threshold (30%) and over that node's own required peak; the instant reading over 15% |
| **Memory** | the hottest node's rise over the movement threshold (5pp); the instant over 35% on the no-data path |
| **API Server** | the windowed average over 100 req/s; the variance peak over its baseline-scaled required peak |
| **GPU** | the hottest node's window peak over the peak threshold (40%) and over that node's own required peak |
| **Operators** | the event count over 5; the age threshold over the median controller age, inverted because younger is hotter — a 2-day median against the 7-day threshold reads 350% |

When several paths could fire one criterion (a node's peak against two levels, the instant override alongside the windows), the hottest wins.

The report also names the **driver**: the criterion sitting at the verdict's boundary.

- **IDLE verdict** → the hottest criterion that voted IDLE — the one *closest to active*, first to flip the verdict if it gets busier.
- **ACTIVE verdict** → the hottest criterion overall, necessarily at or past 1.0 — unless nothing fired and missing-data votes alone tipped the verdict, which the report flags as `"quorum_driven": true`.

The STATUS line keeps its `STATUS: <verdict>` prefix (parsers key on it) and carries the driver as an annotation:

```
STATUS: IDLE (closest to active: Operators at 58% of threshold)
STATUS: ACTIVE (CPU at 194% of threshold)
STATUS: ACTIVE (UNKNOWN-driven; API Server at 19% of threshold)
```

Verbose runs state the same fact above the summary ("Closest to active: ..." / "Most active: ..."). The JSON gains each criterion's `threshold_ratio` plus a `criteria.driver` object (`null` when no criterion has a ratio — every vote UNKNOWN or N/A).

Edge cases: UNKNOWN and N/A criteria carry no ratio (`null`); the floorless zero-baseline API fire (any positive peak over a zero median) has no finite ratio, reads as `Infinity` in JSON — Python's `json` and `jq` both read it back — and displays as `999%+`. A ratio of exactly 1.000 can sit on either side of the verdict (strict vs. non-strict firing comparisons); the verdict stays authoritative.

## Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `-w, --window MINUTES` | Time window for averages | 10080 (7 days) |
| `-c, --cpu-threshold N` | CPU idle threshold (%) — instant override and no-data instant vote | 15 |
| `-m, --mem-threshold N` | Memory idle threshold (%) — no-data instant vote | 35 |
| `--memory-variance-points N` | Memory: rise in percentage points between 15-min window averages that reads as activity | 5 |
| `--memory-variance-windows N` | Memory: consecutive 15-min windows a rise may span | 4 |
| `-a, --api-threshold N` | API requests/sec threshold | 100 |
| `-o, --operator-age N` | Operator age threshold (days) | 7 |
| `--operator-namespaces NS` | Namespaces always included in the operator scan (the scan covers every namespace except the excluded prefixes) | opendatahub,redhat-ods-operator,redhat-ods-applications |
| `--operator-exclude-prefixes PREFIX,...` | Namespace prefixes excluded from the operator scan | openshift-,kube-,open-cluster-management- |
| `--operator-event-hours HOURS` | Reconciliation event window for the operators criterion | 48 |
| `--cpu-peak-threshold N` | CPU: any 15-min window above this % = ACTIVE | 30 |
| `--cpu-variance-ratio N` | CPU: burst multiple the peak must clear at the handoff (peak threshold ÷ ratio = 15%); quieter baselines require more | 2 |
| `--gpu-peak-threshold N` | GPU: any 15-min window above this % = ACTIVE | 40 |
| `--gpu-variance-ratio N` | GPU: burst multiple the peak must clear at the handoff (peak threshold ÷ ratio = 20%); quieter baselines require more | 2 |
| `--api-variance-ratio N` | API: burst multiple the peak must clear at the handoff (request threshold ÷ ratio = ~83.3 req/s); quieter baselines require more | 1.2 |
| `--csv FILE` | Export to CSV | - |
| `--json FILE` | Export to JSON | - |
| `--token TOKEN` | Prometheus bearer token (resolved via `oc` when omitted) | - |
| `-e, --events MINUTES` | Event history window for informational output | 60 |
| `-q, --quiet` | Minimal output | false |
| `--no-ml-check` | Skip the ML/GPU node check | false |

The variance and memory-movement parameters are fleet-calibration knobs; the defaults are provisional.

## Features

### Time-Windowed Metrics
Queries Prometheus for windowed per-node CPU and memory series over the last N minutes instead of just current instant values. The default window of 7 days matches what every real caller passes; a short window leaves the variance rule with too few 15-minute windows to establish a baseline.

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
timestamp,cluster,status,cpu_result,cpu_value,memory_result,...,gpu_result,gpu_value,operators_result,...,has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,...,gpu_cpu_windowed,gpu_mem_windowed
```

**JSON Format:**
```json
{
  "status": "IDLE",
  "criteria": {
    "cpu": { "result": "IDLE", "value": 9.0, "threshold_ratio": 0.47 },
    "gpu": { "result": "ACTIVE", "value": 80.0, "threshold_ratio": 2.0 },
    "operators": { "result": "IDLE", "age_days": 12.0, "events": 0, "threshold_ratio": 0.58 },
    "driver": { "criterion": "gpu", "label": "GPU", "threshold_ratio": 2.0, "quorum_driven": false },
    ...
  },
  "gpu": {
    "has_gpu_nodes": true,
    "node_count": 1,
    "flavors": "NVIDIA(g4dn.xlarge)",
    "usage": { "cpu_current": "7.00", "cpu_windowed": "6.58", ... }
  },
  "oversized": { "result": false, "gpu_peak": null, "nodes": [] }
}
```

`criteria` keeps each criterion's headline fields — `result`, `value`, `threshold_ratio`, plus `age_days` and `events` for operators — so renderers that print it verbatim (the Jenkins report's Metrics column) stay compact. The diagnostics behind those fields (per-node variance numbers, operator units, failure reasons) are not exported.

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
STATUS: IDLE (closest to active: API Server at 42% of threshold)
```

## Configuration

Defaults live in `src/ocp_idle_check/config.py`; prefer overriding them with the command-line options above.

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

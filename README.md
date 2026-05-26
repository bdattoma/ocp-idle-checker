# OpenShift Cluster Idle Detection Script

Detects if your OpenShift cluster is idle based on CPU, memory, API server activity, and operator age. Supports time-windowed metrics and GPU node detection.

## Quick Start

```bash
# Default check (10-minute window)
./ocp-idle-check.sh

# Quiet mode with export
./ocp-idle-check.sh -q --csv results.csv --json results.json

# Custom thresholds
./ocp-idle-check.sh -w 30 -c 15 -m 35 -a 50
```

## Exit Codes

- **0** = Cluster is ACTIVE (success - resources are being used)
- **1** = Cluster is IDLE (warning - resources may be wasted)
- **2** = Error

## Idle Criteria (80% must pass)

1. **CPU** < 15% (time-windowed average)
2. **Memory** < 35% (time-windowed average)
3. **API Server** < 100 req/sec
4. **Operators** ≥ 7 days old with low activity

## Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `-w, --window MINUTES` | Time window for averages | 10 |
| `-c, --cpu-threshold N` | CPU idle threshold (%) | 15 |
| `-m, --mem-threshold N` | Memory idle threshold (%) | 35 |
| `-a, --api-threshold N` | API requests/sec threshold | 100 |
| `-o, --operator-age N` | Operator age threshold (days) | 7 |
| `--operator-namespaces NS` | Operator namespaces to check | opendatahub,redhat-ods-operator,redhat-ods-applications |
| `--csv FILE` | Export to CSV | - |
| `--json FILE` | Export to JSON | - |
| `-q, --quiet` | Minimal output | false |
| `--no-ml-check` | Skip GPU/ML checks | false |

## Features

### Time-Windowed Metrics
Queries Prometheus to get average CPU/Memory over the last N minutes instead of just current instant values. Helps distinguish between temporary pauses and sustained idle periods.

```bash
# Check if idle for 30 minutes
./ocp-idle-check.sh -w 30
```

### GPU Node Detection
Automatically detects GPU nodes (NVIDIA/AMD) and reports:
- GPU count and type
- Instance flavor (e.g., g4dn.xlarge, p5.48xlarge)
- Node age
- CPU and memory usage (current + windowed)

### Export Results
Export to CSV (append mode) or JSON for automation and historical tracking.

**CSV Format:**
```csv
timestamp,cluster,status,cpu_result,cpu_value,memory_result,...,has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,...
```

**JSON Format:**
```json
{
  "status": "IDLE",
  "criteria": { "cpu": {...}, "memory": {...}, "api_server": {...} },
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
./ocp-idle-check.sh -w 30 -c 5

if [ $? -eq 1 ]; then
    echo "Cluster idle for 30+ minutes, consider releasing reservation"
fi
```

### Scheduled Monitoring
```bash
# Cron: check every 15 minutes, log to CSV
*/15 * * * * /path/to/ocp-idle-check.sh -q --csv /var/log/ocp-idle-history.csv
```

### Pre-Maintenance Check
```bash
if ! ./ocp-idle-check.sh -w 20; then
    echo "Cluster idle, safe for maintenance"
    # Your maintenance tasks
else
    echo "Cluster active, skip maintenance"
fi
```

## Dependencies

**Required:**
- `oc` (OpenShift CLI) - logged in to cluster
- `jq` - JSON processing
- `curl` - Prometheus queries
- Prometheus/Thanos - for time-windowed metrics

**Install:**
```bash
# Fedora/RHEL
dnf install jq curl

# Ubuntu/Debian
apt install jq curl
```

## Examples

```bash
# Verbose output with 30-minute window
./ocp-idle-check.sh -w 30

# Quiet mode for automation
./ocp-idle-check.sh -q

# Export with custom thresholds
./ocp-idle-check.sh -w 15 -c 10 -m 40 -a 50 --csv /tmp/results.csv

# Check specific operators
./ocp-idle-check.sh --operator-namespaces "openshift-operators,my-app-operator"

# Skip GPU checks
./ocp-idle-check.sh --no-ml-check
```

## Output Modes

### Verbose (default)
Shows full details: node lists, metrics, operator pods, GPU information, recent events.

### Quiet (`-q`)
Minimal output:
```
CPU: IDLE
Memory: ACTIVE
API Server: IDLE
Operators: IDLE
STATUS: IDLE
```

## Configuration

Edit script defaults at the top:
```bash
CPU_IDLE_THRESHOLD=15
MEMORY_IDLE_THRESHOLD=35
APISERVER_IDLE_THRESHOLD=100
OPERATOR_IDLE_AGE_DAYS=7
OPERATOR_NAMESPACES="opendatahub,redhat-ods-operator,redhat-ods-applications"
TIME_WINDOW_MINUTES=10
ML_NODE_PATTERN="p5|p4d|g5"
```

## Troubleshooting

**"Cannot retrieve CPU metrics"**
- Check Prometheus/Thanos is available: `oc get pods -n openshift-monitoring`
- Verify metrics-server is running

**"jq not found"**
- Install jq: `dnf install jq` or `apt install jq`

**Script hangs**
- Built-in 10-second timeouts on all `oc` commands
- If persistent, check cluster API responsiveness

#!/bin/bash
#
# OpenShift Cluster Idle Detection Script
# Checks if an OCP cluster is idle based on CPU, memory, pod activity, and events
#
# Exit codes:
#   0 = Cluster is IDLE
#   1 = Cluster is ACTIVE
#   2 = Error (cannot determine state)
#

set -uo pipefail

# === CONFIGURATION ===
CPU_IDLE_THRESHOLD=15          # CPU usage below this % is considered idle (raised for system overhead)
MEMORY_IDLE_THRESHOLD=35       # Memory usage below this % is considered idle
APISERVER_IDLE_THRESHOLD=100    # API server requests/sec below this is considered idle
OPERATOR_IDLE_AGE_DAYS=7       # If operator pods are older than this, cluster is likely idle
OPERATOR_NAMESPACES="opendatahub,redhat-ods-operator,redhat-ods-applications"  # Comma-separated list of operator namespaces to check
EVENT_TIME_MINUTES=60          # Check events in last N minutes (informational only)
TIME_WINDOW_MINUTES=10         # Time window for CPU/Memory average calculations (0 = instant only)
CHECK_ML_NODES=true            # Set to false to disable ML node specific checks
ML_NODE_PATTERN="p5|p4d|g5"    # Instance types to consider as ML nodes
VERBOSE=true                   # Set to false for minimal output
EXPORT_CSV=""                  # Path to export CSV file (empty = no export)
EXPORT_JSON=""                 # Path to export JSON file (empty = no export)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# === COMMAND LINE ARGUMENTS ===
show_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

OpenShift Cluster Idle Detection Script

Options:
  -w, --window MINUTES       Time window for CPU/Memory averages (default: $TIME_WINDOW_MINUTES)
                            Set to 0 for instant metrics only
  -c, --cpu-threshold N      CPU idle threshold percentage (default: $CPU_IDLE_THRESHOLD)
  -m, --mem-threshold N      Memory idle threshold percentage (default: $MEMORY_IDLE_THRESHOLD)
  -a, --api-threshold N      API server requests/sec threshold (default: $APISERVER_IDLE_THRESHOLD)
  -e, --events MINUTES       Event history window (default: $EVENT_TIME_MINUTES)
  -o, --operator-age N       Operator pod age threshold in days (default: $OPERATOR_IDLE_AGE_DAYS)
  --operator-namespaces NS   Comma-separated operator namespaces to check
                            (default: $OPERATOR_NAMESPACES)
  --csv FILE                Export results to CSV file
  --json FILE               Export results to JSON file
  -q, --quiet               Quiet mode - show only criteria results and status
  --no-ml-check             Skip ML node specific checks
  -h, --help                Show this help message

Examples:
  $0 -w 30                                    # Check average over last 30 minutes
  $0 -q                                       # Quiet mode - minimal output
  $0 -w 10 -c 15 -m 35                        # 10 min window, 15% CPU, 35% mem thresholds
  $0 -a 20                                    # Consider idle if API requests < 20/sec
  $0 -o 30                                    # Consider idle if operators unchanged for 30+ days
  $0 --operator-namespaces "openshift-operators"  # Check custom operator namespace
  $0 -q --csv results.csv --json results.json     # Export to CSV and JSON files

EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -w|--window)
            TIME_WINDOW_MINUTES="$2"
            shift 2
            ;;
        -c|--cpu-threshold)
            CPU_IDLE_THRESHOLD="$2"
            shift 2
            ;;
        -m|--mem-threshold)
            MEMORY_IDLE_THRESHOLD="$2"
            shift 2
            ;;
        -a|--api-threshold)
            APISERVER_IDLE_THRESHOLD="$2"
            shift 2
            ;;
        -e|--events)
            EVENT_TIME_MINUTES="$2"
            shift 2
            ;;
        -o|--operator-age)
            OPERATOR_IDLE_AGE_DAYS="$2"
            shift 2
            ;;
        --operator-namespaces)
            OPERATOR_NAMESPACES="$2"
            shift 2
            ;;
        --csv)
            EXPORT_CSV="$2"
            shift 2
            ;;
        --json)
            EXPORT_JSON="$2"
            shift 2
            ;;
        -q|--quiet)
            VERBOSE=false
            shift
            ;;
        --no-ml-check)
            CHECK_ML_NODES=false
            shift
            ;;
        -h|--help)
            show_usage
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information"
            exit 2
            ;;
    esac
done

# === FUNCTIONS ===

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1" >&2
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1" >&2
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

check_oc_command() {
    if ! command -v oc &> /dev/null; then
        log_error "oc command not found. Please install OpenShift CLI."
        exit 2
    fi

    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift cluster. Run 'oc login' first."
        exit 2
    fi
}

check_dependencies() {
    # Check for required tools for Prometheus queries
    if ! command -v jq &> /dev/null; then
        log_error "jq not found. This tool is required for Prometheus queries."
        log_error "Install jq: dnf install jq / apt install jq"
        exit 2
    fi

    if ! command -v curl &> /dev/null; then
        log_error "curl not found. This tool is required for Prometheus queries."
        log_error "Install curl: dnf install curl / apt install curl"
        exit 2
    fi
}

query_prometheus() {
    # Query Prometheus/Thanos for metrics
    # Args: $1 = PromQL query
    local query="$1"
    local result

    # Try to get thanos-querier route
    local thanos_host
    thanos_host=$(timeout 5 oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}' 2>/dev/null || echo "")

    if [[ -z "$thanos_host" ]]; then
        echo "N/A"
        return 1
    fi

    # Try bearer token first (works for regular oc login sessions).
    # Falls back to minting a short-lived SA token
    local token
    token=$(oc whoami -t 2>/dev/null || echo "")

    if [[ -z "$token" ]]; then
        [[ "$VERBOSE" == "true" ]] && log_info "query_prometheus: no OAuth token, minting prometheus-k8s SA token"
        token=$(oc create token prometheus-k8s -n openshift-monitoring --duration=10m 2>/dev/null)
        rc=$?
        if [[ $rc -ne 0 || -z "$token" ]]; then
            [[ "$VERBOSE" == "true" ]] && log_error "query_prometheus: failed to create SA token (exit $rc)"
            echo "N/A"
            return 1
        fi
    fi

    if [[ -z "$token" ]]; then
        echo "N/A"
        return 1
    fi

    # Query Prometheus
    result=$(timeout 15 curl -sk -H "Authorization: Bearer $token" \
        "https://$thanos_host/api/v1/query?query=$(echo "$query" | jq -sRr @uri)" 2>/dev/null | \
        jq -r '.data.result[0].value[1] // "N/A"' 2>/dev/null || echo "N/A")

    echo "$result"
}

get_node_cpu_usage_windowed() {
    # Get average CPU usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"
    local query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[${window}]))) * 100"
    local result

    result=$(query_prometheus "$query")

    if [[ "$result" == "N/A" ]]; then
        echo "N/A"
    else
        # Round to 2 decimal places
        awk -v val="$result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_node_memory_usage_windowed() {
    # Get average memory usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"
    local query="(1 - avg_over_time((sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))[${window}:])) * 100"
    local result

    result=$(query_prometheus "$query")

    if [[ "$result" == "N/A" ]]; then
        echo "N/A"
    else
        # Round to 2 decimal places
        awk -v val="$result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_apiserver_request_rate() {
    # Get API server request rate over time window using Prometheus
    # Returns requests per second
    local window="${TIME_WINDOW_MINUTES}m"
    local query

    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        # Get rate of API requests over the time window
        query="sum(rate(apiserver_request_total[${window}]))"
    else
        # Instant rate (5m default for rate function)
        query="sum(rate(apiserver_request_total[5m]))"
    fi

    local result
    result=$(query_prometheus "$query")

    if [[ "$result" == "N/A" ]]; then
        echo "N/A"
    else
        # Round to 2 decimal places
        awk -v val="$result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_apiserver_request_rate_breakdown() {
    # Get breakdown of API requests by verb (GET, POST, etc.) for verbose output
    local window="${TIME_WINDOW_MINUTES}m"
    local query

    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        query="sum by (verb) (rate(apiserver_request_total[${window}]))"
    else
        query="sum by (verb) (rate(apiserver_request_total[5m]))"
    fi

    local result
    result=$(query_prometheus "$query" 2>/dev/null || echo "N/A")

    if [[ "$result" == "N/A" ]]; then
        echo "N/A"
    else
        # Try to get the full JSON response for breakdown
        local thanos_host
        thanos_host=$(timeout 5 oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}' 2>/dev/null || echo "")

        if [[ -n "$thanos_host" ]]; then
            local token
            token=$(oc whoami -t 2>/dev/null || echo "")

            if [[ -n "$token" ]]; then
                timeout 15 curl -sk -H "Authorization: Bearer $token" \
                    "https://$thanos_host/api/v1/query?query=$(echo "$query" | jq -sRr @uri)" 2>/dev/null | \
                    jq -r '.data.result[] | "\(.metric.verb): \(.value[1])"' 2>/dev/null | \
                    awk '{printf "  %s (%.2f req/s)\n", $1, $2}' || echo "N/A"
            fi
        fi
    fi
}

get_node_cpu_usage() {
    # Returns average CPU usage across all nodes
    local cpu_data
    cpu_data=$(timeout 10 oc adm top nodes --no-headers 2>/dev/null | awk '{gsub(/%/,"",$3); sum+=$3; count++} END {if(count>0) print sum/count; else print "N/A"}' || echo "N/A")
    echo "$cpu_data"
}

get_node_memory_usage() {
    # Returns average memory usage across all nodes
    local mem_data
    mem_data=$(timeout 10 oc adm top nodes --no-headers 2>/dev/null | awk '{gsub(/%/,"",$5); sum+=$5; count++} END {if(count>0) print sum/count; else print "N/A"}' || echo "N/A")
    echo "$mem_data"
}

get_ml_node_usage() {
    # Check ML nodes specifically if they exist (instant metrics)
    local ml_nodes
    ml_nodes=$(timeout 10 oc get nodes -o json 2>/dev/null | jq -r ".items[] | select(.metadata.labels.\"node.kubernetes.io/instance-type\" | test(\"$ML_NODE_PATTERN\")) | .metadata.name" 2>/dev/null || echo "")

    if [[ -z "$ml_nodes" ]]; then
        echo "N/A"
        return
    fi

    local ml_cpu_sum=0
    local ml_mem_sum=0
    local count=0

    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local node_stats
            node_stats=$(timeout 10 oc adm top node "$node" --no-headers 2>/dev/null || echo "")
            if [[ -n "$node_stats" ]]; then
                local cpu=$(echo "$node_stats" | awk '{gsub(/%/,"",$3); print $3}')
                local mem=$(echo "$node_stats" | awk '{gsub(/%/,"",$5); print $5}')
                ml_cpu_sum=$(awk -v sum="$ml_cpu_sum" -v val="$cpu" 'BEGIN {print sum + val}')
                ml_mem_sum=$(awk -v sum="$ml_mem_sum" -v val="$mem" 'BEGIN {print sum + val}')
                ((count++))
            fi
        fi
    done <<< "$ml_nodes"

    if [[ $count -gt 0 ]]; then
        local ml_cpu_avg=$(awk -v sum="$ml_cpu_sum" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        local ml_mem_avg=$(awk -v sum="$ml_mem_sum" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        echo "${ml_cpu_avg}%CPU,${ml_mem_avg}%MEM"
    else
        echo "N/A"
    fi
}

get_ml_node_usage_windowed() {
    # Check ML nodes CPU usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"

    # Get ML node names
    local ml_nodes
    ml_nodes=$(timeout 10 oc get nodes -o json 2>/dev/null | jq -r ".items[] | select(.metadata.labels.\"node.kubernetes.io/instance-type\" | test(\"$ML_NODE_PATTERN\")) | .metadata.name" 2>/dev/null || echo "")

    if [[ -z "$ml_nodes" ]]; then
        echo "N/A"
        return
    fi

    # Build node filter regex
    local node_filter=$(echo "$ml_nodes" | tr '\n' '|' | sed 's/|$//')

    # Query Prometheus for ML node CPU
    local query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\",instance=~\"${node_filter}.*\"}[${window}]))) * 100"
    local cpu_result=$(query_prometheus "$query")

    if [[ "$cpu_result" == "N/A" ]]; then
        echo "N/A"
    else
        cpu_result=$(awk -v val="$cpu_result" 'BEGIN {printf "%.2f", val}')
        echo "${cpu_result}%CPU"
    fi
}

get_gpu_nodes() {
    # Check for GPU nodes by looking at capacity/allocatable resources
    local gpu_nodes=""
    local gpu_type=""
    local gpu_count=0

    # Check for NVIDIA GPUs
    local nvidia_nodes=$(timeout 10 oc get nodes -o json 2>/dev/null | \
        jq -r '.items[] | select(.status.capacity["nvidia.com/gpu"] != null) | .metadata.name' 2>/dev/null || echo "")

    if [[ -n "$nvidia_nodes" ]]; then
        gpu_type="NVIDIA"
        gpu_count=$(echo "$nvidia_nodes" | wc -l)
        gpu_nodes="$nvidia_nodes"
    fi

    # Check for AMD GPUs if no NVIDIA found
    if [[ -z "$gpu_nodes" ]]; then
        local amd_nodes=$(timeout 10 oc get nodes -o json 2>/dev/null | \
            jq -r '.items[] | select(.status.capacity["amd.com/gpu"] != null) | .metadata.name' 2>/dev/null || echo "")

        if [[ -n "$amd_nodes" ]]; then
            gpu_type="AMD"
            gpu_count=$(echo "$amd_nodes" | wc -l)
            gpu_nodes="$amd_nodes"
        fi
    fi

    if [[ -z "$gpu_nodes" ]]; then
        echo "N/A:0:N/A"
    else
        echo "$gpu_nodes:$gpu_count:$gpu_type"
    fi
}

get_gpu_machines() {
    # Check for GPU machines by pattern <clustername>-*-gpu-*
    local cluster_name
    cluster_name=$(oc whoami --show-server 2>/dev/null | sed 's/.*api\.\(.*\):.*/\1/' | cut -d'.' -f1 || echo "")

    if [[ -z "$cluster_name" ]]; then
        echo "N/A:0"
        return
    fi

    # Look for machines with gpu pattern
    local gpu_machines=$(timeout 10 oc get machines -n openshift-machine-api -o json 2>/dev/null | \
        jq -r ".items[] | select(.metadata.name | test(\"${cluster_name}-.*-gpu-\")) | .metadata.name" 2>/dev/null || echo "")

    if [[ -z "$gpu_machines" ]]; then
        echo "N/A:0"
    else
        local machine_count=$(echo "$gpu_machines" | wc -l)
        echo "$gpu_machines:$machine_count"
    fi
}

get_gpu_info() {
    # Get comprehensive GPU information
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)
    local node_count=$(echo "$gpu_node_info" | cut -d':' -f2)
    local gpu_type=$(echo "$gpu_node_info" | cut -d':' -f3)

    if [[ "$nodes" == "N/A" ]]; then
        # No GPU nodes found, check for GPU machines
        local gpu_machine_info=$(get_gpu_machines)
        local machines=$(echo "$gpu_machine_info" | cut -d':' -f1)
        local machine_count=$(echo "$gpu_machine_info" | cut -d':' -f2)

        if [[ "$machines" == "N/A" ]]; then
            echo "N/A:0:N/A:0"
        else
            echo "N/A:0:$machines:$machine_count"
        fi
    else
        # GPU nodes found
        echo "$nodes:$node_count:N/A:0"
    fi
}

get_gpu_node_age() {
    # Get the age of GPU nodes (oldest GPU node)
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    local oldest_age=""
    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local node_age=$(oc get node "$node" -o jsonpath='{.metadata.creationTimestamp}' 2>/dev/null)
            if [[ -n "$node_age" ]] && [[ -z "$oldest_age" || "$node_age" < "$oldest_age" ]]; then
                oldest_age="$node_age"
            fi
        fi
    done <<< "$nodes"

    if [[ -n "$oldest_age" ]]; then
        echo "$oldest_age"
    else
        echo "N/A"
    fi
}

get_gpu_flavors() {
    # Get GPU flavors/types from all GPU nodes
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    local flavors=""
    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local gpu_type=$(oc get node "$node" -o json 2>/dev/null | \
                jq -r 'if .status.capacity["nvidia.com/gpu"] != null then "NVIDIA" elif .status.capacity["amd.com/gpu"] != null then "AMD" else "Unknown" end' 2>/dev/null)
            local instance_type=$(oc get node "$node" -o jsonpath='{.metadata.labels.node\.kubernetes\.io/instance-type}' 2>/dev/null)

            local flavor="${gpu_type}"
            if [[ -n "$instance_type" ]]; then
                flavor="${flavor}(${instance_type})"
            fi

            if [[ -z "$flavors" ]]; then
                flavors="$flavor"
            elif [[ "$flavors" != *"$flavor"* ]]; then
                flavors="${flavors},${flavor}"
            fi
        fi
    done <<< "$nodes"

    if [[ -n "$flavors" ]]; then
        echo "$flavors"
    else
        echo "N/A"
    fi
}

get_gpu_node_usage() {
    # Get instant CPU and memory usage for GPU nodes
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    local total_cpu=0
    local total_mem=0
    local count=0

    while IFS= read -r node; do
        if [[ -n "$node" ]]; then
            local node_stats
            node_stats=$(timeout 10 oc adm top node "$node" --no-headers 2>/dev/null || echo "")
            if [[ -n "$node_stats" ]]; then
                local cpu=$(echo "$node_stats" | awk '{gsub(/%/,"",$3); print $3}')
                local mem=$(echo "$node_stats" | awk '{gsub(/%/,"",$5); print $5}')
                total_cpu=$(awk -v sum="$total_cpu" -v val="$cpu" 'BEGIN {print sum + val}')
                total_mem=$(awk -v sum="$total_mem" -v val="$mem" 'BEGIN {print sum + val}')
                ((count++))
            fi
        fi
    done <<< "$nodes"

    if [[ $count -gt 0 ]]; then
        local instant_cpu=$(awk -v sum="$total_cpu" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        local instant_mem=$(awk -v sum="$total_mem" -v cnt="$count" 'BEGIN {printf "%.2f", sum / cnt}')
        echo "${instant_cpu}%CPU,${instant_mem}%MEM"
    else
        echo "N/A"
    fi
}

get_gpu_node_cpu_usage_windowed() {
    # Get GPU nodes CPU usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"

    # Get GPU node names
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    # Build node filter regex
    local node_filter=$(echo "$nodes" | tr '\n' '|' | sed 's/|$//')

    # Query Prometheus for GPU node CPU
    local query="(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\",instance=~\"${node_filter}.*\"}[${window}]))) * 100"
    local cpu_result=$(query_prometheus "$query")

    if [[ "$cpu_result" == "N/A" ]]; then
        echo "N/A"
    else
        awk -v val="$cpu_result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_gpu_node_memory_usage_windowed() {
    # Get GPU nodes memory usage over time window using Prometheus
    local window="${TIME_WINDOW_MINUTES}m"

    # Get GPU node names
    local gpu_node_info=$(get_gpu_nodes)
    local nodes=$(echo "$gpu_node_info" | cut -d':' -f1)

    if [[ "$nodes" == "N/A" ]]; then
        echo "N/A"
        return
    fi

    # Build node filter regex for memory query
    local node_filter=$(echo "$nodes" | tr '\n' '|' | sed 's/|$//')

    # Query Prometheus for GPU node memory usage
    # Calculate: (1 - (available / total)) * 100
    local query="(1 - avg_over_time((avg(node_memory_MemAvailable_bytes{instance=~\"${node_filter}.*\"}) / avg(node_memory_MemTotal_bytes{instance=~\"${node_filter}.*\"}))[${window}:])) * 100"
    local mem_result=$(query_prometheus "$query")

    if [[ "$mem_result" == "N/A" ]]; then
        echo "N/A"
    else
        awk -v val="$mem_result" 'BEGIN {printf "%.2f", val}'
    fi
}

get_pod_counts() {
    local total_pods
    local running_pods

    total_pods=$(timeout 10 oc get pods -A --no-headers 2>/dev/null | wc -l || echo 0)
    running_pods=$(timeout 10 oc get pods -A --no-headers 2>/dev/null | grep -c "Running" || echo 0)

    echo "${total_pods}:${running_pods}"
}

convert_age_to_days() {
    # Convert Kubernetes age format to days
    local age="$1"

    if [[ "$age" =~ ^([0-9]+)d ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "$age" =~ ^([0-9]+)h ]]; then
        echo "0"
    elif [[ "$age" =~ ^([0-9]+)m ]]; then
        echo "0"
    elif [[ "$age" =~ ^([0-9]+)s ]]; then
        echo "0"
    else
        echo "0"
    fi
}

get_operator_age() {
    # Check operator pods in specified namespaces and return their age
    local operator_info=""
    local oldest_age_days=0
    local found_operators=""

    # Split comma-separated namespaces
    IFS=',' read -ra NAMESPACES <<< "$OPERATOR_NAMESPACES"

    for ns in "${NAMESPACES[@]}"; do
        # Trim whitespace
        ns=$(echo "$ns" | xargs)

        # Check if namespace exists
        if ! timeout 5 oc get namespace "$ns" &>/dev/null; then
            continue
        fi

        # Get operator pods from this namespace
        local ns_pods=$(timeout 10 oc get pods -n "$ns" --no-headers 2>/dev/null | grep -E "controller-manager|operator|dashboard" | head -5)

        if [[ -n "$ns_pods" ]]; then
            while IFS= read -r pod; do
                local pod_name=$(echo "$pod" | awk '{print $1}')
                local pod_age=$(echo "$pod" | awk '{print $5}')
                local age_days=$(convert_age_to_days "$pod_age")

                if [[ $age_days -gt $oldest_age_days ]]; then
                    oldest_age_days=$age_days
                fi
            done <<< "$ns_pods"

            # Track which namespaces had operators
            if [[ -z "$found_operators" ]]; then
                found_operators="$ns"
            else
                found_operators="${found_operators},$ns"
            fi
        fi
    done

    if [[ -z "$found_operators" ]]; then
        echo "N/A:0"
    else
        operator_info="${found_operators}:${oldest_age_days}d"
        echo "$operator_info"
    fi
}

check_operator_events() {
    # Check for recent operator reconciliation or significant events
    local total_events=0

    # Split comma-separated namespaces
    IFS=',' read -ra NAMESPACES <<< "$OPERATOR_NAMESPACES"

    for ns in "${NAMESPACES[@]}"; do
        # Trim whitespace
        ns=$(echo "$ns" | xargs)

        # Check if namespace exists
        if ! timeout 5 oc get namespace "$ns" &>/dev/null; then
            continue
        fi

        # Check operator events in this namespace
        local ns_events=$(timeout 10 oc get events -n "$ns" --sort-by='.lastTimestamp' 2>/dev/null | \
            tail -20 | \
            grep -Eic "reconcil|created|updated|scaled" 2>/dev/null || echo 0)

        # Sanitize values (remove whitespace, ensure numeric)
        ns_events=$(echo "$ns_events" | tr -d '[:space:]' | grep -o '[0-9]*' || echo 0)
        ns_events=${ns_events:-0}

        total_events=$((total_events + ns_events))
    done

    echo "$total_events"
}

get_recent_pod_activity() {
    # Count pod-related events in the last N minutes (simplified)
    local pod_events
    local event_output

    # Just count recent events, simplified approach
    event_output=$(timeout 10 oc get events -A --sort-by='.lastTimestamp' 2>/dev/null | tail -20)

    if [[ -n "$event_output" ]]; then
        pod_events=$(echo "$event_output" | grep -Ec "Pod|Deployment|ReplicaSet|Job" 2>/dev/null || echo 0)
    else
        pod_events=0
    fi

    # Sanitize: remove whitespace and ensure numeric, take first number only
    pod_events=$(echo "$pod_events" | head -1 | tr -d '[:space:]' | grep -o '^[0-9]*' || echo 0)
    pod_events=${pod_events:-0}

    echo "$pod_events"
}

check_api_activity() {
    # Check for recent API requests (excluding system components)
    local api_requests
    api_requests=$(oc get events -A --field-selector involvedObject.kind=Deployment,involvedObject.kind=StatefulSet,involvedObject.kind=Job --sort-by='.lastTimestamp' 2>/dev/null | tail -n +2 | head -n 1 | wc -l)
    echo "$api_requests"
}

# === MAIN SCRIPT ===

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
    echo "========================================"
    echo "  OpenShift Cluster Idle Detection"
    echo "========================================"
    echo ""
fi

# Check prerequisites
check_oc_command
check_dependencies

if [[ "$VERBOSE" == "true" ]]; then
    # Get cluster name
    CLUSTER_NAME=$(oc whoami --show-server 2>/dev/null || echo "Unknown")
    log_info "Cluster: $CLUSTER_NAME"
    log_info "Timestamp: $(date)"
    echo ""

    # Display configuration
    echo "Configuration:"
    echo "  CPU Idle Threshold: < ${CPU_IDLE_THRESHOLD}%"
    echo "  Memory Idle Threshold: < ${MEMORY_IDLE_THRESHOLD}%"
    echo "  API Server Threshold: < ${APISERVER_IDLE_THRESHOLD} req/sec"
    echo "  Operator Idle Age: >= ${OPERATOR_IDLE_AGE_DAYS} days"
    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        echo "  Time Window: Last ${TIME_WINDOW_MINUTES} minutes"
    else
        echo "  Time Window: Instant metrics only"
    fi
    echo "  Event History: Last ${EVENT_TIME_MINUTES} minutes"
    echo ""
fi

# Initialize idle criteria counters
idle_criteria_met=0
total_criteria=0

# === CHECK 1: Node CPU Usage ===
((total_criteria++))
cpu_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Node CPU Usage ---"
fi

# Get instant CPU usage
instant_cpu=$(get_node_cpu_usage)

# Get time-windowed CPU usage if time window is enabled
cpu_windowed="N/A"
if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
    cpu_windowed=$(get_node_cpu_usage_windowed)
fi

if [[ "$instant_cpu" == "N/A" ]] && [[ "$cpu_windowed" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_warning "Cannot retrieve CPU metrics. Metrics server may not be available."
    fi
    cpu_result="UNKNOWN"
else
    if [[ "$VERBOSE" == "true" ]]; then
        # Display instant metrics
        if [[ "$instant_cpu" != "N/A" ]]; then
            echo "Current CPU Usage: ${instant_cpu}%"
            timeout 10 oc adm top nodes 2>/dev/null | head -n 10 || log_warning "Could not display node details"
        fi

        # Display windowed metrics
        if [[ "$cpu_windowed" != "N/A" ]]; then
            echo "Average CPU (last ${TIME_WINDOW_MINUTES} min): ${cpu_windowed}%"
        fi
    fi

    # Determine which metric to use for idle check (prefer windowed if available)
    cpu_to_check="$instant_cpu"
    if [[ "$cpu_windowed" != "N/A" ]]; then
        cpu_to_check="$cpu_windowed"
        if [[ "$VERBOSE" == "true" ]]; then
            echo "Using time-windowed average for idle detection"
        fi
    fi

    # Use awk for comparison
    if [[ "$cpu_to_check" != "N/A" ]]; then
        if awk -v cpu="$cpu_to_check" -v threshold="$CPU_IDLE_THRESHOLD" 'BEGIN {exit !(cpu < threshold)}'; then
            if [[ "$VERBOSE" == "true" ]]; then
                log_success "CPU usage is IDLE (< ${CPU_IDLE_THRESHOLD}%)"
            fi
            cpu_result="IDLE"
            ((idle_criteria_met++))
        else
            if [[ "$VERBOSE" == "true" ]]; then
                log_warning "CPU usage is ACTIVE (>= ${CPU_IDLE_THRESHOLD}%)"
            fi
            cpu_result="ACTIVE"
        fi
    fi
fi

if [[ "$VERBOSE" == "false" ]]; then
    echo "CPU: $cpu_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === CHECK 2: Node Memory Usage ===
((total_criteria++))
mem_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Node Memory Usage ---"
fi

# Get instant memory usage
instant_mem=$(get_node_memory_usage)

# Get time-windowed memory usage if time window is enabled
mem_windowed="N/A"
if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
    mem_windowed=$(get_node_memory_usage_windowed)
fi

if [[ "$instant_mem" == "N/A" ]] && [[ "$mem_windowed" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_warning "Cannot retrieve memory metrics."
    fi
    mem_result="UNKNOWN"
else
    if [[ "$VERBOSE" == "true" ]]; then
        # Display instant metrics
        if [[ "$instant_mem" != "N/A" ]]; then
            echo "Current Memory Usage: ${instant_mem}%"
        fi

        # Display windowed metrics
        if [[ "$mem_windowed" != "N/A" ]]; then
            echo "Average Memory (last ${TIME_WINDOW_MINUTES} min): ${mem_windowed}%"
        fi
    fi

    # Determine which metric to use for idle check (prefer windowed if available)
    mem_to_check="$instant_mem"
    if [[ "$mem_windowed" != "N/A" ]]; then
        mem_to_check="$mem_windowed"
        if [[ "$VERBOSE" == "true" ]]; then
            echo "Using time-windowed average for idle detection"
        fi
    fi

    # Use awk for comparison
    if [[ "$mem_to_check" != "N/A" ]]; then
        if awk -v mem="$mem_to_check" -v threshold="$MEMORY_IDLE_THRESHOLD" 'BEGIN {exit !(mem < threshold)}'; then
            if [[ "$VERBOSE" == "true" ]]; then
                log_success "Memory usage is IDLE (< ${MEMORY_IDLE_THRESHOLD}%)"
            fi
            mem_result="IDLE"
            ((idle_criteria_met++))
        else
            if [[ "$VERBOSE" == "true" ]]; then
                log_warning "Memory usage is ACTIVE (>= ${MEMORY_IDLE_THRESHOLD}%)"
            fi
            mem_result="ACTIVE"
        fi
    fi
fi

if [[ "$VERBOSE" == "false" ]]; then
    echo "Memory: $mem_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === CHECK 3: API Server Request Rate ===
((total_criteria++))
api_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- API Server Request Rate ---"
fi

api_rate=$(get_apiserver_request_rate)

if [[ "$api_rate" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_warning "Cannot retrieve API server metrics from Prometheus"
    fi
    api_result="UNKNOWN"
    # Don't count this criteria if we can't get metrics
    ((total_criteria--))
else
    if [[ "$VERBOSE" == "true" ]]; then
        echo "API Server Request Rate: ${api_rate} req/sec"

        # Show breakdown by verb if available
        if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
            echo "Request breakdown (last ${TIME_WINDOW_MINUTES} min):"
        else
            echo "Request breakdown (last 5 min):"
        fi
        get_apiserver_request_rate_breakdown 2>/dev/null || echo "  Breakdown not available"
        echo ""
    fi

    # Compare against threshold
    if awk -v rate="$api_rate" -v threshold="$APISERVER_IDLE_THRESHOLD" 'BEGIN {exit !(rate < threshold)}'; then
        if [[ "$VERBOSE" == "true" ]]; then
            log_success "API server request rate is IDLE (< ${APISERVER_IDLE_THRESHOLD} req/sec)"
        fi
        api_result="IDLE"
        ((idle_criteria_met++))
    else
        if [[ "$VERBOSE" == "true" ]]; then
            log_warning "API server request rate is ACTIVE (>= ${APISERVER_IDLE_THRESHOLD} req/sec)"
        fi
        api_result="ACTIVE"
    fi
fi

if [[ "$VERBOSE" == "false" ]] && [[ "$api_result" != "UNKNOWN" ]]; then
    echo "API Server: $api_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === CHECK 4: GPU/ML Node Detection (informational only) ===
if [[ "$CHECK_ML_NODES" == "true" ]] && [[ "$VERBOSE" == "true" ]]; then
    echo "--- GPU/ML Node Detection ---"

    # Get GPU information
    gpu_info=$(get_gpu_info)
    gpu_nodes=$(echo "$gpu_info" | cut -d':' -f1)
    gpu_node_count=$(echo "$gpu_info" | cut -d':' -f2)
    gpu_machines=$(echo "$gpu_info" | cut -d':' -f3)
    gpu_machine_count=$(echo "$gpu_info" | cut -d':' -f4)

    # Display GPU detection results
    if [[ "$gpu_nodes" != "N/A" ]]; then
        echo "GPU Nodes Found: $gpu_node_count"
        while IFS= read -r node; do
            if [[ -n "$node" ]]; then
                gpu_capacity=$(oc get node "$node" -o json 2>/dev/null | \
                    jq -r '.status.capacity["nvidia.com/gpu"] // .status.capacity["amd.com/gpu"] // "0"' 2>/dev/null)
                gpu_type_detected=$(oc get node "$node" -o json 2>/dev/null | \
                    jq -r 'if .status.capacity["nvidia.com/gpu"] != null then "NVIDIA" elif .status.capacity["amd.com/gpu"] != null then "AMD" else "Unknown" end' 2>/dev/null)
                echo "  $node: $gpu_capacity x $gpu_type_detected GPU(s)"
            fi
        done <<< "$gpu_nodes"

        echo ""
        echo "GPU Node Resource Usage:"

        # Get instant GPU node usage
        gpu_usage=$(get_gpu_node_usage)
        if [[ "$gpu_usage" != "N/A" ]]; then
            gpu_cpu_instant=$(echo "$gpu_usage" | cut -d',' -f1 | sed 's/%CPU//')
            gpu_mem_instant=$(echo "$gpu_usage" | cut -d',' -f2 | sed 's/%MEM//')
            echo "  Current: CPU ${gpu_cpu_instant}%, Memory ${gpu_mem_instant}%"
        fi

        # Get time-windowed GPU node usage
        if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
            gpu_cpu_windowed=$(get_gpu_node_cpu_usage_windowed)
            gpu_mem_windowed=$(get_gpu_node_memory_usage_windowed)

            if [[ "$gpu_cpu_windowed" != "N/A" ]] && [[ "$gpu_mem_windowed" != "N/A" ]]; then
                echo "  Average (last ${TIME_WINDOW_MINUTES} min): CPU ${gpu_cpu_windowed}%, Memory ${gpu_mem_windowed}%"
            fi
        fi

    elif [[ "$gpu_machines" != "N/A" ]]; then
        echo "GPU Machines Found (by pattern): $gpu_machine_count"
        while IFS= read -r machine; do
            if [[ -n "$machine" ]]; then
                echo "  $machine"
            fi
        done <<< "$gpu_machines"
        log_info "GPU machines exist but nodes may not be ready/running"
    else
        log_info "No GPU nodes or machines found"
    fi

    echo ""

    # Check ML nodes by instance type pattern
    echo "ML Node Check (by instance type):"
    ml_usage=$(get_ml_node_usage)

    # Get windowed ML node usage if time window is enabled
    ml_usage_windowed="N/A"
    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        ml_usage_windowed=$(get_ml_node_usage_windowed)
    fi

    if [[ "$ml_usage" == "N/A" ]] && [[ "$ml_usage_windowed" == "N/A" ]]; then
        log_info "No ML nodes found (patterns: $ML_NODE_PATTERN)"
    else
        # Display instant metrics
        if [[ "$ml_usage" != "N/A" ]]; then
            echo "Current ML Node Average: $ml_usage"
        fi

        # Display windowed metrics
        if [[ "$ml_usage_windowed" != "N/A" ]]; then
            echo "ML Node Average (last ${TIME_WINDOW_MINUTES} min): $ml_usage_windowed"
        fi

        # Determine which metric to use for comparison
        ml_cpu_to_check=""
        if [[ "$ml_usage_windowed" != "N/A" ]]; then
            ml_cpu_to_check=$(echo "$ml_usage_windowed" | sed 's/%CPU//')
        elif [[ "$ml_usage" != "N/A" ]]; then
            ml_cpu_to_check=$(echo "$ml_usage" | cut -d',' -f1 | sed 's/%CPU//')
        fi

        # Check if idle
        if [[ -n "$ml_cpu_to_check" ]]; then
            if awk -v cpu="$ml_cpu_to_check" -v threshold="$CPU_IDLE_THRESHOLD" 'BEGIN {exit !(cpu < threshold)}'; then
                log_success "ML nodes are IDLE"
            else
                log_warning "ML nodes are ACTIVE (expensive resources in use!)"
            fi
        fi
    fi
    echo ""
fi

# === CHECK 5: Operator Age (RHODS/OpenDataHub) ===
((total_criteria++))
operator_result="UNKNOWN"

if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Operator Age & Activity ---"
fi

operator_info=$(get_operator_age)
operator_name=$(echo "$operator_info" | cut -d':' -f1)
operator_age_days=$(echo "$operator_info" | cut -d':' -f2 | sed 's/d//')

if [[ "$VERBOSE" == "true" ]]; then
    echo "Detected Operators: $operator_name"
fi

if [[ "$operator_name" == "N/A" ]]; then
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "No operators found in configured namespaces: $OPERATOR_NAMESPACES"
        # If no operators, check general pod info
        pod_data=$(get_pod_counts)
        IFS=':' read -r total_pods running_pods <<< "$pod_data"
        echo "Total Pods: $total_pods (Running: $running_pods)"
        log_info "Skipping operator age check (criteria not counted)"
    fi
    ((total_criteria--))
    operator_result="N/A"
else
    if [[ "$VERBOSE" == "true" ]]; then
        echo "Oldest Operator Pod Age: ${operator_age_days} days"
    fi

    # Check for recent operator events
    operator_events=$(check_operator_events)

    if [[ "$VERBOSE" == "true" ]]; then
        echo "Recent Operator Events (last 20): $operator_events"

        # Display some operator pods from configured namespaces
        echo ""
        echo "Sample Operator Pods:"

        IFS=',' read -ra NAMESPACES <<< "$OPERATOR_NAMESPACES"
        for ns in "${NAMESPACES[@]}"; do
            ns=$(echo "$ns" | xargs)
            if timeout 5 oc get namespace "$ns" &>/dev/null; then
                timeout 10 oc get pods -n "$ns" --no-headers 2>/dev/null | \
                    grep -E "controller-manager|operator|dashboard" | \
                    head -3 | \
                    awk -v ns_name="$ns" '{printf "  [%s] %-50s  Age: %s\n", ns_name, $1, $5}'
            fi
        done
        echo ""
    fi

    # Determine if operators indicate idle state
    if [[ $operator_age_days -ge $OPERATOR_IDLE_AGE_DAYS ]] && [[ $operator_events -lt 5 ]]; then
        if [[ "$VERBOSE" == "true" ]]; then
            log_success "Operators are IDLE (age: ${operator_age_days}d, low activity)"
        fi
        operator_result="IDLE"
        ((idle_criteria_met++))
    else
        if [[ "$VERBOSE" == "true" ]]; then
            log_warning "Operators are ACTIVE (age: ${operator_age_days}d, events: $operator_events)"
        fi
        operator_result="ACTIVE"
    fi
fi

if [[ "$VERBOSE" == "false" ]]; then
    echo "Operators: $operator_result"
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo ""
fi

# === INFORMATIONAL: Recent Pod Activity (not counted in criteria) ===
if [[ "$VERBOSE" == "true" ]]; then
    echo "--- Recent Activity (last ${EVENT_TIME_MINUTES} minutes) ---"
    recent_events=$(get_recent_pod_activity)

    echo "Pod-related events: $recent_events"
    timeout 10 oc get events -A --sort-by='.lastTimestamp' 2>/dev/null | tail -5 || echo "Could not retrieve events"

    if [[ $recent_events -eq 0 ]]; then
        log_success "No recent pod activity"
    else
        log_warning "Recent pod activity detected - it does not imply the cluster is being actively used"
    fi
    echo ""
fi

# === COLLECT ALL RESULTS FOR EXPORT ===
# Store all results in variables for export
CLUSTER_NAME=$(oc whoami --show-server 2>/dev/null || echo "Unknown")
TIMESTAMP=$(date -Iseconds)
TIMESTAMP_HUMAN=$(date)

# Collect GPU information for export
GPU_INFO=$(get_gpu_info)
GPU_NODES_LIST=$(echo "$GPU_INFO" | cut -d':' -f1)
GPU_NODE_COUNT=$(echo "$GPU_INFO" | cut -d':' -f2)
GPU_MACHINES_LIST=$(echo "$GPU_INFO" | cut -d':' -f3)
GPU_MACHINE_COUNT=$(echo "$GPU_INFO" | cut -d':' -f4)

# Determine if cluster has GPU nodes
if [[ "$GPU_NODES_LIST" != "N/A" ]]; then
    HAS_GPU_NODES="true"
    GPU_FLAVORS=$(get_gpu_flavors)
    GPU_NODE_AGE=$(get_gpu_node_age)

    # Get GPU node usage if available
    GPU_NODE_USAGE=$(get_gpu_node_usage)
    if [[ "$GPU_NODE_USAGE" != "N/A" ]]; then
        GPU_CPU_CURRENT=$(echo "$GPU_NODE_USAGE" | cut -d',' -f1 | sed 's/%CPU//')
        GPU_MEM_CURRENT=$(echo "$GPU_NODE_USAGE" | cut -d',' -f2 | sed 's/%MEM//')
    else
        GPU_CPU_CURRENT="N/A"
        GPU_MEM_CURRENT="N/A"
    fi

    # Get windowed GPU usage if time window is enabled
    if [[ $TIME_WINDOW_MINUTES -gt 0 ]]; then
        GPU_CPU_WINDOWED=$(get_gpu_node_cpu_usage_windowed)
        GPU_MEM_WINDOWED=$(get_gpu_node_memory_usage_windowed)
    else
        GPU_CPU_WINDOWED="N/A"
        GPU_MEM_WINDOWED="N/A"
    fi
else
    HAS_GPU_NODES="false"
    GPU_FLAVORS="N/A"
    GPU_NODE_AGE="N/A"
    GPU_CPU_CURRENT="N/A"
    GPU_MEM_CURRENT="N/A"
    GPU_CPU_WINDOWED="N/A"
    GPU_MEM_WINDOWED="N/A"
fi

# === FINAL DETERMINATION ===
# Determine if cluster is idle (need at least 75% criteria met)
idle_threshold=$(awk -v total="$total_criteria" 'BEGIN {print int(total * 0.80)}')

if [[ $idle_criteria_met -ge $idle_threshold ]]; then
    FINAL_STATUS="IDLE"
    EXIT_CODE=1  # Exit 1 for IDLE (warning - wasting resources)
else
    FINAL_STATUS="ACTIVE"
    EXIT_CODE=0  # Exit 0 for ACTIVE (success - resources being used)
fi

if [[ "$VERBOSE" == "true" ]]; then
    echo "========================================"
    echo "         IDLE DETECTION SUMMARY"
    echo "========================================"
    echo ""
    echo "Idle Criteria Met: $idle_criteria_met / $total_criteria"
    echo ""

    if [[ "$FINAL_STATUS" == "IDLE" ]]; then
        echo -e "${RED}╔═══════════════════════════════╗${NC}"
        echo -e "${RED}║   CLUSTER STATUS: IDLE ✗      ║${NC}"
        echo -e "${RED}╚═══════════════════════════════╝${NC}"
        echo ""
        log_warning "Cluster is considered IDLE - resources may be wasted!"

        if [[ "$ml_usage" != "N/A" ]] || [[ "$HAS_GPU_NODES" == "true" ]]; then
            echo ""
            log_warning "Expensive GPU/ML nodes are idle - incurring unnecessary costs!"
        fi
    else
        echo -e "${GREEN}╔═══════════════════════════════╗${NC}"
        echo -e "${GREEN}║   CLUSTER STATUS: ACTIVE ✓    ║${NC}"
        echo -e "${GREEN}╚═══════════════════════════════╝${NC}"
        echo ""
        log_success "Cluster is considered ACTIVE - resources are being utilized"
    fi
else
    # Quiet mode - just print final status
    echo "STATUS: $FINAL_STATUS"
fi

# === EXPORT RESULTS ===
export_csv() {
    local csv_file="$1"

    # Prepare values (remove N/A and handle empty values)
    local cpu_val="${cpu_to_check:-N/A}"
    local mem_val="${mem_to_check:-N/A}"
    local api_val="${api_rate:-N/A}"
    local operator_age="${operator_age_days:-N/A}"

    # Write CSV header if file doesn't exist
    if [[ ! -f "$csv_file" ]]; then
        echo "timestamp,cluster,status,cpu_result,cpu_value,memory_result,memory_value,api_server_result,api_server_value,operators_result,operator_age_days,criteria_met,total_criteria,time_window_minutes,has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,gpu_cpu_current,gpu_mem_current,gpu_cpu_windowed,gpu_mem_windowed" > "$csv_file"
    fi

    # Append data
    echo "${TIMESTAMP},${CLUSTER_NAME},${FINAL_STATUS},${cpu_result},${cpu_val},${mem_result},${mem_val},${api_result},${api_val},${operator_result},${operator_age},${idle_criteria_met},${total_criteria},${TIME_WINDOW_MINUTES},${HAS_GPU_NODES},${GPU_NODE_COUNT},${GPU_FLAVORS},${GPU_NODE_AGE},${GPU_CPU_CURRENT},${GPU_MEM_CURRENT},${GPU_CPU_WINDOWED},${GPU_MEM_WINDOWED}" >> "$csv_file"
}

export_json() {
    local json_file="$1"

    # Prepare values
    local cpu_val="${cpu_to_check:-null}"
    local mem_val="${mem_to_check:-null}"
    local api_val="${api_rate:-null}"
    local operator_age="${operator_age_days:-null}"

    # Quote string values, leave null as-is
    [[ "$cpu_val" != "null" ]] && cpu_val="\"$cpu_val\""
    [[ "$mem_val" != "null" ]] && mem_val="\"$mem_val\""
    [[ "$api_val" != "null" ]] && api_val="\"$api_val\""
    [[ "$operator_age" != "null" ]] && operator_age="$operator_age"

    # Prepare GPU values
    local gpu_cpu_current_val="${GPU_CPU_CURRENT}"
    local gpu_mem_current_val="${GPU_MEM_CURRENT}"
    local gpu_cpu_windowed_val="${GPU_CPU_WINDOWED}"
    local gpu_mem_windowed_val="${GPU_MEM_WINDOWED}"
    local gpu_flavors_val="${GPU_FLAVORS}"
    local gpu_age_val="${GPU_NODE_AGE}"

    # Convert N/A to null
    [[ "$gpu_cpu_current_val" == "N/A" ]] && gpu_cpu_current_val="null" || gpu_cpu_current_val="\"$gpu_cpu_current_val\""
    [[ "$gpu_mem_current_val" == "N/A" ]] && gpu_mem_current_val="null" || gpu_mem_current_val="\"$gpu_mem_current_val\""
    [[ "$gpu_cpu_windowed_val" == "N/A" ]] && gpu_cpu_windowed_val="null" || gpu_cpu_windowed_val="\"$gpu_cpu_windowed_val\""
    [[ "$gpu_mem_windowed_val" == "N/A" ]] && gpu_mem_windowed_val="null" || gpu_mem_windowed_val="\"$gpu_mem_windowed_val\""
    [[ "$gpu_flavors_val" == "N/A" ]] && gpu_flavors_val="null" || gpu_flavors_val="\"$gpu_flavors_val\""
    [[ "$gpu_age_val" == "N/A" ]] && gpu_age_val="null" || gpu_age_val="\"$gpu_age_val\""

    # Create JSON
    cat > "$json_file" << EOF
{
  "timestamp": "${TIMESTAMP}",
  "timestamp_human": "${TIMESTAMP_HUMAN}",
  "cluster": "${CLUSTER_NAME}",
  "status": "${FINAL_STATUS}",
  "exit_code": ${EXIT_CODE},
  "configuration": {
    "time_window_minutes": ${TIME_WINDOW_MINUTES},
    "cpu_threshold": ${CPU_IDLE_THRESHOLD},
    "memory_threshold": ${MEMORY_IDLE_THRESHOLD},
    "api_threshold": ${APISERVER_IDLE_THRESHOLD},
    "operator_age_threshold_days": ${OPERATOR_IDLE_AGE_DAYS}
  },
  "criteria": {
    "total": ${total_criteria},
    "met": ${idle_criteria_met},
    "threshold": ${idle_threshold},
    "cpu": {
      "result": "${cpu_result}",
      "value": ${cpu_val}
    },
    "memory": {
      "result": "${mem_result}",
      "value": ${mem_val}
    },
    "api_server": {
      "result": "${api_result}",
      "value": ${api_val}
    },
    "operators": {
      "result": "${operator_result}",
      "age_days": ${operator_age}
    }
  },
  "gpu": {
    "has_gpu_nodes": ${HAS_GPU_NODES},
    "node_count": ${GPU_NODE_COUNT},
    "flavors": ${gpu_flavors_val},
    "node_age": ${gpu_age_val},
    "usage": {
      "cpu_current": ${gpu_cpu_current_val},
      "memory_current": ${gpu_mem_current_val},
      "cpu_windowed": ${gpu_cpu_windowed_val},
      "memory_windowed": ${gpu_mem_windowed_val}
    }
  }
}
EOF
}

# Export if requested
if [[ -n "$EXPORT_CSV" ]]; then
    export_csv "$EXPORT_CSV"
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "Results exported to CSV: $EXPORT_CSV"
    fi
fi

if [[ -n "$EXPORT_JSON" ]]; then
    export_json "$EXPORT_JSON"
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "Results exported to JSON: $EXPORT_JSON"
    fi
fi

exit $EXIT_CODE
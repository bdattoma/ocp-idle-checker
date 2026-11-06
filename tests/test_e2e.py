"""End-to-end tests: the full run() orchestration against a local
Prometheus HTTP server and a stubbed oc CLI, plus subprocess smoke tests
for the bash shim that Jenkins and cluster-monitor invoke.

oc is stubbed at the Oc.run seam with strict argv matching: every command
the module issues must be declared by the test, and an undeclared command
fails the test loudly.  That keeps the oc command construction under test
without spawning oc or keeping a fake binary on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import ocp_idle_check as oic
from helpers import (
    api_avg_query,
    make_dcgm_series,
    make_event,
    make_range_series,
    node_cpu_window_query,
    node_mem_window_query,
    pod_row,
    prom_instant_json,
    prom_instant_series_json,
    prom_range_json,
    rs_row,
    ts_before,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "ocp-idle-check.sh"
# Resolved at import time: one test narrows PATH to a directory without bash.
BASH = shutil.which("bash") or "/bin/bash"
SERVER = "https://api.test-cluster.example.com:6443"
WINDOW = 10080

# Timestamps are taken at import time: the criteria round ages to two
# decimals, so the sub-second gap to run() never moves a rounded age.
E2E_NOW = datetime.now(UTC)
OPERATOR_PODS_OLD = (
    pod_row(
        "opendatahub",
        "odh-operator-controller-manager-abc",
        "ReplicaSet",
        "odh-operator-5d4c3b",
        ts_before(E2E_NOW, days=12),
    )
    + "\n"
)
OPERATOR_REPLICASETS_OLD = (
    rs_row("opendatahub", "odh-operator-5d4c3b", ts_before(E2E_NOW, days=12)) + "\n"
)
# Young variant: a rollout two days ago left a new owner reference behind.
OPERATOR_PODS_YOUNG = (
    pod_row(
        "opendatahub",
        "odh-operator-controller-manager-abc",
        "ReplicaSet",
        "odh-operator-9f8e7d",
        ts_before(E2E_NOW, days=2),
    )
    + "\n"
)
OPERATOR_REPLICASETS_YOUNG = (
    rs_row("opendatahub", "odh-operator-9f8e7d", ts_before(E2E_NOW, days=2)) + "\n"
)
QUIET_EVENTS = json.dumps(
    {
        "items": [
            make_event(
                "opendatahub",
                now=E2E_NOW,
                hours=1,
                reason="Pulled",
                kind="Pod",
                involved="odh-xyz",
                message="Pull image quay.io/example",
            )
        ]
    }
)
CREATE_TOKEN_ARGS = (
    "create",
    "token",
    "prometheus-k8s",
    "-n",
    "openshift-monitoring",
    "--duration=10m",
)


def install_fake_oc(
    monkeypatch, responses: dict[tuple[str, ...], str | None]
) -> list[tuple[str, ...]]:
    """Replace Oc.run with a strict argv-keyed stub; returns the call log.

    A value of None means the command failed (non-zero exit), matching how
    Oc.run reports failures.
    """
    calls: list[tuple[str, ...]] = []

    def fake_run(self, args, timeout=oic.OC_TIMEOUT):
        key = tuple(args)
        calls.append(key)
        if key not in responses:
            raise AssertionError(f"unexpected oc invocation: {' '.join(key)}")
        return responses[key]

    monkeypatch.setattr(oic.Oc, "run", fake_run)
    # The oc presence probe must pass regardless of the host PATH.
    monkeypatch.setattr(oic.shutil, "which", lambda name: "/fakebin/oc" if name == "oc" else None)
    return calls


def node_item(
    name: str,
    instance_type: str = "m5.xlarge",
    gpus: str | None = None,
    roles: list[str] | None = None,
) -> dict:
    capacity = {"nvidia.com/gpu": gpus} if gpus is not None else {}
    labels = {"node.kubernetes.io/instance-type": instance_type}
    labels.update({role: "" for role in roles or []})
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"capacity": capacity},
    }


def base_oc_responses(nodes: list[dict]) -> dict[tuple[str, ...], str | None]:
    names = [n["metadata"]["name"] for n in nodes]
    top_lines = "\n".join(f"{name} 250m 2% 3212Mi 5%" for name in names) + "\n"
    return {
        ("whoami",): "test-user",
        ("whoami", "--show-server"): SERVER,
        ("whoami", "-t"): "fake-token",
        ("get", "nodes", "-o", "json"): json.dumps({"items": nodes}),
        ("adm", "top", "nodes", "--no-headers"): top_lines,
        ("get", "pods", "-A", "-o", oic.PODS_ALL_JSONPATH): OPERATOR_PODS_OLD,
        ("get", "rs", "-A", "-o", oic.REPLICASETS_ALL_JSONPATH): OPERATOR_REPLICASETS_OLD,
        ("get", "events", "-A", "-o", "json"): QUIET_EVENTS,
    }


def quiet_cpu(responses: dict, nodes: list[str]) -> None:
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json(
        [make_range_series(n, ["2"] * 8) for n in nodes]
    )


def quiet_memory_and_api(responses: dict, nodes: list[str]) -> None:
    # Flat per-node windows: the movement rule sees no rise, and the level
    # (10%) is irrelevant to it - only the legacy rule ever read levels.
    responses[("/api/v1/query_range", oic.MEMORY_RANGE_QUERY)] = prom_range_json(
        [make_range_series(n, ["10"] * 8) for n in nodes]
    )
    responses[("/api/v1/query", api_avg_query(WINDOW))] = prom_instant_json("5")
    responses[("/api/v1/query_range", oic.API_RANGE_QUERY)] = prom_range_json(
        [{"metric": {}, "values": [[i * 900, "5"] for i in range(8)]}]
    )


def young_operators(oc_responses: dict) -> None:
    """Swap the default old operators for a rollout two days ago (ACTIVE)."""
    oc_responses[("get", "pods", "-A", "-o", oic.PODS_ALL_JSONPATH)] = OPERATOR_PODS_YOUNG
    oc_responses[("get", "rs", "-A", "-o", oic.REPLICASETS_ALL_JSONPATH)] = (
        OPERATOR_REPLICASETS_YOUNG
    )


def gpu_windowed_usage(responses: dict, nodes: list[str]) -> None:
    """The batched windowed CPU/memory queries the gpu report issues per node."""
    for node in nodes:
        responses[("/api/v1/query", node_cpu_window_query([node], WINDOW))] = (
            prom_instant_series_json({f"{node}:9100": "7"})
        )
        responses[("/api/v1/query", node_mem_window_query([node], WINDOW))] = (
            prom_instant_series_json({f"{node}:9100": "11"})
        )


# === FULL RUN, IN PROCESS ===================================================


def test_idle_cluster_verbose(monkeypatch, prom_server, tmp_path, capsys):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    # Verbose mode also pulls the machine list; the cluster-wide event list
    # is shared with the operators criterion through the Oc cache.
    oc_responses[("get", "machines", "-n", "openshift-machine-api", "-o", "json")] = '{"items": []}'
    calls = install_fake_oc(monkeypatch, oc_responses)
    quiet_cpu(responses, ["node-1", "node-2", "node-3"])
    quiet_memory_and_api(responses, ["node-1", "node-2", "node-3"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 1

    # The quiet result lines are stdout; everything else goes to stderr.
    assert capsys.readouterr().out.splitlines() == [
        "CPU: IDLE",
        "Memory: IDLE",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: IDLE",
        "STATUS: IDLE (closest to active: Operators at 58% of threshold)",
    ]

    report = json.loads(json_path.read_text())
    assert report["status"] == "IDLE"
    assert report["exit_code"] == 1
    assert report["cluster"] == SERVER
    assert list(report) == [
        "timestamp",
        "timestamp_human",
        "cluster",
        "status",
        "exit_code",
        "configuration",
        "criteria",
        "gpu",
        "oversized",
    ]
    assert list(report["configuration"]) == [
        "time_window_minutes",
        "cpu_threshold",
        "memory_threshold",
        "api_threshold",
        "operator_age_threshold_days",
        "operator_event_window_hours",
        "operator_exclude_prefixes",
        "cpu_peak_threshold",
        "cpu_variance_ratio",
        "gpu_peak_threshold",
        "gpu_variance_ratio",
        "api_variance_ratio",
        "memory_variance_points",
        "memory_variance_windows",
    ]
    criteria = report["criteria"]
    assert list(criteria) == [
        "total",
        "met",
        "threshold",
        "cpu",
        "memory",
        "api_server",
        "gpu",
        "operators",
        "driver",
    ]
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (4, 4, 3)
    assert criteria["cpu"]["result"] == "IDLE"
    assert criteria["cpu"]["threshold_ratio"] == 0.24
    assert criteria["operators"]["age_days"] == 12.0
    assert criteria["operators"]["events"] == 0
    assert criteria["gpu"]["result"] == "N/A"
    assert criteria["driver"] == {
        "criterion": "operators",
        "label": "Operators",
        "threshold_ratio": 0.58,
        "quorum_driven": False,
    }
    # The summary keeps only the headline fields, so renderers printing
    # `criteria` verbatim (the Jenkins report's Metrics column) stay compact.
    assert set(criteria["cpu"]) == {"result", "value", "threshold_ratio"}
    assert set(criteria["operators"]) == {"result", "age_days", "events", "threshold_ratio"}
    # The token came from `oc whoami -t`; no service-account token was created.
    assert CREATE_TOKEN_ARGS not in calls
    # An IDLE cluster is not oversized, whatever its node levels.
    assert report["oversized"] == {"result": False, "gpu_peak": None, "nodes": []}


def test_active_cluster_quiet(monkeypatch, prom_server, tmp_path, capsys):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    young_operators(oc_responses)
    install_fake_oc(monkeypatch, oc_responses)
    # One node spends a single 15-minute window at 50% CPU.
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json(
        [
            make_range_series("node-1", ["5"] * 8),
            make_range_series("node-2", ["5", "5", "50"] + ["5"] * 5),
            make_range_series("node-3", ["5"] * 8),
        ]
    )
    quiet_memory_and_api(responses, ["node-1", "node-2", "node-3"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "CPU: ACTIVE",
        "Memory: IDLE",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: ACTIVE",
        "Oversized: true (2 level-idle nodes)",
        "STATUS: ACTIVE (Operators at 350% of threshold)",
    ]

    report = json.loads(json_path.read_text())
    criteria = report["criteria"]
    assert (criteria["total"], criteria["met"]) == (4, 2)
    assert criteria["cpu"]["result"] == "ACTIVE"
    # The burst cleared its firing level: the ratio sits past 1.0.
    assert criteria["cpu"]["threshold_ratio"] > 1
    assert criteria["memory"]["result"] == "IDLE"
    assert criteria["api_server"]["result"] == "IDLE"
    assert criteria["operators"]["result"] == "ACTIVE"
    # node-2's 50% window made the cluster active, but node-1 and node-3
    # never crossed the peak threshold - they are the right-sizing signal.
    assert report["oversized"] == {
        "result": True,
        "gpu_peak": None,
        "nodes": [
            {"node": "node-1", "cpu_peak": 5.0, "gpu_node": False},
            {"node": "node-3", "cpu_peak": 5.0, "gpu_node": False},
        ],
    }


def test_oversized_names_level_idle_workers_on_a_variance_active_cluster(
    monkeypatch, prom_server, tmp_path, capsys
):
    # The cluster is ACTIVE purely on CPU variance - worker-1's low-level
    # burst (1% -> 15%) never crosses the 30% peak threshold, so without the
    # variance check every node would have been judged inactive.  The quiet
    # master is expected, not right-sizing evidence, so only the workers are
    # named.
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [
            node_item("master-0", roles=["node-role.kubernetes.io/master"]),
            node_item("worker-1"),
            node_item("worker-2"),
        ]
    )
    young_operators(oc_responses)
    # Verbose mode also pulls the machine list for the ML node display.
    oc_responses[("get", "machines", "-n", "openshift-machine-api", "-o", "json")] = '{"items": []}'
    install_fake_oc(monkeypatch, oc_responses)
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json(
        [
            make_range_series("master-0", ["10"] * 8),
            make_range_series("worker-1", ["1", "1", "15"] + ["1"] * 5),
            make_range_series("worker-2", ["2"] * 8),
        ]
    )
    quiet_memory_and_api(responses, ["master-0", "worker-1", "worker-2"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "CPU: ACTIVE",
        "Memory: IDLE",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: ACTIVE",
        "Oversized: true (2 level-idle nodes)",
        "STATUS: ACTIVE (Operators at 350% of threshold)",
    ]
    assert "Level-idle node: worker-1 (CPU peak 15.0%)" in captured.err
    assert "Level-idle node: worker-2 (CPU peak 2.0%)" in captured.err
    assert "Level-idle node: master-0" not in captured.err

    report = json.loads(json_path.read_text())
    assert report["oversized"] == {
        "result": True,
        "gpu_peak": None,
        "nodes": [
            {"node": "worker-1", "cpu_peak": 15.0, "gpu_node": False},
            {"node": "worker-2", "cpu_peak": 2.0, "gpu_node": False},
        ],
    }


def test_operator_listing_failure_reports_unknown(monkeypatch, prom_server, tmp_path, capsys):
    # A failed cluster-wide listing is UNKNOWN and uncounted, not an N/A
    # "no controller pods": the quiet line and the criteria totals must
    # distinguish a failed listing from real emptiness.
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    oc_responses[("get", "pods", "-A", "-o", oic.PODS_ALL_JSONPATH)] = None
    install_fake_oc(monkeypatch, oc_responses)
    quiet_cpu(responses, ["node-1", "node-2", "node-3"])
    quiet_memory_and_api(responses, ["node-1", "node-2", "node-3"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == [
        "CPU: IDLE",
        "Memory: IDLE",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: UNKNOWN",
        "STATUS: IDLE (closest to active: API Server at 31% of threshold)",
    ]

    report = json.loads(json_path.read_text())
    criteria = report["criteria"]
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (3, 3, 2)
    assert criteria["operators"] == {
        "result": "UNKNOWN",
        "age_days": 0,
        "threshold_ratio": None,
    }


def test_node_listing_failure_reports_gpu_unknown(monkeypatch, prom_server, tmp_path, capsys):
    # A failed `oc get nodes` is not an empty cluster: the GPU criterion
    # reads UNKNOWN (counted) instead of N/A "no GPU nodes", and the CPU
    # variance rule runs unfiltered.  All four real criteria are IDLE, so
    # 4 of 5 still meets the 80% quorum.
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    oc_responses[("get", "nodes", "-o", "json")] = None
    install_fake_oc(monkeypatch, oc_responses)
    quiet_cpu(responses, ["node-1", "node-2", "node-3"])
    quiet_memory_and_api(responses, ["node-1", "node-2", "node-3"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "CPU: IDLE",
        "Memory: IDLE",
        "API Server: IDLE",
        "GPU: UNKNOWN",
        "Operators: IDLE",
        "STATUS: IDLE (closest to active: Operators at 58% of threshold)",
    ]

    report = json.loads(json_path.read_text())
    criteria = report["criteria"]
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (5, 4, 4)
    # Unfiltered: the CPU series kept evaluating despite the missing listing.
    assert criteria["cpu"]["result"] == "IDLE"
    assert criteria["gpu"] == {
        "result": "UNKNOWN",
        "value": None,
        "threshold_ratio": None,
    }
    # The report's GPU section records the unknown presence rather than
    # claiming the cluster has no GPU nodes.
    assert report["gpu"]["has_gpu_nodes"] is None
    assert report["gpu"]["node_count"] is None


def test_too_many_failed_reads_error(monkeypatch, prom_server, tmp_path, capsys):
    # The gate's consequence: too many counted UNKNOWNs from failed reads
    # error out (exit 2) rather than issue a verdict built on too little
    # data.  The CPU and memory range queries fail for real here (the stub
    # server 404s them, which the client reads as a failed read, not empty
    # data), so the gate fires at its shipped threshold of two.
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    install_fake_oc(monkeypatch, oc_responses)
    # Only the API criterion gets windowed data; CPU and memory fail.
    responses[("/api/v1/query", api_avg_query(WINDOW))] = prom_instant_json("5")
    responses[("/api/v1/query_range", oic.API_RANGE_QUERY)] = prom_range_json(
        [{"metric": {}, "values": [[i * 900, "5"] for i in range(8)]}]
    )

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 2
    assert capsys.readouterr().out.splitlines() == [
        "CPU: UNKNOWN",
        "Memory: UNKNOWN",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: IDLE",
        "STATUS: ERROR (too many UNKNOWN criteria: CPU, Memory)",
    ]

    report = json.loads(json_path.read_text())
    assert report["status"] == "ERROR"
    assert report["exit_code"] == 2
    assert report["error"] == "too many UNKNOWN criteria: CPU, Memory"
    # No verdict means no boundary criterion and no right-sizing claim.
    assert report["criteria"]["driver"] is None
    assert report["oversized"] == {"result": False, "gpu_peak": None, "nodes": []}


def test_gpu_cluster_adds_fifth_criterion(monkeypatch, prom_server, tmp_path):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    nodes = [node_item("node-1"), node_item("gpu-node-1", instance_type="g5.xlarge", gpus="4")]
    oc_responses = base_oc_responses(nodes)
    young_operators(oc_responses)
    # Per-node instant usage comes from the cluster-wide snapshot: the GPU
    # node has its own line there, and no per-node `adm top node` may run.
    oc_responses[("adm", "top", "nodes", "--no-headers")] = (
        "node-1 250m 2% 3212Mi 5%\ngpu-node-1 500m 3% 8000Mi 6%\n"
    )
    calls = install_fake_oc(monkeypatch, oc_responses)

    quiet_cpu(responses, ["node-1", "gpu-node-1"])
    quiet_memory_and_api(responses, ["node-1", "gpu-node-1"])
    # One 15-minute window at 12% GPU utilization: over the 5% variance
    # floor on a zero baseline (the burst branch fires well under the 40%
    # peak threshold), and enough to touch the GPU.
    responses[("/api/v1/query_range", oic.GPU_RANGE_QUERY)] = prom_range_json(
        [make_dcgm_series(["0", "0", "0", "12"])]
    )
    # Batched windowed usage queries from the gpu report (the ML node
    # display is verbose-only, so a quiet run never issues its query).
    gpu_windowed_usage(responses, ["gpu-node-1"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0

    assert ("adm", "top", "node", "gpu-node-1", "--no-headers") not in calls
    report = json.loads(json_path.read_text())
    criteria = report["criteria"]
    # Five criteria now; cpu, memory, and api vote IDLE - 3 of 5 is below 80%.
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (5, 3, 4)
    assert criteria["gpu"]["result"] == "ACTIVE"
    # The 12% window cleared its firing level - the 5% floor on a zero
    # baseline - so the ratio sits past 1.0.
    assert criteria["gpu"]["threshold_ratio"] > 1
    gpu = report["gpu"]
    assert gpu["has_gpu_nodes"] is True
    assert gpu["node_count"] == 1
    assert gpu["flavors"] == "NVIDIA(g5.xlarge)"
    assert gpu["usage"] == {
        "cpu_current": "3.00",
        "memory_current": "6.00",
        "cpu_windowed": "7.00",
        "memory_windowed": "11.00",
    }
    # Both workers stayed under the CPU peak threshold, but the GPU node is
    # not named: its GPU series met the variance floor (12% over 5%), and a
    # touched GPU reads as in use - VRAM capacity is often why it was
    # picked, and utilization is a weak signal for that.
    assert report["oversized"] == {
        "result": True,
        "gpu_peak": None,
        "nodes": [{"node": "node-1", "cpu_peak": 2.0, "gpu_node": False}],
    }


def test_oversized_gpu_node_carries_the_cluster_wide_gpu_peak(
    monkeypatch, prom_server, tmp_path, capsys
):
    # The one run where a GPU node is actually named: node-1's CPU burst
    # keeps the cluster ACTIVE, the GPU node's CPU never crosses the peak
    # threshold, and no DCGM series ever meets the 5% variance floor - the
    # GPU was never touched, so the node is acquirable.  gpu_peak then
    # records the hottest series cluster-wide, the acquittal's receipt, not
    # the node's own peak (series cannot be attributed to node names).
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    nodes = [node_item("node-1"), node_item("gpu-node-1", instance_type="g5.xlarge", gpus="4")]
    oc_responses = base_oc_responses(nodes)
    young_operators(oc_responses)
    install_fake_oc(monkeypatch, oc_responses)

    # One 15-minute window at 50% CPU on node-1: the cluster is ACTIVE, but
    # node-1 crossed the peak threshold so only the GPU node can be named.
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json(
        [
            make_range_series("node-1", ["5", "5", "50"] + ["5"] * 5),
            make_range_series("gpu-node-1", ["2"] * 8),
        ]
    )
    quiet_memory_and_api(responses, ["node-1", "gpu-node-1"])
    # Two exporters on the cluster, the hotter peaking at 4% - under the
    # 5% variance floor (DCGM's idle noise tail), so the GPUs were never
    # touched and every GPU node is acquirable.
    responses[("/api/v1/query_range", oic.GPU_RANGE_QUERY)] = prom_range_json(
        [
            make_dcgm_series(["4"] * 8, instance="10.130.2.24:9400"),
            make_dcgm_series(["0"] * 8, instance="10.130.2.25:9400", gpu="1"),
        ]
    )
    # Batched windowed usage queries from the gpu report (the ML node
    # display is verbose-only, so a quiet run never issues its query).
    gpu_windowed_usage(responses, ["gpu-node-1"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0
    assert "Oversized: true (1 level-idle node)" in capsys.readouterr().out

    report = json.loads(json_path.read_text())
    assert report["oversized"] == {
        "result": True,
        "gpu_peak": 4.0,
        "nodes": [{"node": "gpu-node-1", "cpu_peak": 2.0, "gpu_node": True}],
    }


def test_verbose_gpu_cluster_reports_ml_nodes(monkeypatch, prom_server, tmp_path, capsys):
    """Verbose mode adds the ML node display: per-node instant numbers from
    the cluster-wide top snapshot and one batched windowed query per metric,
    with no per-node `oc adm top node` spawns."""
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    nodes = [node_item("node-1"), node_item("gpu-node-1", instance_type="g5.xlarge", gpus="4")]
    oc_responses = base_oc_responses(nodes)
    oc_responses[("adm", "top", "nodes", "--no-headers")] = (
        "node-1 250m 2% 3212Mi 5%\ngpu-node-1 500m 3% 8000Mi 6%\n"
    )
    calls = install_fake_oc(monkeypatch, oc_responses)

    quiet_cpu(responses, ["node-1"])
    quiet_memory_and_api(responses, ["node-1", "gpu-node-1"])
    responses[("/api/v1/query_range", oic.GPU_RANGE_QUERY)] = prom_range_json(
        [make_dcgm_series(["0"] * 8)]
    )
    # The gpu report and the ML node display cover the same node, so their
    # batched queries are identical strings served by the same entry.
    gpu_windowed_usage(responses, ["gpu-node-1"])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 1

    stderr = capsys.readouterr().err
    assert "Found 1 ML/GPU instance-type nodes:" in stderr
    assert "gpu-node-1 (g5.xlarge): instant cpu=3.00% mem=6.00%, windowed cpu=7.00%" in stderr
    assert "Expensive GPU/ML nodes are idle - review before cleanup" in stderr
    # Instant per-node numbers come from the cluster-wide snapshot.
    assert ("adm", "top", "node", "gpu-node-1", "--no-headers") not in calls


def test_csv_export_appends_with_header(monkeypatch, prom_server, tmp_path):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    install_fake_oc(monkeypatch, base_oc_responses([node_item("node-1")]))
    quiet_cpu(responses, ["node-1"])
    quiet_memory_and_api(responses, ["node-1"])

    csv_path = tmp_path / "results.csv"
    argv = ["-q", "--csv", str(csv_path), "-w", str(WINDOW)]
    assert oic.run(argv) == 1
    assert oic.run(argv) == 1

    lines = csv_path.read_text().splitlines()
    assert len(lines) == 3
    assert lines[0] == oic.CSV_HEADER
    assert lines[1] == lines[2]
    for row in lines[1:]:
        assert len(row.split(",")) == 24


def test_unknown_votes_depriving_the_quorum_annotate_status(
    monkeypatch, prom_server, tmp_path, capsys
):
    # CPU and memory read UNKNOWN (Prometheus down for their queries, quiet
    # instants) and still count; the operators event listing fails, so it is
    # UNKNOWN and uncounted.  Those UNKNOWNs are declared no-data verdicts,
    # not failed reads, so they do not trip the too-many-UNKNOWNs gate: one
    # IDLE vote of three counted - below the quorum - makes the verdict
    # ACTIVE with nothing fired, and the STATUS annotation says so instead
    # of implying a hot criterion.
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    oc_responses[("get", "events", "-A", "-o", "json")] = None
    install_fake_oc(monkeypatch, oc_responses)
    # Only the API criterion has windowed data; the CPU and memory range
    # queries succeed but come back empty (a fresh install, short retention),
    # which the client reads as no data rather than a failed read.
    responses[("/api/v1/query", api_avg_query(WINDOW))] = prom_instant_json("5")
    responses[("/api/v1/query_range", oic.API_RANGE_QUERY)] = prom_range_json(
        [{"metric": {}, "values": [[i * 900, "5"] for i in range(8)]}]
    )
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json([])
    responses[("/api/v1/query_range", oic.MEMORY_RANGE_QUERY)] = prom_range_json([])

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "CPU: UNKNOWN",
        "Memory: UNKNOWN",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: UNKNOWN",
        "STATUS: ACTIVE (UNKNOWN-driven; API Server at 31% of threshold)",
    ]

    criteria = json.loads(json_path.read_text())["criteria"]
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (3, 1, 2)
    assert criteria["cpu"]["result"] == "UNKNOWN"
    assert criteria["memory"]["result"] == "UNKNOWN"
    assert criteria["operators"]["result"] == "UNKNOWN"
    assert criteria["driver"] == {
        "criterion": "api_server",
        "label": "API Server",
        "threshold_ratio": 0.31,
        "quorum_driven": True,
    }
    # Nothing fired, so the ACTIVE verdict carries no right-sizing claim -
    # and no Oversized line printed above.
    report = json.loads(json_path.read_text())
    assert report["oversized"] == {"result": False, "gpu_peak": None, "nodes": []}


# === BASH SHIM SMOKE TESTS ==================================================


def run_shim(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, str(SHIM), *args], capture_output=True, text=True, timeout=timeout)


def test_shim_help_exits_0():
    proc = run_shim(["-h"])
    assert proc.returncode == 0
    assert "ocp-idle-check.sh" in proc.stdout


def test_shim_unknown_flag_exits_2():
    proc = run_shim(["--nope"])
    assert proc.returncode == 2


def test_shim_missing_oc_exits_2(monkeypatch):
    # A PATH with python3 but no oc: the shim must fail cleanly with 2.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    proc = run_shim(["-q"])
    assert proc.returncode == 2
    assert "oc command not found" in proc.stderr


def test_shim_without_python3_exits_2(monkeypatch, tmp_path):
    # No python3 anywhere on PATH: exit 2, not exec's 127.  The guards run
    # before the SCRIPT_DIR resolution on purpose - dirname is an external
    # binary and would also be missing from a stripped PATH.
    monkeypatch.setenv("PATH", str(tmp_path))
    proc = run_shim(["-q"])
    assert proc.returncode == 2
    assert "python3 not found" in proc.stderr


def test_shim_old_python3_exits_2(monkeypatch, tmp_path):
    # A too-old python3 must fail with 2; a Python traceback would exit 1,
    # which callers would misread as IDLE.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/bash\necho 'Python 3.6.9'\nexit 1\n")
    fake_python.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    proc = run_shim(["-q"])
    assert proc.returncode == 2
    assert "python3 >= 3.12 required" in proc.stderr
    assert "Python 3.6.9" in proc.stderr


def test_shim_prefers_python312_over_old_bare_python3(monkeypatch, tmp_path):
    # The Jenkins agent image ships a bare python3 of 3.9 alongside a full
    # python3.12; the shim must run the module with the versioned name.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    old = fake_bin / "python3"
    old.write_text("#!/bin/bash\necho 'Python 3.9.25'\nexit 1\n")
    old.chmod(0o755)
    fake_312 = fake_bin / "python3.12"
    fake_312.write_text(
        "#!/bin/bash\n"
        "# The version probe passes; any other invocation reports its argv.\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'echo "ran $*"\n'
    )
    fake_312.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    proc = run_shim(["-q"])
    assert proc.returncode == 0
    # dirname is absent from the stripped PATH, so the shim falls back to the
    # working directory for SCRIPT_DIR - the same fallback the oc test above
    # relies on.  The shim runs the package via -m, not a launcher file.
    assert proc.stdout.strip() == "ran -m ocp_idle_check -q"

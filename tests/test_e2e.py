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
    memory_window_query,
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


def node_item(name: str, instance_type: str = "m5.xlarge", gpus: str | None = None) -> dict:
    capacity = {"nvidia.com/gpu": gpus} if gpus is not None else {}
    return {
        "metadata": {
            "name": name,
            "labels": {"node.kubernetes.io/instance-type": instance_type},
        },
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


def quiet_memory_and_api(responses: dict) -> None:
    responses[("/api/v1/query", memory_window_query(WINDOW))] = prom_instant_json("10")
    responses[("/api/v1/query", api_avg_query(WINDOW))] = prom_instant_json("5")
    responses[("/api/v1/query_range", oic.API_RANGE_QUERY)] = prom_range_json(
        [{"metric": {}, "values": [[i * 900, "5"] for i in range(8)]}]
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
    quiet_memory_and_api(responses)

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
    assert criteria["cpu"]["threshold_ratio"] == 0.13
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


def test_active_cluster_quiet(monkeypatch, prom_server, tmp_path, capsys):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    oc_responses[("get", "pods", "-A", "-o", oic.PODS_ALL_JSONPATH)] = OPERATOR_PODS_YOUNG
    oc_responses[("get", "rs", "-A", "-o", oic.REPLICASETS_ALL_JSONPATH)] = (
        OPERATOR_REPLICASETS_YOUNG
    )
    install_fake_oc(monkeypatch, oc_responses)
    # One node spends a single 15-minute window at 50% CPU.
    responses[("/api/v1/query_range", oic.CPU_RANGE_QUERY)] = prom_range_json(
        [
            make_range_series("node-1", ["5"] * 8),
            make_range_series("node-2", ["5", "5", "50"] + ["5"] * 5),
            make_range_series("node-3", ["5"] * 8),
        ]
    )
    quiet_memory_and_api(responses)

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0
    assert (
        capsys.readouterr().out.splitlines()[-1]
        == "STATUS: ACTIVE (Operators at 350% of threshold)"
    )

    report = json.loads(json_path.read_text())
    criteria = report["criteria"]
    assert (criteria["total"], criteria["met"]) == (4, 2)
    assert criteria["cpu"]["result"] == "ACTIVE"
    # The burst cleared its firing level: the ratio sits past 1.0.
    assert criteria["cpu"]["threshold_ratio"] > 1
    assert criteria["memory"]["result"] == "IDLE"
    assert criteria["api_server"]["result"] == "IDLE"
    assert criteria["operators"]["result"] == "ACTIVE"


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
    quiet_memory_and_api(responses)

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == [
        "CPU: IDLE",
        "Memory: IDLE",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: UNKNOWN",
        "STATUS: IDLE (closest to active: Memory at 29% of threshold)",
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
    quiet_memory_and_api(responses)

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


def test_gpu_cluster_adds_fifth_criterion(monkeypatch, prom_server, tmp_path):
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    nodes = [node_item("node-1"), node_item("gpu-node-1", instance_type="g5.xlarge", gpus="4")]
    oc_responses = base_oc_responses(nodes)
    oc_responses[("get", "pods", "-A", "-o", oic.PODS_ALL_JSONPATH)] = OPERATOR_PODS_YOUNG
    oc_responses[("get", "rs", "-A", "-o", oic.REPLICASETS_ALL_JSONPATH)] = (
        OPERATOR_REPLICASETS_YOUNG
    )
    # Per-node instant usage comes from the cluster-wide snapshot: the GPU
    # node has its own line there, and no per-node `adm top node` may run.
    oc_responses[("adm", "top", "nodes", "--no-headers")] = (
        "node-1 250m 2% 3212Mi 5%\ngpu-node-1 500m 3% 8000Mi 6%\n"
    )
    calls = install_fake_oc(monkeypatch, oc_responses)

    quiet_cpu(responses, ["node-1"])
    quiet_memory_and_api(responses)
    # One 15-minute window at 80% GPU utilization.
    responses[("/api/v1/query_range", oic.GPU_RANGE_QUERY)] = prom_range_json(
        [make_dcgm_series(["0", "0", "0", "80"])]
    )
    # Batched windowed usage queries from the gpu report (the ML node
    # display is verbose-only, so a quiet run never issues its query).
    responses[("/api/v1/query", node_cpu_window_query(["gpu-node-1"], WINDOW))] = (
        prom_instant_series_json({"gpu-node-1:9100": "7"})
    )
    responses[("/api/v1/query", node_mem_window_query(["gpu-node-1"], WINDOW))] = (
        prom_instant_series_json({"gpu-node-1:9100": "11"})
    )

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0

    assert ("adm", "top", "node", "gpu-node-1", "--no-headers") not in calls
    report = json.loads(json_path.read_text())
    criteria = report["criteria"]
    # Five criteria now; cpu, memory, and api vote IDLE - 3 of 5 is below 80%.
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (5, 3, 4)
    assert criteria["gpu"]["result"] == "ACTIVE"
    # The 80% window cleared its firing level: the ratio sits past 1.0.
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
    quiet_memory_and_api(responses)
    responses[("/api/v1/query_range", oic.GPU_RANGE_QUERY)] = prom_range_json(
        [make_dcgm_series(["0"] * 8)]
    )
    # The gpu report and the ML node display cover the same node, so their
    # batched queries are identical strings served by the same entry.
    responses[("/api/v1/query", node_cpu_window_query(["gpu-node-1"], WINDOW))] = (
        prom_instant_series_json({"gpu-node-1:9100": "7"})
    )
    responses[("/api/v1/query", node_mem_window_query(["gpu-node-1"], WINDOW))] = (
        prom_instant_series_json({"gpu-node-1:9100": "11"})
    )

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
    quiet_memory_and_api(responses)

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
    # UNKNOWN and uncounted.  One IDLE vote of three counted - below the
    # quorum - makes the verdict ACTIVE with nothing fired, and the STATUS
    # annotation says so instead of implying a hot criterion.
    url, responses = prom_server
    monkeypatch.setenv("OCP_IDLE_PROMETHEUS_URL", url)
    oc_responses = base_oc_responses(
        [node_item("node-1"), node_item("node-2"), node_item("node-3")]
    )
    oc_responses[("get", "events", "-A", "-o", "json")] = None
    install_fake_oc(monkeypatch, oc_responses)
    # Only the API criterion has Prometheus data; the server 404s the
    # CPU/memory queries, which the client reads as no data.
    responses[("/api/v1/query", api_avg_query(WINDOW))] = prom_instant_json("5")
    responses[("/api/v1/query_range", oic.API_RANGE_QUERY)] = prom_range_json(
        [{"metric": {}, "values": [[i * 900, "5"] for i in range(8)]}]
    )

    json_path = tmp_path / "report.json"
    exit_code = oic.run(["-q", "--json", str(json_path), "-w", str(WINDOW)])
    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "CPU: UNKNOWN",
        "Memory: UNKNOWN",
        "API Server: IDLE",
        "GPU: N/A",
        "Operators: UNKNOWN",
        "STATUS: ACTIVE (UNKNOWN-driven; API Server at 19% of threshold)",
    ]

    criteria = json.loads(json_path.read_text())["criteria"]
    assert (criteria["total"], criteria["met"], criteria["threshold"]) == (3, 1, 2)
    assert criteria["cpu"]["result"] == "UNKNOWN"
    assert criteria["memory"]["result"] == "UNKNOWN"
    assert criteria["operators"]["result"] == "UNKNOWN"
    assert criteria["driver"] == {
        "criterion": "api_server",
        "label": "API Server",
        "threshold_ratio": 0.19,
        "quorum_driven": True,
    }


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
    # relies on.
    assert proc.stdout.strip() == f"ran {Path.cwd() / 'ocp_idle_check.py'} -q"

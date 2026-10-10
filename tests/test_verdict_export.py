"""Tests for the 80% verdict rule and the CSV/JSON exports."""

from __future__ import annotations

import json

import pytest

import ocp_idle_check as oic
from helpers import base_config


def outcome(result: str, counted: bool = True) -> oic.CheckOutcome:
    return oic.CheckOutcome(result, {"result": result}, counted)


# === VERDICT ================================================================


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        # All five voting IDLE.
        (
            [outcome("IDLE")] * 5,
            (5, 5, 4, "IDLE", 1),
        ),
        # Four criteria (operators N/A), all IDLE.
        (
            [outcome("IDLE")] * 4 + [outcome("N/A", counted=False)],
            (4, 4, 3, "IDLE", 1),
        ),
        # Boundary: 3 of 4 met is exactly 80% - still IDLE.
        (
            [outcome("IDLE")] * 3 + [outcome("ACTIVE")] + [outcome("N/A", counted=False)],
            (4, 3, 3, "IDLE", 1),
        ),
        # 2 of 4 met is below the threshold.
        (
            [outcome("IDLE")] * 2 + [outcome("ACTIVE")] * 2 + [outcome("N/A", counted=False)],
            (4, 2, 3, "ACTIVE", 0),
        ),
        # A single ACTIVE criterion among four still yields IDLE - the bash
        # script had the same property (int(4 * 0.80) = 3).
        (
            [outcome("ACTIVE")] + [outcome("IDLE")] * 3,
            (4, 3, 3, "IDLE", 1),
        ),
        # CPU UNKNOWN still counts in the denominator but does not vote
        # IDLE; 4 of 5 is exactly 80%.
        (
            [outcome("UNKNOWN")] + [outcome("IDLE")] * 4,
            (5, 4, 4, "IDLE", 1),
        ),
        # Everything unknown or N/A: total 0, threshold 0, 0 >= 0 - IDLE.
        # Unreachable in practice (cpu/memory UNKNOWN still count), but it
        # is what the bash formula did, so it is what this does.
        (
            [outcome("UNKNOWN", counted=False)] * 5,
            (0, 0, 0, "IDLE", 1),
        ),
        (
            [outcome("ACTIVE")] * 5,
            (5, 0, 4, "ACTIVE", 0),
        ),
    ],
)
def test_compute_verdict(outcomes, expected):
    assert oic.compute_verdict(outcomes) == expected


# === EXPORTS ================================================================


def sample_report() -> dict:
    """A report shaped exactly like run()'s, with quiet-IDLE values."""
    return {
        "timestamp": "2026-10-09T00:00:00+00:00",
        "timestamp_human": "Thu Oct 09 00:00:00 UTC 2026",
        "cluster": "https://api.test-cluster.example.com:6443",
        "status": "IDLE",
        "exit_code": 1,
        "configuration": {
            "time_window_minutes": 10080,
            "cpu_threshold": 15.0,
        },
        "criteria": {
            "total": 4,
            "met": 4,
            "threshold": 3,
            "cpu": {"result": "IDLE", "value": "2.00"},
            "memory": {"result": "IDLE", "value": "10.00"},
            "api_server": {"result": "IDLE", "value": "5.00"},
            "gpu": {"result": "N/A", "value": None},
            "operators": {
                "result": "IDLE",
                "age_days": 12.0,
                "units": [
                    {
                        "namespace": "opendatahub",
                        "kind": "replicaset",
                        "name": "odh-operator-5d4c3b",
                        "age_days": 12.0,
                    }
                ],
                "namespaces": ["opendatahub"],
                "events": 0,
                "events_excluded_workload": 0,
                "event_window_hours": 48,
            },
        },
        "gpu": {
            "has_gpu_nodes": False,
            "node_count": 0,
            "flavors": None,
            "node_age": None,
            "usage": {
                "cpu_current": None,
                "memory_current": None,
                "cpu_windowed": None,
                "memory_windowed": None,
            },
        },
    }


def test_export_csv_writes_header_once(tmp_path):
    path = tmp_path / "results.csv"
    oic.export_csv(sample_report(), path)
    oic.export_csv(sample_report(), path)
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert lines[0] == oic.CSV_HEADER
    assert lines[1] == lines[2]
    for row in lines[1:]:
        assert len(row.split(",")) == 24


def test_export_csv_renders_missing_values_as_na(tmp_path):
    path = tmp_path / "results.csv"
    oic.export_csv(sample_report(), path)
    row = path.read_text().splitlines()[1].split(",")
    assert row[2] == "IDLE"  # status
    assert row[9] == "N/A"  # gpu_result
    assert row[10] == "N/A"  # gpu_value (absent -> N/A, not empty)
    assert row[13] == "4"  # criteria_met
    assert row[14] == "4"  # total_criteria
    assert row[19] == "N/A"  # gpu_node_age
    assert row[20:24] == ["N/A"] * 4  # gpu usage columns


def test_export_csv_renders_unknown_gpu_presence(tmp_path):
    # A failed node listing leaves has_gpu_nodes/node_count as None; the CSV
    # row must say "unknown", not "false" - that cluster is not known to be
    # free of GPU nodes.
    report = sample_report()
    report["gpu"] = {
        "has_gpu_nodes": None,
        "node_count": None,
        "flavors": None,
        "node_age": None,
        "usage": {
            "cpu_current": None,
            "memory_current": None,
            "cpu_windowed": None,
            "memory_windowed": None,
        },
    }
    path = tmp_path / "results.csv"
    oic.export_csv(report, path)
    row = path.read_text().splitlines()[1].split(",")
    assert row[16] == "unknown"  # has_gpu_nodes
    assert row[17] == "N/A"  # gpu_node_count


def test_export_json_round_trips(tmp_path):
    path = tmp_path / "report.json"
    oic.export_json(sample_report(), path)
    data = json.loads(path.read_text())
    assert data["status"] == "IDLE"
    assert data["criteria"]["gpu"]["value"] is None
    # json.dump never emits Infinity; a null ratio must survive the round trip.
    assert "Infinity" not in path.read_text()


def test_base_config_matches_shipped_defaults():
    # Guards the test helpers against drifting away from the real defaults.
    cfg = base_config()
    assert cfg.time_window_minutes == oic.DEFAULT_TIME_WINDOW_MINUTES
    assert cfg.cpu_peak_threshold == oic.CPU_PEAK_THRESHOLD
    assert cfg.api_variance_ratio == oic.API_VARIANCE_RATIO
    assert cfg.operator_namespaces == oic.OPERATOR_NAMESPACES
    assert cfg.operator_exclude_prefixes == oic.OPERATOR_EXCLUDE_PREFIXES
    assert cfg.operator_event_hours == oic.OPERATOR_EVENT_WINDOW_HOURS

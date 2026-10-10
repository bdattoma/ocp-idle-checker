"""Tests for command-line parsing."""

from __future__ import annotations

import pytest

import ocp_idle_check as oic


def test_defaults():
    cfg = oic.parse_args([])
    assert cfg.time_window_minutes == 10080
    assert cfg.cpu_idle_threshold == 15.0
    assert cfg.memory_idle_threshold == 35.0
    assert cfg.api_threshold == 100.0
    assert cfg.operator_age_days == 7
    assert cfg.operator_namespaces == oic.OPERATOR_NAMESPACES
    assert cfg.operator_exclude_prefixes == oic.OPERATOR_EXCLUDE_PREFIXES
    assert cfg.operator_event_hours == oic.OPERATOR_EVENT_WINDOW_HOURS
    assert cfg.verbose is True
    assert cfg.check_ml_nodes is True
    assert cfg.debug_probe is False
    assert cfg.token == ""
    assert cfg.csv_path is None
    assert cfg.json_path is None


def test_variance_rule_defaults():
    cfg = oic.parse_args([])
    assert cfg.cpu_peak_threshold == 30.0
    assert cfg.cpu_variance_ratio == 2.0
    assert cfg.gpu_peak_threshold == 40.0
    assert cfg.gpu_variance_ratio == 2.0
    assert cfg.api_variance_ratio == 2.0


def test_window_flag():
    assert oic.parse_args(["-w", "60"]).time_window_minutes == 60
    assert oic.parse_args(["--window", "1440"]).time_window_minutes == 1440


def test_quiet_flag():
    assert oic.parse_args(["-q"]).verbose is False


def test_no_ml_check_flag():
    assert oic.parse_args(["--no-ml-check"]).check_ml_nodes is False


def test_variance_rule_flags():
    cfg = oic.parse_args(
        [
            "--cpu-peak-threshold",
            "55",
            "--cpu-variance-ratio",
            "3",
            "--gpu-peak-threshold",
            "60",
            "--gpu-variance-ratio",
            "2.5",
            "--api-variance-ratio",
            "4",
        ]
    )
    assert cfg.cpu_peak_threshold == 55.0
    assert cfg.cpu_variance_ratio == 3.0
    assert cfg.gpu_peak_threshold == 60.0
    assert cfg.gpu_variance_ratio == 2.5
    assert cfg.api_variance_ratio == 4.0


def test_removed_and_renamed_variance_flags_are_rejected():
    # The API variance floor flag was removed outright (the CPU/GPU floors
    # are module constants, never flags), and the ratio/floor flags were
    # renamed to *-variance-*; old spellings must fail loudly rather than
    # be silently ignored.
    with pytest.raises(SystemExit):
        oic.parse_args(["--cpu-shape-floor", "20"])
    with pytest.raises(SystemExit):
        oic.parse_args(["--gpu-shape-floor", "20"])
    with pytest.raises(SystemExit):
        oic.parse_args(["--cpu-shape-ratio", "3"])
    with pytest.raises(SystemExit):
        oic.parse_args(["--gpu-shape-ratio", "3"])
    with pytest.raises(SystemExit):
        oic.parse_args(["--api-spike-ratio", "4"])
    with pytest.raises(SystemExit):
        oic.parse_args(["--api-spike-floor", "75"])
    with pytest.raises(SystemExit):
        oic.parse_args(["--api-variance-floor", "75"])


def test_legacy_flags_still_parse():
    cfg = oic.parse_args(
        [
            "-c",
            "20",
            "-m",
            "40",
            "-a",
            "200",
            "-e",
            "30",
            "-o",
            "14",
            "--operator-namespaces",
            "foo,bar",
            "--token",
            "t",
            "--debug-probe",
        ]
    )
    assert cfg.cpu_idle_threshold == 20.0
    assert cfg.memory_idle_threshold == 40.0
    assert cfg.api_threshold == 200.0
    assert cfg.event_history_minutes == 30
    assert cfg.operator_age_days == 14
    assert cfg.operator_namespaces == "foo,bar"
    assert cfg.token == "t"
    assert cfg.debug_probe is True


def test_export_paths_parse(tmp_path):
    csv_path = tmp_path / "out.csv"
    json_path = tmp_path / "out.json"
    cfg = oic.parse_args(["--csv", str(csv_path), "--json", str(json_path)])
    assert cfg.csv_path == csv_path
    assert cfg.json_path == json_path


def test_unknown_flag_exits_2():
    with pytest.raises(SystemExit) as exc:
        oic.parse_args(["--nope"])
    assert exc.value.code == 2


def test_help_exits_0(capsys):
    with pytest.raises(SystemExit) as exc:
        oic.parse_args(["-h"])
    assert exc.value.code == 0
    # Help is addressed to the shim name callers actually invoke.
    assert "ocp-idle-check.sh" in capsys.readouterr().out


def test_abbreviations_are_rejected():
    # --win must not silently expand to --window; the bash original's getopt
    # behavior was equally strict.
    with pytest.raises(SystemExit):
        oic.parse_args(["--win", "60"])

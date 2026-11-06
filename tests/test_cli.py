"""Tests for command-line parsing."""

from __future__ import annotations

import sys

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
    assert cfg.token == ""
    assert cfg.csv_path is None
    assert cfg.json_path is None


def test_variance_rule_defaults():
    cfg = oic.parse_args([])
    assert cfg.cpu_peak_threshold == 30.0
    assert cfg.cpu_variance_ratio == 2.0  # trough baseline: 2x is the least burst
    assert cfg.gpu_peak_threshold == 40.0
    assert cfg.gpu_variance_ratio == 2.0
    assert cfg.api_variance_ratio == 1.2  # API keeps the median baseline
    assert cfg.memory_variance_points == 5.0
    assert cfg.memory_variance_windows == 4


def test_cpu_peak_threshold_scales_with_cores():
    # Hard to max out a large machine: the absolute branch eases from 30%
    # at 10 vCPUs to 8% at 100 along a power law, never below 8% - below
    # that only the variance rule can accuse.
    assert oic.cpu_peak_threshold(None) == 30.0
    assert oic.cpu_peak_threshold(4) == 30.0
    assert oic.cpu_peak_threshold(10) == 30.0
    assert oic.cpu_peak_threshold(100) == 8.0
    assert oic.cpu_peak_threshold(160) == 8.0
    # On the curve between the anchors, monotone decreasing.
    mid = oic.cpu_peak_threshold(32)
    assert 8.0 < mid < 30.0
    assert mid > oic.cpu_peak_threshold(64)


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
            "--memory-variance-points",
            "6",
            "--memory-variance-windows",
            "3",
        ]
    )
    assert cfg.cpu_peak_threshold == 55.0
    assert cfg.cpu_variance_ratio == 3.0
    assert cfg.gpu_peak_threshold == 60.0
    assert cfg.gpu_variance_ratio == 2.5
    assert cfg.api_variance_ratio == 4.0
    assert cfg.memory_variance_points == 6.0
    assert cfg.memory_variance_windows == 3


def test_removed_and_renamed_variance_flags_are_rejected():
    # The API variance floor flag was removed outright (the CPU/GPU floors
    # are module constants, never flags), and the ratio/floor flags were
    # renamed to *-variance-*; old spellings must fail loudly rather than
    # be silently ignored.  The same goes for --debug-probe: it gated a
    # diagnostics export that no longer exists.
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
    with pytest.raises(SystemExit):
        oic.parse_args(["--debug-probe"])


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
        ]
    )
    assert cfg.cpu_idle_threshold == 20.0
    assert cfg.memory_idle_threshold == 40.0
    assert cfg.api_threshold == 200.0
    assert cfg.event_history_minutes == 30
    assert cfg.operator_age_days == 14
    assert cfg.operator_namespaces == "foo,bar"
    assert cfg.token == "t"


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


def test_module_entry_maps_uncaught_exception_to_exit_2(monkeypatch, capsys):
    # An uncaught exception's default exit status is 1, which the contract
    # reads as IDLE; the module entry point must convert it to an error exit.
    import ocp_idle_check.__main__ as entry
    import ocp_idle_check.main as main_module

    def boom(argv):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main_module, "run", boom)
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "unexpected failure: RuntimeError: kaboom" in err
    assert "Traceback" in err


def test_module_entry_maps_keyboard_interrupt_to_exit_2(monkeypatch):
    # Ctrl-C's default exit status is also 1; an interrupted run is an error.
    import ocp_idle_check.__main__ as entry
    import ocp_idle_check.main as main_module

    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "run", interrupted)
    assert entry.main() == 2


def test_module_entry_maps_import_failure_to_exit_2(monkeypatch, capsys):
    # The package import runs inside the guarded block, so a broken module
    # is an error exit, not a traceback exit 1.
    import ocp_idle_check.__main__ as entry

    monkeypatch.setitem(sys.modules, "ocp_idle_check.main", None)
    assert entry.main() == 2
    assert "unexpected failure: ModuleNotFoundError" in capsys.readouterr().err

"""CSV and JSON exports of the run report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CSV_HEADER = (
    "timestamp,cluster,status,cpu_result,cpu_value,memory_result,memory_value,"
    "api_server_result,api_server_value,gpu_result,gpu_value,operators_result,"
    "operator_age_days,criteria_met,total_criteria,time_window_minutes,"
    "has_gpu_nodes,gpu_node_count,gpu_flavors,gpu_node_age,gpu_cpu_current,"
    "gpu_mem_current,gpu_cpu_windowed,gpu_mem_windowed"
)


def export_csv(report: dict[str, Any], path: Path) -> None:
    """Append one result row; write the header only for a new file."""
    gpu = report["gpu"]
    criteria = report["criteria"]
    row = [
        report["timestamp"],
        report["cluster"],
        report["status"],
        criteria["cpu"]["result"],
        criteria["cpu"]["value"] or "N/A",
        criteria["memory"]["result"],
        criteria["memory"]["value"] or "N/A",
        criteria["api_server"]["result"],
        criteria["api_server"]["value"] or "N/A",
        criteria["gpu"]["result"],
        criteria["gpu"]["value"] or "N/A",
        criteria["operators"]["result"],
        criteria["operators"]["age_days"],
        criteria["met"],
        criteria["total"],
        report["configuration"]["time_window_minutes"],
        "unknown"
        if gpu["has_gpu_nodes"] is None
        else ("true" if gpu["has_gpu_nodes"] else "false"),
        gpu["node_count"] if gpu["node_count"] is not None else "N/A",
        gpu["flavors"] or "N/A",
        gpu["node_age"] or "N/A",
        gpu["usage"]["cpu_current"] or "N/A",
        gpu["usage"]["memory_current"] or "N/A",
        gpu["usage"]["cpu_windowed"] or "N/A",
        gpu["usage"]["memory_windowed"] or "N/A",
    ]
    is_new = not path.exists()
    with path.open("a", newline="") as fh:
        if is_new:
            fh.write(CSV_HEADER + "\n")
        fh.write(",".join(str(field) for field in row) + "\n")


def export_json(report: dict[str, Any], path: Path) -> None:
    with path.open("w") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")

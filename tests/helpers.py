"""Shared helpers for the ocp_idle_check test suite.

Importable as a top-level module because conftest.py puts this directory
on sys.path.  Unit tests stub the Prometheus transport (FakeProm) and the
oc CLI's operator calls (FakeOc) at their Python seams; the end-to-end
test stubs Oc.run with strict argv matching and drives the real HTTP
client against a local server.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import ocp_idle_check as oic


class FakeProm:
    """In-memory stand-in for PrometheusClient; queries match exactly."""

    def __init__(
        self,
        instant: dict[str, float | None] | None = None,
        instant_all: dict[str, list[dict]] | None = None,
        ranges: dict[str, list[dict]] | None = None,
    ) -> None:
        self.instant = instant or {}
        self.instant_all = instant_all or {}
        self.ranges = ranges or {}
        self.range_calls: list[str] = []

    def query(self, promql: str) -> float | None:
        return self.instant.get(promql)

    def query_all(self, promql: str) -> list[dict] | None:
        return self.instant_all.get(promql)

    def query_values(self, promql: str) -> list[float]:
        # Mirrors PrometheusClient.query_values: every series' value, junk dropped.
        return oic._to_floats(
            [s.get("value", [None, None])[1] for s in self.query_all(promql) or []]
        )

    def query_range(self, promql: str, start_s: int, end_s: int, step_s: int = 900):
        self.range_calls.append(promql)
        return self.ranges.get(promql)


def make_range_series(node: str, values: list[str]) -> dict:
    """One query_range result series for a node label and sample values."""
    return {
        "metric": {"node": node, "instance": f"{node}:9100"},
        "values": [[1700000000 + i * 900, v] for i, v in enumerate(values)],
    }


def make_dcgm_series(values: list[str], gpu: str = "0", instance: str = "gpu-node:9400") -> dict:
    """One query_range result series for a DCGM utilization gauge.

    Real DCGM series carry an exporter `instance` label (host:port) and no
    `node` label, so per-node grouping keys on it; cards on the same node
    share the instance and pool into one node's windows.
    """
    return {
        "metric": {"gpu": gpu, "Hostname": "gpu-node", "instance": instance},
        "values": [[1700000000 + i * 900, v] for i, v in enumerate(values)],
    }


def base_config(**overrides) -> oic.Config:
    """A Config with the shipped defaults, quiet, plus test overrides."""
    values = dict(
        time_window_minutes=10080,
        cpu_idle_threshold=15.0,
        memory_idle_threshold=35.0,
        api_threshold=100.0,
        operator_age_days=7,
        operator_namespaces="opendatahub,redhat-ods-operator,redhat-ods-applications",
        operator_exclude_prefixes="openshift-,kube-,open-cluster-management-",
        operator_event_hours=48,
        event_history_minutes=60,
        check_ml_nodes=True,
        ml_node_pattern="p5|p4d|g5",
        cpu_peak_threshold=30.0,
        cpu_variance_ratio=2.0,
        gpu_peak_threshold=40.0,
        gpu_variance_ratio=2.0,
        api_variance_ratio=2.0,
        verbose=False,
        token="",
        csv_path=None,
        json_path=None,
    )
    values.update(overrides)
    return oic.Config(**values)


class FakeOc:
    """In-memory stand-in for the oc CLI's operator-criterion calls.

    `pods`/`replicasets` are jsonpath row lists, `events` a list of
    `oc get events -A -o json` items; all default to genuinely-empty
    listings.  The matching `*_failed=True` simulates an oc error or
    timeout, which the real wrapper reports as None - a failed listing,
    not an empty one.
    """

    def __init__(
        self,
        pods: list[str] | None = None,
        replicasets: list[str] | None = None,
        events: list[dict] | None = None,
        pods_failed: bool = False,
        replicasets_failed: bool = False,
        events_failed: bool = False,
    ) -> None:
        self.pod_rows = pods or []
        self.rs_rows = replicasets or []
        self.event_items = events or []
        self.pods_failed = pods_failed
        self.replicasets_failed = replicasets_failed
        self.events_failed = events_failed
        self.calls: list[str] = []

    def pods_all(self) -> list[str] | None:
        self.calls.append("pods")
        return None if self.pods_failed else self.pod_rows

    def replicasets_all(self) -> list[str] | None:
        self.calls.append("replicasets")
        return None if self.replicasets_failed else self.rs_rows

    def events_all(self) -> list[dict] | None:
        self.calls.append("events")
        return None if self.events_failed else self.event_items


# Builders mirroring the module's listing shapes - the pods/replicasets
# jsonpath rows and the events JSON items; tests key their FakeOc data on
# these so a changed shape surfaces here instead of silently producing
# garbage ages downstream.


def ts_before(now: datetime, **delta) -> str:
    """A Kubernetes timestamp `delta` (days=..., hours=...) before `now`."""
    return (now - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_time_before(now: datetime, **delta) -> str:
    """An events-API eventTime `delta` before `now`: microsecond precision,
    the metav1.MicroTime shape an event recorded through the newer events
    API carries instead of the legacy second-precision timestamps."""
    return (now - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pod_row(
    namespace: str, pod: str, owner_kind: str = "", owner_name: str = "", created: str = ""
) -> str:
    """One `oc get pods -A` row: namespace, pod, owner kind, owner name, created."""
    return f"{namespace}\t{pod}\t{owner_kind}\t{owner_name}\t{created}"


def rs_row(namespace: str, name: str, created: str) -> str:
    """One `oc get rs -A` row: namespace, name, created."""
    return f"{namespace}\t{name}\t{created}"


def make_event(
    namespace: str,
    *,
    now: datetime,
    hours: float = 1.0,
    type_: str = "Normal",
    reason: str = "Pulled",
    kind: str = "Pod",
    involved: str = "odh-xyz",
    message: str = "quiet",
    timestamp: str | None = None,
    event_time: str | None = None,
) -> dict:
    """One `oc get events -A -o json` item.  `timestamp` populates both
    legacy columns (auto-computed from `hours` unless given);
    `timestamp=""` leaves them null - the shape of an event recorded
    through the newer events API, which carries only `event_time`
    (microsecond precision) instead."""
    ts = ts_before(now, hours=hours) if timestamp is None else (timestamp or None)
    return {
        "metadata": {"namespace": namespace},
        "type": type_,
        "reason": reason,
        "involvedObject": {"kind": kind, "name": involved},
        "message": message,
        "lastTimestamp": ts,
        "firstTimestamp": ts,
        "eventTime": event_time,
    }


# Expected query strings, mirroring the f-strings the module builds.  Tests
# key their FakeProm / prom_server data on these so any change to a query
# fails loudly instead of silently returning no data.


def legacy_cpu_query(minutes: int) -> str:
    return f'(1 - avg(rate(node_cpu_seconds_total{{mode="idle"}}[{minutes}m]))) * 100'


def memory_window_query(minutes: int) -> str:
    return (
        "(1 - avg_over_time((sum(node_memory_MemAvailable_bytes) / "
        f"sum(node_memory_MemTotal_bytes))[{minutes}m:])) * 100"
    )


def api_avg_query(minutes: int) -> str:
    return f"sum(rate(apiserver_request_total[{minutes}m]))"


def gpu_max_query(minutes: int) -> str:
    return f"max_over_time(DCGM_FI_DEV_GPU_UTIL[{minutes}m])"


def gpu_avg_query(minutes: int) -> str:
    return f"avg_over_time(DCGM_FI_DEV_GPU_UTIL[{minutes}m])"


def _promql_instance_pattern(nodes: list[str]) -> str:
    """The instance=~ alternation: re.escape patterns with every backslash
    doubled, because a lone backslash is an invalid escape in a double-quoted
    PromQL string literal and fails the whole query."""
    return "|".join(re.escape(node).replace("\\", "\\\\") + ".*" for node in nodes)


def node_cpu_window_query(nodes: list[str], minutes: int) -> str:
    """The batched per-node windowed CPU query (one round trip for all nodes)."""
    pattern = _promql_instance_pattern(nodes)
    return (
        '(1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",'
        f'instance=~"{pattern}"}}[{minutes}m]))) * 100'
    )


def node_mem_window_query(nodes: list[str], minutes: int) -> str:
    """The batched per-node windowed memory query (one round trip for all nodes)."""
    pattern = _promql_instance_pattern(nodes)
    return (
        "(1 - avg_over_time((avg by (instance) (node_memory_MemAvailable_bytes"
        f'{{instance=~"{pattern}"}}) / avg by (instance) (node_memory_MemTotal_bytes'
        f'{{instance=~"{pattern}"}}))[{minutes}m:])) * 100'
    )


def prom_instant_json(value: str) -> str:
    """A successful instant-query response body with a single series."""
    return (
        '{"status":"success","data":{"resultType":"vector","result":'
        f'[{{"metric":{{}},"value":[1700000000,"{value}"]}}]}}}}'
    )


def prom_instant_series_json(series: dict[str, str]) -> str:
    """A successful instant-query response with one series per instance label."""
    result = [
        {"metric": {"instance": instance}, "value": [1700000000, value]}
        for instance, value in series.items()
    ]
    return json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}})


def prom_range_json(series: list[dict]) -> str:
    """A successful query_range response body."""
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": series}})

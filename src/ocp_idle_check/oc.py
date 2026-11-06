"""The oc CLI wrapper: subprocess calls, JSON listings, route URL lookup."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from .config import (
    OC_LIST_TIMEOUT,
    OC_TIMEOUT,
    PODS_ALL_JSONPATH,
    REPLICASETS_ALL_JSONPATH,
    ROUTE_TIMEOUT,
)
from .log import log_warn
from .nodes import node_top_percentages


class Oc:
    """Thin wrapper around the oc CLI with per-command result caching."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._node_items: list[dict[str, Any]] | None = None
        self._nodes_fetched = False
        self._top_lines: list[str] | None = None
        self._top_map: dict[str, tuple[float, float]] | None = None
        self._event_items: list[dict[str, Any]] | None = None
        self._events_fetched = False

    def run(self, args: list[str], timeout: float = OC_TIMEOUT) -> str | None:
        """Run oc; return stdout on success, None on failure or timeout."""
        try:
            proc = subprocess.run(["oc", *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if self.verbose:
                log_warn(f"oc {' '.join(args)} failed: {exc}")
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def logged_in(self) -> bool:
        return self.run(["whoami"]) is not None

    def whoami_server(self) -> str:
        out = self.run(["whoami", "--show-server"])
        return (out or "").strip() or "Unknown"

    def whoami_token(self) -> str | None:
        out = self.run(["whoami", "-t"])
        return out.strip() or None if out else None

    def create_prometheus_token(self) -> str | None:
        out = self.run(
            ["create", "token", "prometheus-k8s", "-n", "openshift-monitoring", "--duration=10m"]
        )
        return out.strip() or None if out else None

    def route_host(self) -> str | None:
        out = self.run(
            [
                "get",
                "route",
                "thanos-querier",
                "-n",
                "openshift-monitoring",
                "-o",
                "jsonpath={.spec.host}",
            ],
            timeout=ROUTE_TIMEOUT,
        )
        return out.strip() or None if out else None

    def top_nodes(self) -> list[str]:
        """Cached `oc adm top nodes --no-headers` lines."""
        if self._top_lines is None:
            out = self.run(["adm", "top", "nodes", "--no-headers"])
            self._top_lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        return self._top_lines

    def top_node_map(self) -> dict[str, tuple[float, float]]:
        """Per-node (CPU%, MEM%) parsed from the cached cluster-wide snapshot.

        Replaces per-node `oc adm top node <name>` spawns — one snapshot
        serves every consumer.
        """
        if self._top_map is None:
            self._top_map = node_top_percentages(self.top_nodes())
        return self._top_map

    def nodes(self) -> list[dict[str, Any]] | None:
        """Cached `oc get nodes -o json` items; None when the oc call fails
        or the listing is malformed, like events_all - a failed listing is
        not an empty one, and unknown node inventory is not "no nodes".
        The failure is cached."""
        if not self._nodes_fetched:
            self._nodes_fetched = True
            out = self.run(["get", "nodes", "-o", "json"], timeout=OC_LIST_TIMEOUT)
            items: list[dict[str, Any]] | None
            try:
                parsed = json.loads(out or "{}").get("items")
                items = parsed if isinstance(parsed, list) else None
            except json.JSONDecodeError:
                items = None
            self._node_items = items
        return self._node_items

    def pods_all(self) -> list[str] | None:
        """Cluster-wide pod rows: namespace, name, owner kind, owner name,
        created.  None when the oc call fails - a failed listing is not an
        empty one, and the operators criterion reports UNKNOWN on it."""
        out = self.run(["get", "pods", "-A", "-o", PODS_ALL_JSONPATH], timeout=OC_LIST_TIMEOUT)
        if out is None:
            return None
        return [ln for ln in out.splitlines() if ln.strip()]

    def replicasets_all(self) -> list[str] | None:
        """Cluster-wide ReplicaSet rows: namespace, name, created.  None when
        the oc call fails, like pods_all."""
        out = self.run(["get", "rs", "-A", "-o", REPLICASETS_ALL_JSONPATH], timeout=OC_LIST_TIMEOUT)
        if out is None:
            return None
        return [ln for ln in out.splitlines() if ln.strip()]

    def events_all(self) -> list[dict[str, Any]] | None:
        """Cached cluster-wide event items from `oc get events -A -o json`;
        None when the oc call fails, like pods_all.  JSON rather than
        jsonpath rows: a message is free text, and one with an embedded
        newline splits a jsonpath row into fragments the parser drops
        (about one event in seven on a live-cluster sample).  A listing
        that is not a JSON item list is None too - a malformed listing is
        a failed listing, not an empty one.  The failure is cached."""
        if not self._events_fetched:
            out = self.run(["get", "events", "-A", "-o", "json"], timeout=OC_LIST_TIMEOUT)
            self._events_fetched = True
            items: list[dict[str, Any]] | None
            try:
                parsed = json.loads(out or "{}").get("items")
                items = parsed if isinstance(parsed, list) else None
            except json.JSONDecodeError:
                items = None
            self._event_items = items
        return self._event_items

    def machines(self) -> list[dict[str, Any]]:
        out = self.run(["get", "machines", "-n", "openshift-machine-api", "-o", "json"])
        try:
            return json.loads(out or "{}").get("items", [])
        except json.JSONDecodeError:
            return []

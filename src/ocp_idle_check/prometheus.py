"""Minimal Prometheus/Thanos client over urllib (unverified TLS, like oc)."""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import PROM_URL_OVERRIDE, QUERY_TIMEOUT, RANGE_TIMEOUT, STEP_SECONDS
from .log import log_warn
from .oc import Oc

_UNVERIFIED_CTX = ssl.create_default_context()
_UNVERIFIED_CTX.check_hostname = False
_UNVERIFIED_CTX.verify_mode = ssl.CERT_NONE


class PrometheusClient:
    """Minimal Prometheus/Thanos client over urllib (unverified TLS, like
    the bash script's `curl -sk`).  Queries are POSTed as form bodies
    rather than GET query strings: the batched per-node queries carry
    node-name alternations (twice, for the memory ratio) that can outgrow
    a proxy's URL limit on a large fleet, and a form body is not subject
    to one."""

    def __init__(self, oc: Oc, token: str = "", verbose: bool = False) -> None:
        self._oc = oc
        self.verbose = verbose
        self._base: str | None = None
        self._base_resolved = False
        self._token: str | None = token or None
        self._token_resolved = bool(token)
        # The five criteria run in a thread pool; the lazy endpoint/token
        # resolution below must happen exactly once for all of them.
        self._init_lock = threading.Lock()

    def _base_url(self) -> str | None:
        with self._init_lock:
            if not self._base_resolved:
                self._base_resolved = True
                override = os.environ.get(PROM_URL_OVERRIDE, "")
                if override:
                    self._base = override.rstrip("/")
                else:
                    host = self._oc.route_host()
                    self._base = f"https://{host}" if host else None
            return self._base

    def _bearer(self) -> str:
        with self._init_lock:
            if not self._token_resolved:
                self._token_resolved = True
                self._token = self._oc.whoami_token() or self._oc.create_prometheus_token()
            return self._token or ""

    def _post_json(
        self, path: str, params: dict[str, str], timeout: float
    ) -> dict[str, Any] | None:
        base = self._base_url()
        if base is None:
            return None
        # The params travel as a POST form body, never the URL: the batched
        # per-node queries embed a node-name alternation (twice, for the
        # memory ratio), and on a fleet with 100+ GPU nodes that string can
        # outgrow a proxy's URL limit - a failure that would drop the query
        # silently.  Prometheus accepts form-encoded POSTs on both the
        # instant and range endpoints.
        body = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(
            f"{base}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=_UNVERIFIED_CTX
            ) as response:
                data = json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if self.verbose:
                log_warn(f"Prometheus request failed: {exc}")
            return None
        if not isinstance(data, dict) or data.get("status") != "success":
            if self.verbose:
                log_warn(f"Prometheus query was not successful: {path}")
            return None
        return data

    def query(self, promql: str) -> float | None:
        """Instant query; returns the first series' value or None."""
        data = self._post_json("/api/v1/query", {"query": promql}, QUERY_TIMEOUT)
        if data is None:
            return None
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def query_all(self, promql: str) -> list[dict[str, Any]] | None:
        """Instant query; returns every result series (or None on failure)."""
        data = self._post_json("/api/v1/query", {"query": promql}, QUERY_TIMEOUT)
        if data is None:
            return None
        return data.get("data", {}).get("result", [])

    def query_values(self, promql: str) -> list[float]:
        """Instant query; every series' value as a float, junk dropped."""
        return _to_floats([s.get("value", [None, None])[1] for s in self.query_all(promql) or []])

    def query_range(
        self, promql: str, start_s: int, end_s: int, step_s: int = STEP_SECONDS
    ) -> list[dict[str, Any]] | None:
        """Range query; returns every result series with its `values` matrix."""
        data = self._post_json(
            "/api/v1/query_range",
            {"query": promql, "start": str(start_s), "end": str(end_s), "step": str(step_s)},
            RANGE_TIMEOUT,
        )
        if data is None:
            return None
        return data.get("data", {}).get("result", [])


def _to_floats(raw_values: list[Any]) -> list[float]:
    """Convert Prometheus sample values, dropping non-finite garbage."""
    out: list[float] = []
    for raw in raw_values:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append(value)
    return out


def _series_points(series: list[dict[str, Any]] | None) -> list[float]:
    """Flatten a query_range matrix into a pooled list of sample values."""
    if not series:
        return []
    points: list[float] = []
    for entry in series:
        points.extend(_to_floats([v for _, v in entry.get("values", [])]))
    return points


def _series_node(metric: dict[str, Any]) -> str:
    """Node name for a series: the `node` label, else `instance` sans port."""
    node = str(metric.get("node") or metric.get("instance") or "")
    return re.sub(r":[0-9]+$", "", node)

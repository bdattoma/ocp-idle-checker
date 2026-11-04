"""Unit tests for node facts and the per-node informational helpers."""

from __future__ import annotations

import re

import ocp_idle_check as oic


def test_worker_nodes_from_items_drops_role_labeled_nodes():
    items = [
        {"metadata": {"name": "master-0", "labels": {"node-role.kubernetes.io/master": ""}}},
        {"metadata": {"name": "cp-0", "labels": {"node-role.kubernetes.io/control-plane": ""}}},
        {"metadata": {"name": "infra-0", "labels": {"node-role.kubernetes.io/infra": ""}}},
        {"metadata": {"name": "worker-0", "labels": {"node-role.kubernetes.io/worker": ""}}},
        {"metadata": {"name": "bare-0", "labels": {}}},
    ]
    assert oic.worker_nodes_from_items(items) == {"worker-0", "bare-0"}


def test_master_nodes_from_items_keeps_only_control_plane():
    items = [
        {"metadata": {"name": "master-0", "labels": {"node-role.kubernetes.io/master": ""}}},
        {"metadata": {"name": "cp-0", "labels": {"node-role.kubernetes.io/control-plane": ""}}},
        {"metadata": {"name": "infra-0", "labels": {"node-role.kubernetes.io/infra": ""}}},
        {"metadata": {"name": "worker-0", "labels": {"node-role.kubernetes.io/worker": ""}}},
    ]
    # Infra is platform capacity but not control plane: the variance-only
    # CPU rule is about masters specifically.
    assert oic.master_nodes_from_items(items) == {"master-0", "cp-0"}


def test_node_top_percentages_maps_and_skips_malformed():
    lines = [
        "node-1 250m 2% 3212Mi 5%",
        "gpu-node-1 500m 3% 8000Mi 6%",
        "short line",
        "node-2 100m junk% 2000Mi 7%",
    ]
    # Malformed rows are dropped whole; later duplicates win, like a dict build.
    assert oic.node_top_percentages(lines) == {
        "node-1": (2.0, 5.0),
        "gpu-node-1": (3.0, 6.0),
    }


def test_instant_by_instance_and_average_matching():
    series = [
        {"metric": {"instance": "gpu-node-1:9100"}, "value": [1700000000, "7"]},
        {"metric": {"instance": "gpu-node-10:9100"}, "value": [1700000000, "9"]},
        {"metric": {"instance": "other:9100"}, "value": [1700000000, "50"]},
        {"metric": {}, "value": [1700000000, "junk"]},
    ]
    by_instance = oic.instant_by_instance(series)
    assert by_instance == {"gpu-node-1:9100": 7.0, "gpu-node-10:9100": 9.0, "other:9100": 50.0}
    # The prefix quirk is inherited from the original per-node queries:
    # `instance=~"gpu-node-1.*"` also matches gpu-node-10's series, so the
    # mean pools both. An unmatched node has no value.
    gpu_node_1 = re.compile(r"gpu-node-1.*")
    assert oic.average_matching(by_instance, gpu_node_1) == 8.0
    assert oic.average_matching(by_instance, re.compile(r"missing.*")) is None
    assert oic.instant_by_instance(None) == {}


def test_instance_alternation_is_promql_escaped():
    # The alternation is embedded in a double-quoted PromQL string literal,
    # where a lone backslash is an invalid escape sequence and fails the whole
    # query (run 25 returned null windowed GPU usage on every cluster this way).
    name = "ip-10-0-1-5.ec2.internal"
    pattern = oic._instance_alternation([name])
    assert pattern == "ip\\\\-10\\\\-0\\\\-1\\\\-5\\\\.ec2\\\\.internal.*"
    # PromQL unescaping (\\ -> \) must restore the re.escape pattern that the
    # Python-side matching uses, so both sides select the same series.
    assert pattern.replace("\\\\", "\\") == re.escape(name) + ".*"

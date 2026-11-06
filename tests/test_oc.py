"""Unit tests for the oc CLI wrapper (Oc)."""

from __future__ import annotations

import ocp_idle_check as oic


def test_cluster_wide_listings_carry_their_own_timeout(monkeypatch):
    # The cluster-wide listings can be large on busy clusters (run 25 saw
    # the pods and events listings time out at the 10 s point-call budget);
    # they must pass the listings' own, larger budget to Oc.run.  The nodes
    # JSON is small but metadata-fat, so it rides the same budget.
    seen: list[tuple[tuple[str, ...], float]] = []

    def fake_run(self, args, timeout=oic.OC_TIMEOUT):
        seen.append((tuple(args[:3]), timeout))
        return ""

    monkeypatch.setattr(oic.Oc, "run", fake_run)
    oc = oic.Oc()
    oc.pods_all()
    oc.replicasets_all()
    oc.events_all()
    oc.nodes()
    assert seen == [
        (("get", "pods", "-A"), oic.OC_LIST_TIMEOUT),
        (("get", "rs", "-A"), oic.OC_LIST_TIMEOUT),
        (("get", "events", "-A"), oic.OC_LIST_TIMEOUT),
        (("get", "nodes", "-o"), oic.OC_LIST_TIMEOUT),
    ]


def test_nodes_listing_failure_is_none_not_empty(monkeypatch):
    # A failed or malformed nodes listing is None (unknown inventory), not
    # [] (known-empty): GPU reads UNKNOWN on it and main leaves CPU series
    # unfiltered.  The failure is cached, like the other listings'.
    returns: list[str | None] = [None, "{not json"]

    def fake_run(self, args, timeout=oic.OC_TIMEOUT):
        return returns.pop(0)

    monkeypatch.setattr(oic.Oc, "run", fake_run)
    oc = oic.Oc()
    assert oc.nodes() is None
    assert oc.nodes() is None  # cached: the malformed retry never runs
    assert returns == ["{not json"]

"""Integration tests for P2 tools (observe-with-diff, snapshots, wait_for)."""
from __future__ import annotations


def test_observe_full_then_diff(client):
    full = client.get("/api/observe?window_index=0").get_json()
    assert full["format"] == "full"
    token = full["tree_token"]

    diff = client.get(f"/api/observe?window_index=0&since={token}").get_json()
    assert diff["format"] == "custom"
    assert diff["unchanged"] is True
    assert diff["changes"] == []


def test_observe_json_patch(client):
    full = client.get("/api/observe?window_index=0").get_json()
    token = full["tree_token"]
    diff = client.get(
        f"/api/observe?window_index=0&since={token}&format=json-patch"
    ).get_json()
    assert diff["format"] == "json-patch"


def test_observe_unknown_token_returns_full(client):
    diff = client.get(
        "/api/observe?window_index=0&since=tt:unknown"
    ).get_json()
    assert diff["format"] == "full"
    assert diff["base_token"] is None


def test_snapshot_lifecycle(client):
    s = client.post("/api/snapshot").get_json()
    assert s["ok"] is True
    sid = s["snapshot_id"]

    g = client.get(f"/api/snapshot/{sid}").get_json()
    assert g["ok"] is True
    assert g["snapshot_id"] == sid

    d = client.post("/api/snapshot/diff", json={"a": sid, "b": sid}).get_json()
    assert d["ok"] is True
    assert d["windows_added"] == []
    assert d["windows_removed"] == []

    dr = client.delete(f"/api/snapshot/{sid}").get_json()
    assert dr["ok"] is True
    assert dr["dropped"] is True


def test_snapshot_expired(client):
    g = client.get("/api/snapshot/snap:does_not_exist").get_json()
    assert g["ok"] is False
    assert g["error"]["code"] == "SnapshotExpired"


def test_wait_for_matches_immediately(client):
    r = client.post("/api/wait_for", json={
        "any_of": [{"type": "window_appears", "title_regex": "Notepad"}],
        "timeout_ms": 500,
    }).get_json()
    assert r["ok"] is True
    assert r["matched_index"] == 0


def test_wait_for_timeout(client):
    r = client.post("/api/wait_for", json={
        "any_of": [{"type": "window_appears", "title_regex": "NEVER"}],
        "timeout_ms": 200, "poll_ms": 80,
    }).get_json()
    assert r["ok"] is False
    assert r["error"]["code"] == "Timeout"
    assert r["polls"] >= 1


def test_click_and_observe(client):
    obs = client.get("/api/observe?window_index=0").get_json()
    token = obs["tree_token"]
    r = client.post("/api/element/click_and_observe", json={
        "window_index": 0,
        "selector": 'Window/MenuBar/MenuItem[name="File"]',
        "wait_after_ms": 0,
        "since": token,
    }).get_json()
    assert r["ok"] is True
    assert "observation" in r


# ─── P1 perf: changed_only + perf telemetry ──────────────────────────────────

def _mutate_editor(tree):
    for c in tree.children:
        if c.role == "Document":
            c.value = "changed content"
    return tree


def test_observe_changed_only_unchanged(client):
    client.get("/api/observe?window_index=0")            # warm the baseline
    r = client.get("/api/observe?window_index=0&changed_only=1").get_json()
    assert r["ok"] is True
    assert r["changed_only"] is True
    assert r["unchanged"] is True
    assert r["tree_hash"].startswith("sha1:")
    assert "tree" not in r and "changes" not in r        # tiny response


def test_observe_changed_only_returns_diff(client, observer):
    base = client.get("/api/observe?window_index=0").get_json()
    observer._adapter.tree_mutator = _mutate_editor
    r = client.get("/api/observe?window_index=0&changed_only=1").get_json()
    assert r["ok"] is True
    assert r["unchanged"] is False
    assert r["format"] == "custom"
    assert r["changes"]                                   # non-empty diff
    assert r["tree_hash"] != base["tree_hash"]
    assert "tree" not in r                                # diff, not full tree


def test_observe_changed_only_without_baseline_returns_full(client):
    r = client.get("/api/observe?window_index=0&changed_only=1").get_json()
    assert r["ok"] is True
    assert r["format"] == "full"
    assert "tree" in r
    assert r["perf"]["cache"] == "bypass"


def test_observe_perf_telemetry(client):
    r = client.get("/api/observe?window_index=0").get_json()
    perf = r["perf"]
    assert set(perf) == {"capture_ms", "node_count", "cache", "depth_used"}
    assert perf["cache"] == "miss"
    assert perf["node_count"] > 1
    assert perf["depth_used"] == 5


def test_structure_perf_telemetry_cache_hit(client):
    r1 = client.get("/api/structure?window_index=0").get_json()
    assert r1["perf"]["cache"] == "miss"
    r2 = client.get("/api/structure?window_index=0").get_json()
    assert r2["perf"]["cache"] == "hit"
    assert r2["perf"]["node_count"] == r1["perf"]["node_count"]


def test_structure_scoped_perf(client):
    client.get("/api/structure?window_index=0")           # warm cache
    r = client.get("/api/structure?window_index=0&scope=root.4").get_json()
    assert r["perf"]["cache"] == "hit"
    assert r["perf"]["depth_used"] == 5


def test_observe_since_perf(client):
    full = client.get("/api/observe?window_index=0").get_json()
    diff = client.get(
        f"/api/observe?window_index=0&since={full['tree_token']}"
    ).get_json()
    assert "perf" in diff
    assert diff["perf"]["cache"] in ("hit", "miss")

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

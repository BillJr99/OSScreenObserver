"""Integration tests for P1 tools via the REST surface."""
from __future__ import annotations


def test_list_windows(client):
    r = client.get("/api/windows").get_json()
    assert r["ok"] is True
    assert r["count"] == 3
    for w in r["windows"]:
        assert w["window_uid"].startswith("mock:")


def test_capabilities(client):
    r = client.get("/api/capabilities").get_json()
    assert r["ok"] is True
    assert r["supports"]["accessibility_tree"] is True
    assert "version" in r


def test_monitors(client):
    r = client.get("/api/monitors").get_json()
    assert r["ok"] is True
    assert "monitors" in r


def test_find_element_happy(client):
    r = client.get(
        '/api/find_element?window_index=0&selector=Window/MenuBar/MenuItem[name="Edit"]'
    ).get_json()
    assert r["ok"] is True
    assert r["element_id"]
    assert r["ambiguous_matches"] == 1


def test_find_element_ambiguous(client):
    r = client.get(
        "/api/find_element?window_index=0&selector=Window/MenuBar/MenuItem"
    ).get_json()
    assert r["ok"] is True
    assert r["ambiguous_matches"] >= 2


def test_find_element_not_found(client):
    r = client.get(
        '/api/find_element?window_index=0&selector=Window/Nope[name="X"]'
    ).get_json()
    assert r["ok"] is False
    assert r["error"]["code"] == "ElementNotFound"
    assert r["error"]["recoverable"] is True
    assert r["error"]["suggested_next_tool"] == "find_element"


def test_click_element_receipt(client):
    r = client.post("/api/element/click", json={
        "window_index": 0,
        "selector": 'Window/MenuBar/MenuItem[name="Edit"]',
    }).get_json()
    assert r["ok"] is True
    assert r["action"] == "click_element"
    assert "before" in r and "after" in r
    assert "tree_hash" in r["before"]
    assert "duration_ms" in r
    assert r["dry_run"] is False


def test_click_element_dry_run(client):
    r = client.post("/api/element/click", json={
        "window_index": 0,
        "selector": 'Window/MenuBar/MenuItem[name="Edit"]',
        "dry_run": True,
    }).get_json()
    assert r["ok"] is True
    assert r["dry_run"] is True
    assert r["changed"] is False


def test_window_uid_resolution(client):
    ws = client.get("/api/windows").get_json()
    uid = ws["windows"][1]["window_uid"]
    r = client.get(f"/api/find_element?window_uid={uid}&"
                   f"selector=Window").get_json()
    assert r["ok"] is True
    assert r["window_uid"] == uid


def test_healthz(client):
    r = client.get("/api/healthz").get_json()
    assert r["ok"] is True
    assert "uptime_s" in r and "adapter" in r

"""Integration tests for P6 tools (extra verbs, telemetry)."""
from __future__ import annotations


def test_right_click_element_returns_receipt(client):
    ws = client.get("/api/windows").get_json()
    uid = ws["windows"][0]["window_uid"]
    r = client.post("/api/element/right_click", json={
        "window_uid": uid,
        "selector": 'Window/MenuBar/MenuItem[name="File"]',
    }).get_json()
    assert r["ok"] is True
    assert "before" in r and "after" in r


def test_double_click_element(client):
    ws = client.get("/api/windows").get_json()
    uid = ws["windows"][0]["window_uid"]
    r = client.post("/api/element/double_click", json={
        "window_uid": uid,
        "selector": 'Window/MenuBar/MenuItem[name="Edit"]',
    }).get_json()
    assert r["ok"] is True


def test_drag_bad_request(client):
    r = client.post("/api/drag", json={"from": {}, "to": {}}).get_json()
    assert r["ok"] is False
    assert r["error"]["code"] == "BadRequest"


def test_metrics_exposes_step_count(client):
    client.get("/api/windows")
    m = client.get("/api/metrics")
    body = m.get_data(as_text=True)
    assert "oso_step_count" in body
    assert m.content_type.startswith("text/plain")

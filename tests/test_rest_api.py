"""Comprehensive pytest tests for the Flask REST API.

All tests use the `client` fixture from conftest.py which wires up a
mock ScreenObserver so no real desktop or display is required.
"""
from __future__ import annotations


# ── /api/windows ────────────────────────────────────────────────────────────


def test_get_windows_returns_200(client):
    r = client.get("/api/windows")
    assert r.status_code == 200


def test_get_windows_json_structure(client):
    data = client.get("/api/windows").get_json()
    assert data["ok"] is True
    assert "windows" in data
    assert "count" in data
    assert isinstance(data["windows"], list)
    assert data["count"] == len(data["windows"])


def test_get_windows_mock_flag(client):
    data = client.get("/api/windows").get_json()
    # Mock adapter always sets is_mock=True
    assert data.get("is_mock") is True


def test_get_windows_entries_have_required_fields(client):
    data = client.get("/api/windows").get_json()
    for w in data["windows"]:
        assert "title" in w
        assert "window_uid" in w
        assert "pid" in w
        assert "bounds" in w
        bounds = w["bounds"]
        for key in ("x", "y", "width", "height"):
            assert key in bounds


# ── /api/healthz ────────────────────────────────────────────────────────────


def test_healthz_returns_200(client):
    r = client.get("/api/healthz")
    assert r.status_code == 200


def test_healthz_json_structure(client):
    data = client.get("/api/healthz").get_json()
    assert data["ok"] is True
    assert "uptime_s" in data
    assert "adapter" in data
    assert "step_count" in data


def test_healthz_adapter_is_mock(client):
    data = client.get("/api/healthz").get_json()
    # The mock adapter name contains "Mock"
    assert "Mock" in data["adapter"]


# ── /api/structure ──────────────────────────────────────────────────────────


def test_structure_default_window(client):
    data = client.get("/api/structure").get_json()
    assert data["ok"] is True
    assert "tree" in data
    assert "window" in data
    assert "element_count" in data


def test_structure_with_window_index(client):
    data = client.get("/api/structure?window_index=0").get_json()
    assert data["ok"] is True
    assert data["element_count"] >= 1


def test_structure_invalid_window_index(client):
    # The mock adapter falls back to the focused window when the index is out
    # of range, so the call still succeeds rather than returning an error.
    r = client.get("/api/structure?window_index=9999")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── /api/description ────────────────────────────────────────────────────────


def test_description_default_window(client):
    data = client.get("/api/description").get_json()
    assert data["ok"] is True
    assert "description" in data


def test_description_with_window_index(client):
    data = client.get("/api/description?window_index=0").get_json()
    assert data["ok"] is True
    assert isinstance(data["description"], str)


# ── /api/sketch ─────────────────────────────────────────────────────────────


def test_sketch_default_window(client):
    data = client.get("/api/sketch").get_json()
    assert "sketch" in data
    assert isinstance(data["sketch"], str)
    assert len(data["sketch"]) > 0


def test_sketch_with_window_index(client):
    data = client.get("/api/sketch?window_index=0").get_json()
    assert "sketch" in data
    assert "window" in data
    assert "grid_width" in data
    assert "grid_height" in data


def test_sketch_custom_grid_size(client):
    data = client.get("/api/sketch?window_index=0&grid_width=40&grid_height=20").get_json()
    assert "sketch" in data
    assert data["grid_width"] == 40
    assert data["grid_height"] == 20


# ── /api/screenshot ─────────────────────────────────────────────────────────


def test_screenshot_returns_200(client):
    r = client.get("/api/screenshot")
    assert r.status_code == 200


def test_screenshot_json_structure(client):
    data = client.get("/api/screenshot").get_json()
    assert "data" in data
    assert data.get("format") == "png"
    assert data.get("encoding") == "base64"
    assert isinstance(data["data"], str)
    assert len(data["data"]) > 0


def test_screenshot_with_window_index(client):
    data = client.get("/api/screenshot?window_index=0").get_json()
    assert "data" in data
    assert "window" in data


# ── /api/action (POST) ──────────────────────────────────────────────────────


def test_action_click_at(client):
    data = client.post(
        "/api/action",
        json={"action": "click_at", "x": 100, "y": 200},
    ).get_json()
    assert data["ok"] is True


def test_action_type(client):
    data = client.post(
        "/api/action",
        json={"action": "type", "value": "hello"},
    ).get_json()
    assert data["ok"] is True


def test_action_key(client):
    data = client.post(
        "/api/action",
        json={"action": "key", "value": "ctrl+c"},
    ).get_json()
    assert data["ok"] is True


def test_action_unknown_returns_400(client):
    r = client.post(
        "/api/action",
        json={"action": "does_not_exist"},
    )
    assert r.status_code == 400
    data = r.get_json()
    assert data.get("ok") is False


def test_action_missing_body_defaults(client):
    # Empty body — action="" should return 400
    r = client.post("/api/action", json={})
    assert r.status_code == 400


# ── /api/capabilities ──────────────────────────────────────────────────────


def test_capabilities_returns_200(client):
    r = client.get("/api/capabilities")
    assert r.status_code == 200


def test_capabilities_json_structure(client):
    data = client.get("/api/capabilities").get_json()
    assert data["ok"] is True
    assert "supports" in data
    assert "version" in data


def test_capabilities_supports_accessibility_tree(client):
    data = client.get("/api/capabilities").get_json()
    assert data["supports"].get("accessibility_tree") is True


# ── /api/monitors ──────────────────────────────────────────────────────────


def test_monitors_returns_list(client):
    data = client.get("/api/monitors").get_json()
    assert data["ok"] is True
    assert "monitors" in data
    assert isinstance(data["monitors"], list)


# ── /api/tools ─────────────────────────────────────────────────────────────


def test_tools_list(client):
    data = client.get("/api/tools").get_json()
    assert data["ok"] is True
    assert "tools" in data
    assert isinstance(data["tools"], list)
    # Core tools must be registered
    for tool in ("list_windows", "get_window_structure", "click_at"):
        assert tool in data["tools"]


# ── /api/tool/<name> (generic console) ─────────────────────────────────────


def test_tool_run_list_windows(client):
    data = client.post("/api/tool/list_windows", json={}).get_json()
    assert data["ok"] is True
    assert "windows" in data


def test_tool_run_get_windows_via_get(client):
    data = client.get("/api/tool/list_windows").get_json()
    assert data["ok"] is True


def test_tool_run_unknown_tool_returns_error(client):
    data = client.post("/api/tool/nonexistent_tool_xyz", json={}).get_json()
    assert data.get("ok") is False


# ── /api/metrics ────────────────────────────────────────────────────────────


def test_metrics_returns_prometheus_text(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.content_type
    text = r.data.decode()
    assert "oso_step_count" in text
    assert "oso_uptime_seconds" in text


# ── Root UI ─────────────────────────────────────────────────────────────────


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"OS Screen Observer" in r.data


# ── /api/visible_areas ──────────────────────────────────────────────────────


def test_visible_areas_requires_window_index(client):
    r = client.get("/api/visible_areas")
    # No window_index → error (400 or JSON error)
    data = r.get_json()
    assert data.get("error") is not None or r.status_code >= 400


def test_visible_areas_with_window_index(client):
    data = client.get("/api/visible_areas?window_index=0").get_json()
    assert "visible_regions" in data
    assert "window" in data
    assert isinstance(data["visible_regions"], list)


# ── /api/find_element ───────────────────────────────────────────────────────


def test_find_element_happy_path(client):
    r = client.get(
        '/api/find_element?window_index=0&selector=Window/MenuBar/MenuItem[name="Edit"]'
    ).get_json()
    assert r["ok"] is True
    assert "element_id" in r


def test_find_element_not_found(client):
    r = client.get(
        '/api/find_element?window_index=0&selector=Window/Bogus[name="NoSuchElement"]'
    ).get_json()
    assert r["ok"] is False
    assert r["error"]["code"] == "ElementNotFound"


# ── /api/observe ────────────────────────────────────────────────────────────


def test_observe_full_snapshot(client):
    data = client.get("/api/observe?window_index=0").get_json()
    assert data["ok"] is True
    assert data["format"] == "full"
    assert "tree_token" in data


def test_observe_diff_unchanged(client):
    full = client.get("/api/observe?window_index=0").get_json()
    token = full["tree_token"]
    diff = client.get(f"/api/observe?window_index=0&since={token}").get_json()
    assert diff["format"] == "custom"
    assert diff["unchanged"] is True


# ── /api/snapshot lifecycle ──────────────────────────────────────────────────


def test_snapshot_create_get_delete(client):
    # Create
    s = client.post("/api/snapshot").get_json()
    assert s["ok"] is True
    sid = s["snapshot_id"]

    # Retrieve
    g = client.get(f"/api/snapshot/{sid}").get_json()
    assert g["ok"] is True
    assert g["snapshot_id"] == sid

    # Delete
    d = client.delete(f"/api/snapshot/{sid}").get_json()
    assert d["ok"] is True
    assert d["dropped"] is True


def test_snapshot_missing_returns_error(client):
    g = client.get("/api/snapshot/snap:does_not_exist_xyz").get_json()
    assert g["ok"] is False
    assert g["error"]["code"] == "SnapshotExpired"


# ── /api/budget_status ──────────────────────────────────────────────────────


def test_budget_status(client):
    data = client.get("/api/budget_status").get_json()
    assert data["ok"] is True


# ── /api/redaction_status ───────────────────────────────────────────────────


def test_redaction_status(client):
    data = client.get("/api/redaction_status").get_json()
    assert data["ok"] is True

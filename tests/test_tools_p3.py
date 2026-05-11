"""Integration tests for P3 tools (filtering, crops, description)."""
from __future__ import annotations


def _count_nodes(n):
    if n is None:
        return 0
    return 1 + sum(_count_nodes(c) for c in (n.get("children") or []))


def test_structure_unfiltered(client):
    r = client.get("/api/structure?window_index=0").get_json()
    assert r["ok"] is True
    assert r["element_count"] >= 1
    assert "tree_token" in r


def test_structure_role_filter(client):
    r = client.get(
        "/api/structure?window_index=0&roles=MenuItem&prune_empty=true"
    ).get_json()
    # All surviving leaf nodes should be MenuItems; ancestors are kept for path.
    assert r["ok"] is True
    nodes = _count_nodes(r["tree"])
    assert 0 < nodes < r["element_count"]


def test_structure_pagination(client):
    r = client.get("/api/structure?window_index=0&max_nodes=5").get_json()
    assert r["ok"] is True
    assert r["truncated"] is True
    assert r["next_cursor"] is not None


def test_screenshot_cropped(client):
    r = client.get(
        "/api/screenshot/cropped?window_index=0&max_width=200"
    ).get_json()
    assert r["ok"] is True
    assert "data" in r


def test_description_combined(client):
    r = client.get("/api/description?window_index=0&max_tokens=15").get_json()
    assert r["ok"] is True
    assert r["effective_mode"] == "combined"
    assert r["truncated"] is True


def test_description_focus_element(client):
    full = client.get("/api/structure?window_index=0").get_json()
    # First child id
    child_id = full["tree"]["children"][0]["id"]
    r = client.get(
        f"/api/description?window_index=0&mode=accessibility&focus_element={child_id}"
    ).get_json()
    assert r["ok"] is True

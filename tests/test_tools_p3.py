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


# ─── P1 perf: bounded depth + subtree scope ──────────────────────────────────

from observer import Bounds, UIElement  # noqa: E402


def _graft_deep_chain(tree, levels=9):
    """Attach a linear chain of Group nodes below the tree root."""
    node = tree
    for i in range(levels):
        child = UIElement(f"{node.element_id}.{len(node.children)}",
                          f"Deep{i}", "Group", bounds=Bounds(0, 0, 5, 5))
        node.children.append(child)
        node = child
    return tree


def _max_depth_of(node, depth=0):
    kids = node.get("children") or []
    if not kids:
        return depth
    return max(_max_depth_of(c, depth + 1) for c in kids)


def _find_truncated(node):
    if node.get("truncated"):
        return node
    for c in node.get("children") or []:
        f = _find_truncated(c)
        if f is not None:
            return f
    return None


def test_structure_default_depth_truncates(client, observer):
    observer._adapter.tree_mutator = _graft_deep_chain
    r = client.get("/api/structure?window_index=0").get_json()
    assert r["ok"] is True
    assert r["depth_used"] == 5              # tree.default_depth
    assert r["depth_truncated"] is True
    assert _max_depth_of(r["tree"]) == 5
    marker = _find_truncated(r["tree"])
    assert marker is not None
    assert marker["child_count"] == 1
    assert marker["children"] == []


def test_structure_explicit_depth_capped(client, observer):
    observer._adapter.tree_mutator = _graft_deep_chain
    r = client.get("/api/structure?window_index=0&depth=7").get_json()
    assert r["depth_used"] == 7
    assert _max_depth_of(r["tree"]) == 7

    r = client.get("/api/structure?window_index=0&depth=20").get_json()
    assert r["depth_used"] == 8              # clamped to tree.max_depth


def test_structure_shallow_tree_not_marked(client):
    r = client.get("/api/structure?window_index=0").get_json()
    assert r["depth_truncated"] is False     # mock tree is only 2 deep
    assert _find_truncated(r["tree"]) is None


def test_structure_scope_subtree(client):
    r = client.get("/api/structure?window_index=0&scope=root.4").get_json()
    assert r["ok"] is True
    assert r["scope"] == "root.4"
    assert r["tree"]["id"] == "root.4"
    assert r["tree"]["role"] == "StatusBar"
    assert len(r["tree"]["children"]) == 4
    assert r["tree_token"] is None           # scoped ≠ diff baseline


def test_structure_scope_with_depth(client, observer):
    observer._adapter.tree_mutator = _graft_deep_chain
    deep_root = "root.7"                     # chain head appended at index 7
    r = client.get(
        f"/api/structure?window_index=0&scope={deep_root}&depth=2"
    ).get_json()
    assert r["ok"] is True
    assert r["tree"]["id"] == deep_root
    assert _max_depth_of(r["tree"]) <= 2


def test_structure_scope_not_found(client):
    r = client.get("/api/structure?window_index=0&scope=root.99").get_json()
    assert r["ok"] is False
    assert r["error"]["code"] == "ElementNotFound"


def test_observe_full_is_depth_bounded(client, observer):
    observer._adapter.tree_mutator = _graft_deep_chain
    r = client.get("/api/observe?window_index=0").get_json()
    assert r["format"] == "full"
    assert r["depth_used"] == 5
    assert r["depth_truncated"] is True
    assert _max_depth_of(r["tree"]) == 5

    # Diffs still compare the FULL captures (depth only bounds the payload).
    diff = client.get(
        f"/api/observe?window_index=0&since={r['tree_token']}"
    ).get_json()
    assert diff["unchanged"] is True


def test_mock_adapter_subtree_walk(observer):
    adapter = observer._adapter
    sub = adapter.get_element_subtree(None, "root.0", max_depth=0)
    assert sub.element_id == "root.0"
    assert sub.role == "MenuBar"
    assert sub.children == []                # depth 0 prunes children

    sub = adapter.get_element_subtree(None, "root.0", max_depth=1)
    assert len(sub.children) == 5


def test_observer_subtree_served_from_cache(observer):
    w = observer.list_windows()[0]
    observer.get_element_tree(w.handle, window_uid=w.window_uid)
    assert observer._adapter.capture_count == 1
    sub = observer.get_element_subtree(w.handle, "root.4", max_depth=2,
                                       window_uid=w.window_uid)
    assert sub is not None and sub.element_id == "root.4"
    assert observer._adapter.capture_count == 1    # extracted from cache

    # Extraction copies — the cached tree keeps its children intact.
    shallow = observer.get_element_subtree(w.handle, "root.4", max_depth=0,
                                           window_uid=w.window_uid)
    assert shallow.children == []
    full = observer.get_element_tree(w.handle, window_uid=w.window_uid)
    sb = next(c for c in full.children if c.element_id == "root.4")
    assert len(sb.children) == 4

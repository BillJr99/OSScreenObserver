"""Tests for the per-window tree cache (P1 perception performance)."""
from __future__ import annotations

import time

import pytest

import tools as _tools
from observer import Bounds, UIElement
from session import get_session
from tree_cache import TreeCache


# ─── Helpers / fixtures ───────────────────────────────────────────────────────

def _make_tree(name: str = "T") -> UIElement:
    return UIElement("root", name, "Window", bounds=Bounds(0, 0, 10, 10))


def _entry_kwargs(name: str = "T") -> dict:
    t = _make_tree(name)
    return dict(tree=t, serialized=t.to_dict(), tree_hash=f"sha1:{name}",
                max_depth=8, capture_ms=1, node_count=1, named_node_count=1)


@pytest.fixture()
def ctx(config, observer, renderer, describer):
    return _tools.ToolContext(observer=observer, renderer=renderer,
                              describer=describer, config=config)


def _first_window(observer):
    w = observer.list_windows()[0]
    return w.handle, w.window_uid


# ─── TreeCache unit tests ─────────────────────────────────────────────────────

def test_cache_put_get_hit():
    c = TreeCache(ttl_s=60)
    c.put("w1", **_entry_kwargs("A"))
    e = c.get("w1")
    assert e is not None
    assert e.tree_hash == "sha1:A"
    assert c.get("nope") is None


def test_cache_ttl_expiry():
    c = TreeCache(ttl_s=60)
    entry = c.put("w1", **_entry_kwargs())
    entry.captured_at -= 120          # age the entry past the TTL
    assert c.get("w1") is None        # expired entries are dropped
    assert "w1" not in c


def test_cache_per_call_ttl_override():
    c = TreeCache(ttl_s=0.0)
    entry = c.put("w1", **_entry_kwargs())
    entry.captured_at -= 1
    assert c.get("w1", ttl_s=60) is not None


def test_cache_peek_ignores_ttl():
    c = TreeCache(ttl_s=60)
    entry = c.put("w1", **_entry_kwargs())
    entry.captured_at -= 120
    assert c.peek("w1") is not None   # baseline lookups ignore TTL


def test_cache_invalidate():
    c = TreeCache(ttl_s=60)
    c.put("w1", **_entry_kwargs())
    assert c.invalidate("w1") is True
    assert c.get("w1") is None
    assert c.invalidate("w1") is False


def test_cache_lru_eviction():
    c = TreeCache(ttl_s=60, max_windows=3)
    for i in range(3):
        c.put(f"w{i}", **_entry_kwargs())
    c.get("w0")                       # touch w0 → w1 becomes LRU
    c.put("w3", **_entry_kwargs())
    assert "w0" in c and "w2" in c and "w3" in c
    assert "w1" not in c
    assert len(c) == 3


def test_cache_stats_survive_invalidation():
    c = TreeCache(ttl_s=60)
    c.put("w1", **_entry_kwargs())
    c.invalidate("w1")
    stats = c.stats()
    assert "w1" in stats
    assert stats["w1"]["node_count"] == 1
    assert stats["w1"]["named_node_count"] == 1


# ─── Observer-level caching ───────────────────────────────────────────────────

def test_observer_cache_hit_avoids_walk(observer):
    hwnd, uid = _first_window(observer)
    adapter = observer._adapter
    t1 = observer.get_element_tree(hwnd, window_uid=uid)
    assert adapter.capture_count == 1
    t2 = observer.get_element_tree(hwnd, window_uid=uid)
    assert adapter.capture_count == 1          # served from cache
    assert t2 is t1


def test_observer_cache_meta_hit_miss_bypass(observer):
    hwnd, uid = _first_window(observer)
    _, meta = observer.get_element_tree_with_meta(hwnd, window_uid=uid)
    assert meta["cache"] == "miss"
    assert meta["node_count"] > 1
    _, meta = observer.get_element_tree_with_meta(hwnd, window_uid=uid)
    assert meta["cache"] == "hit"
    _, meta = observer.get_element_tree_with_meta(hwnd, window_uid=uid,
                                                  use_cache=False)
    assert meta["cache"] == "bypass"
    assert observer._adapter.capture_count == 2


def test_observer_cache_ttl_expiry_rewalks(observer):
    hwnd, uid = _first_window(observer)
    observer.get_element_tree(hwnd, window_uid=uid)
    entry = get_session().tree_cache.peek(uid)
    entry.captured_at = time.time() - 100      # simulate TTL expiry
    observer.get_element_tree(hwnd, window_uid=uid)
    assert observer._adapter.capture_count == 2


def test_observer_no_uid_is_never_cached(observer):
    hwnd, _ = _first_window(observer)
    observer.get_element_tree(hwnd)
    observer.get_element_tree(hwnd)
    assert observer._adapter.capture_count == 2
    assert len(get_session().tree_cache) == 0


def test_observer_bypass_still_refreshes_cache(observer):
    hwnd, uid = _first_window(observer)
    observer.get_element_tree(hwnd, window_uid=uid, use_cache=False)
    assert get_session().tree_cache.peek(uid) is not None


# ─── Invalidation through tools.dispatch ─────────────────────────────────────

def test_input_tool_invalidates_target_window(ctx, observer):
    _, uid = _first_window(observer)
    _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    assert uid in get_session().tree_cache

    r = _tools.dispatch(ctx, "click_element", {
        "window_index": 0,
        "selector": 'Window/MenuBar/MenuItem[name="File"]',
    })
    assert r["ok"] is True
    assert uid not in get_session().tree_cache


def test_legacy_input_tool_invalidates_all(ctx, observer):
    _, uid = _first_window(observer)
    _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    assert uid in get_session().tree_cache

    _tools.dispatch(ctx, "type_text", {"text": "hi"})   # no window binding
    assert len(get_session().tree_cache) == 0


def test_read_only_tool_does_not_invalidate(ctx, observer):
    _, uid = _first_window(observer)
    _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    before = observer._adapter.capture_count
    _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    assert observer._adapter.capture_count == before    # cache hit
    assert uid in get_session().tree_cache


def test_receipt_after_state_bypasses_cache(ctx, observer):
    """A mutation applied after the cache is warmed must be visible in the
    ActionReceipt's after-state (post-action reads bypass the cache)."""
    adapter = observer._adapter
    _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})

    def _rename(tree):
        tree.name = "MUTATED TITLE"
        return tree

    adapter.tree_mutator = _rename
    r = _tools.dispatch(ctx, "click_element", {
        "window_index": 0,
        "selector": 'Window/MenuBar/MenuItem[name="File"]',
    })
    assert r["ok"] is True
    # before came from the warm cache; after re-walked and saw the mutation.
    assert r["before"]["tree_hash"] != r["after"]["tree_hash"]
    assert r["changed"] is True


def test_mock_adapter_mutation_hook(observer):
    adapter = observer._adapter
    adapter.tree_mutator = lambda t: UIElement(
        "root", "Replaced", "Window", bounds=Bounds(0, 0, 1, 1))
    tree = observer.get_element_tree(None)
    assert tree.name == "Replaced"
    assert adapter.capture_count == 1


# ─── P1: degradation signal (sparse accessibility trees) ─────────────────────

def _sparse_tree(tree):
    """Simulate an accessibility-dark window: one named child only."""
    return UIElement(
        "root", "Game Window", "Window", bounds=Bounds(0, 0, 800, 600),
        children=[
            UIElement("root.0", "Canvas", "Pane", bounds=Bounds(0, 0, 800, 600)),
            UIElement("root.1", "", "Pane", bounds=Bounds(0, 0, 10, 10)),
        ])


def test_sparse_tree_attaches_degraded(ctx, observer):
    observer._adapter.tree_mutator = _sparse_tree
    r = _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    assert r["ok"] is True
    d = r["degraded"]
    assert d["reason"] == "sparse_accessibility_tree"
    assert d["named_node_count"] == 1          # unnamed nodes don't count
    assert d["suggested_fallbacks"] == ["get_ocr", "get_screen_description"]


def test_rich_tree_has_no_degraded(ctx):
    r = _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    assert r["ok"] is True
    assert "degraded" not in r


def test_sparse_threshold_configurable(ctx, observer, config):
    config.setdefault("tree", {})["sparse_threshold"] = 100
    r = _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    assert r["degraded"]["threshold"] == 100   # even the rich mock tree trips


def test_capabilities_reports_tree_stats(ctx, observer):
    _, uid = _first_window(observer)
    caps0 = _tools.dispatch(ctx, "get_capabilities", {})
    assert caps0["tree_stats"] == {}           # nothing captured yet

    _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    caps = _tools.dispatch(ctx, "get_capabilities", {})
    stats = caps["tree_stats"][uid]
    assert stats["node_count"] > 1
    assert stats["named_node_count"] >= 5
    assert "capture_ms" in stats and "captured_at" in stats


def test_capabilities_stats_survive_input_invalidation(ctx, observer):
    _, uid = _first_window(observer)
    _tools.dispatch(ctx, "get_window_structure", {"window_index": 0})
    _tools.dispatch(ctx, "type_text", {"text": "x"})     # invalidates cache
    caps = _tools.dispatch(ctx, "get_capabilities", {})
    assert uid in caps["tree_stats"]

"""Unit tests for the WindowsAdapter raw-UIA walker (P1 perf).

Real COM/UIA is unavailable off-Windows, so these tests inject fake
comtypes/pywinauto/pywin32 modules via sys.modules and exercise both the
CacheRequest bulk-fetch path and the per-property fallback path.
Manual verification on real Windows is still required.
"""
from __future__ import annotations

import sys
import types

import pytest

from observer import WindowsAdapter


# ─── Fake COM / UIA infrastructure ───────────────────────────────────────────

_NAME, _CTRL_TYPE = 30005, 30003
_BOUNDING_RECT = 30001


class FakeRect:
    def __init__(self, left=0, top=0, right=10, bottom=10):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class FakeElementList:
    def __init__(self, items):
        self._items = list(items)

    @property
    def Length(self):
        return len(self._items)

    def GetElement(self, i):
        return self._items[i]


class FakeUIAElement:
    """IUIAutomationElement stand-in with call counters."""

    def __init__(self, name, ctrl_type=50000, children=(),
                 fail_build_cache=False):
        self._props = {_NAME: name, _CTRL_TYPE: ctrl_type}
        self._children = list(children)
        self.fail_build_cache = fail_build_cache
        self.current_calls = 0     # GetCurrentPropertyValue round trips
        self.cached_calls = 0
        self.find_all_calls = 0
        self.build_cache_calls = 0
        self.fetched_via_cache = False

    # Per-property (cross-process on real Windows).
    def GetCurrentPropertyValue(self, pid):
        self.current_calls += 1
        return self._props.get(pid)

    def GetCachedPropertyValue(self, pid):
        self.cached_calls += 1
        if pid == _BOUNDING_RECT:
            return [1.0, 2.0, 3.0, 4.0]
        return self._props.get(pid)

    @property
    def CurrentBoundingRectangle(self):
        self.current_calls += 1
        return FakeRect()

    @property
    def CachedBoundingRectangle(self):
        raise RuntimeError("not cached — use GetCachedPropertyValue")

    def FindAll(self, scope, cond):
        self.find_all_calls += 1
        return FakeElementList(self._children)

    def FindAllBuildCache(self, scope, cond, cache_request):
        self.build_cache_calls += 1
        if self.fail_build_cache:
            raise RuntimeError("BuildCache unsupported here")
        assert cache_request is not None
        for c in self._children:
            c.fetched_via_cache = True
        return FakeElementList(self._children)


class FakeCacheRequest:
    def __init__(self):
        self.properties = []

    def AddProperty(self, pid):
        self.properties.append(pid)


class FakeRawUIA:
    def __init__(self, root, cache_request_fails=False):
        self._root = root
        self.cache_request_fails = cache_request_fails
        self.cache_requests_built = 0

    def ElementFromHandle(self, hwnd):
        return self._root

    def CreateTrueCondition(self):
        return object()

    def CreateCacheRequest(self):
        if self.cache_request_fails:
            raise RuntimeError("CacheRequest not supported")
        self.cache_requests_built += 1
        return FakeCacheRequest()


class FakeApplication:
    """pywinauto.Application stand-in that records connect() attempts."""
    connect_calls = 0

    def __init__(self, backend=None):
        pass

    def connect(self, handle=None):
        FakeApplication.connect_calls += 1
        raise RuntimeError("no real UIA here")   # → pw_tree stays None


def _make_fake_tree():
    """root → [child0(Button) → [grand0(Text)], child1(Edit)]"""
    grand0 = FakeUIAElement("Grand", 50020)
    child0 = FakeUIAElement("Child A", 50000, children=[grand0])
    child1 = FakeUIAElement("Child B", 50004)
    root = FakeUIAElement("Root Window", 50032, children=[child0, child1])
    return root, child0, child1, grand0


@pytest.fixture()
def fake_win(monkeypatch):
    """Install fake win32/pywinauto modules; returns a factory that builds a
    WindowsAdapter wired to a fake UIA tree."""
    win32gui = types.ModuleType("win32gui")
    win32gui.GetForegroundWindow = lambda: 1
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process",
                        types.ModuleType("win32process"))
    monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil"))

    FakeApplication.connect_calls = 0
    pywinauto = types.ModuleType("pywinauto")
    pywinauto.Application = FakeApplication
    monkeypatch.setitem(sys.modules, "pywinauto", pywinauto)

    uia_defines = types.ModuleType("pywinauto.uia_defines")
    pywinauto.uia_defines = uia_defines
    monkeypatch.setitem(sys.modules, "pywinauto.uia_defines", uia_defines)

    def _build(config=None, cache_request_fails=False):
        root, *rest = _make_fake_tree()
        raw = FakeRawUIA(root, cache_request_fails=cache_request_fails)

        class FakeIUIA:
            def __init__(self):
                self.iuia = raw

        uia_defines.IUIA = FakeIUIA
        adapter = WindowsAdapter(config or {"tree": {"max_depth": 8}})
        return adapter, raw, root, rest

    return _build


# ─── CacheRequest bulk-fetch path ────────────────────────────────────────────

def test_uia_walk_uses_cache_request(fake_win):
    adapter, raw, root, (child0, child1, grand0) = fake_win()
    tree = adapter._uia_walk(1)

    assert tree is not None
    assert tree.name == "Root Window"
    assert [c.name for c in tree.children] == ["Child A", "Child B"]
    assert tree.children[0].children[0].name == "Grand"
    assert tree.children[0].children[0].element_id == "root.0.0"

    assert raw.cache_requests_built == 1
    # Children were fetched with FindAllBuildCache, not FindAll.
    assert root.build_cache_calls == 1
    assert root.find_all_calls == 0
    # Cached elements were read via GetCachedPropertyValue — zero per-node
    # GetCurrentPropertyValue round trips.
    for el in (child0, child1, grand0):
        assert el.fetched_via_cache is True
        assert el.current_calls == 0
        assert el.cached_calls > 0
    # Cached bounds came from the BoundingRectangle SAFEARRAY fallback.
    assert tree.children[0].bounds.to_dict() == {
        "x": 1, "y": 2, "width": 3, "height": 4}


def test_cache_request_covers_walked_properties(fake_win):
    adapter, raw, _, _ = fake_win()
    cr = adapter._uia_cache_request(raw)
    assert _NAME in cr.properties and _CTRL_TYPE in cr.properties
    assert _BOUNDING_RECT in cr.properties
    assert len(cr.properties) >= 12


# ─── Fallback paths ──────────────────────────────────────────────────────────

def test_uia_walk_falls_back_when_cache_request_fails(fake_win):
    adapter, raw, root, (child0, child1, grand0) = fake_win(
        cache_request_fails=True)
    tree = adapter._uia_walk(1)

    assert tree is not None
    assert [c.name for c in tree.children] == ["Child A", "Child B"]
    # No BuildCache attempted; classic FindAll + per-property fetches.
    assert root.build_cache_calls == 0
    assert root.find_all_calls == 1
    for el in (child0, child1):
        assert el.fetched_via_cache is False
        assert el.current_calls > 0


def test_uia_walk_falls_back_per_node_when_build_cache_raises(fake_win):
    adapter, raw, root, (child0, child1, grand0) = fake_win()
    child0.fail_build_cache = True     # BuildCache breaks below child0
    tree = adapter._uia_walk(1)

    assert tree is not None
    # child0's children still walked, via the FindAll fallback…
    assert child0.find_all_calls == 1
    assert tree.children[0].children[0].name == "Grand"
    # …and grand0 was therefore read per-property, not from a cache.
    assert grand0.fetched_via_cache is False
    assert grand0.current_calls > 0


# ─── tree.strategy = uia_only ────────────────────────────────────────────────

def test_uia_only_skips_pywinauto_walk(fake_win):
    adapter, _, _, _ = fake_win(config={
        "tree": {"max_depth": 8, "strategy": "uia_only"}})
    tree = adapter.get_element_tree(1)
    assert tree is not None
    assert tree.name == "Root Window"
    assert FakeApplication.connect_calls == 0


def test_merged_strategy_still_attempts_pywinauto(fake_win):
    adapter, _, _, _ = fake_win(config={
        "tree": {"max_depth": 8, "strategy": "merged"}})
    tree = adapter.get_element_tree(1)
    assert tree is not None                       # uia tree survives pw failure
    assert FakeApplication.connect_calls == 1


# ─── Subtree navigation (commit 2 path, exercised against fake COM) ─────────

def test_get_element_subtree_navigates_indices(fake_win):
    adapter, _, root, (child0, child1, grand0) = fake_win()
    sub = adapter.get_element_subtree(1, "root.0", max_depth=8)
    assert sub is not None
    assert sub.element_id == "root.0"
    assert sub.name == "Child A"
    assert sub.children[0].name == "Grand"
    # Only the requested branch was walked — child1 untouched.
    assert child1.current_calls == 0 and child1.cached_calls == 0

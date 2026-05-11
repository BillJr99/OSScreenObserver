"""Tests for hashing."""
from __future__ import annotations

from hashing import focused_selector, tree_hash, windows_hash
from observer import Bounds, UIElement, WindowInfo


def test_tree_hash_stable():
    a = UIElement("r", "x", "Window", bounds=Bounds(0, 0, 1, 1))
    b = UIElement("r2", "x", "Window", bounds=Bounds(0, 0, 1, 1))  # different id
    # Same role/name/value/bounds/enabled → same hash (id excluded by design).
    assert tree_hash(a) == tree_hash(b)


def test_tree_hash_focused_excluded():
    a = UIElement("r", "x", "Window", bounds=Bounds(0, 0, 1, 1), focused=True)
    b = UIElement("r", "x", "Window", bounds=Bounds(0, 0, 1, 1), focused=False)
    assert tree_hash(a) == tree_hash(b)


def test_tree_hash_bounds_distinguishes():
    a = UIElement("r", "x", "Window", bounds=Bounds(0, 0, 1, 1))
    b = UIElement("r", "x", "Window", bounds=Bounds(0, 0, 2, 1))
    assert tree_hash(a) != tree_hash(b)


def test_focused_selector_root():
    root = UIElement("r", "Win", "Window", bounds=Bounds(0, 0, 1, 1),
                     focused=True)
    s = focused_selector(root)
    assert s.startswith("Window")


def test_windows_hash_order_independent():
    a = WindowInfo(1, "A", "p", 0, Bounds(0, 0, 1, 1), True, window_uid="x:1")
    b = WindowInfo(2, "B", "p", 0, Bounds(0, 0, 1, 1), False, window_uid="x:2")
    assert windows_hash([a, b]) == windows_hash([b, a])

"""Tests for diff (custom and JSON Patch)."""
from __future__ import annotations

from diff import apply_custom, diff_custom, diff_json_patch


def _n(role, name="", **kw):
    return {
        "id": kw.get("id", role.lower()),
        "name": name, "role": role,
        "value": kw.get("value"),
        "bounds": kw.get("bounds", {"x": 0, "y": 0, "width": 0, "height": 0}),
        "enabled": kw.get("enabled", True),
        "focused": False,
        "keyboard_shortcut": None, "description": None,
        "children": kw.get("children", []),
    }


def test_no_changes():
    a = _n("Window", "x", children=[_n("Button", "OK")])
    b = _n("Window", "x", children=[_n("Button", "OK")])
    assert diff_custom(a, b) == []


def test_replace_field():
    a = _n("Window", "x", children=[_n("Button", "OK")])
    b = _n("Window", "x", children=[_n("Button", "OK", value="clicked")])
    changes = diff_custom(a, b)
    assert any(c["op"] == "replace" for c in changes)


def test_add_remove_child():
    a = _n("Window", "x", children=[_n("Button", "OK")])
    b = _n("Window", "x", children=[_n("Button", "OK"), _n("Button", "Cancel")])
    changes = diff_custom(a, b)
    assert any(c["op"] == "add" and c["node"].get("name") == "Cancel"
               for c in changes)


def test_move_detection():
    a = _n("Window", "x", children=[_n("Button", "OK"), _n("Button", "Cancel")])
    b = _n("Window", "x", children=[_n("Button", "Cancel"), _n("Button", "OK")])
    changes = diff_custom(a, b)
    assert any(c["op"] == "move" for c in changes)


def test_apply_custom_round_trip_field():
    a = _n("Window", "x", children=[_n("Button", "OK")])
    b = _n("Window", "x", children=[_n("Button", "OK", value="clicked")])
    changes = diff_custom(a, b)
    out = apply_custom(a, changes)
    # The replace touched the button's 'value'
    assert out["children"][0]["value"] == "clicked"


def test_json_patch_replace():
    a = _n("Window", "x", children=[_n("Button", "OK")])
    b = _n("Window", "x", children=[_n("Button", "OK", value="hello")])
    p = diff_json_patch(a, b)
    assert any(op["op"] == "replace" and op["path"].endswith("/value")
               for op in p)


def test_json_patch_add_remove():
    a = _n("Window", "x", children=[_n("Button", "OK")])
    b = _n("Window", "x", children=[])
    p = diff_json_patch(a, b)
    assert any(op["op"] == "remove" for op in p)

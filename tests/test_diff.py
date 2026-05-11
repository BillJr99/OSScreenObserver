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


def test_duplicate_identity_siblings_not_silently_moved():
    """Two siblings with the same (role, name) on either side must NOT be
    matched as moves — the setdefault collapse would have picked the
    first occurrence and silently mapped it across the lists, producing
    a misleading move op instead of clean add/remove pairs."""
    # Two anonymous Buttons in different orders.  Without de-dup, the
    # diff would call this a no-op or a single move; the correct answer
    # is to treat them as ambiguous and emit removes + adds.
    a = _n("Window", "w", children=[_n("Button", ""), _n("Button", "")])
    b = _n("Window", "w", children=[_n("Button", ""), _n("Button", "")])
    changes = diff_custom(a, b)
    # When duplicates are excluded from move detection, identical lists
    # produce add+remove pairs for every duplicated child.
    moves = [c for c in changes if c["op"] == "move"]
    assert moves == [], f"unexpected moves on duplicate identities: {moves}"


def test_unique_siblings_still_matched_as_move():
    """The duplicate-skip must not break clean reorderings of distinct
    siblings."""
    a = _n("Window", "w", children=[_n("Button", "OK"), _n("Button", "Cancel")])
    b = _n("Window", "w", children=[_n("Button", "Cancel"), _n("Button", "OK")])
    changes = diff_custom(a, b)
    assert any(c["op"] == "move" for c in changes)

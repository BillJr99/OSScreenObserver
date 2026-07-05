"""[P2] Trust-boundary tests: untrusted-content marking on screen-derived
results, ANSI/control-character sanitization, and the design-doc §21
confirmation-token audit (every destructive element-targeted verb must
route through the confirmation gate when confirmation mode is enabled).
"""
from __future__ import annotations

import pytest

import tools as _tools
from redaction import (
    UNTRUSTED_RESULT_TOOLS, mark_untrusted, sanitize_screen_text,
)


# ── sanitize_screen_text ─────────────────────────────────────────────────────


def test_sanitize_strips_csi_sequences():
    assert sanitize_screen_text("a\x1b[31mred\x1b[0mb") == "aredb"


def test_sanitize_strips_osc_sequences():
    assert sanitize_screen_text("x\x1b]0;evil title\x07y") == "xy"


def test_sanitize_strips_control_chars():
    assert sanitize_screen_text("a\x00b\x08c\x7fd") == "abcd"


def test_sanitize_keeps_layout_whitespace():
    assert sanitize_screen_text("line1\nline2\tcol\r") == "line1\nline2\tcol\r"


def test_sanitize_non_string_passthrough():
    assert sanitize_screen_text(None) is None
    assert sanitize_screen_text(42) == 42
    assert sanitize_screen_text("") == ""


# ── mark_untrusted ───────────────────────────────────────────────────────────


def test_mark_untrusted_flags_and_sanitizes_nested_text():
    result = {
        "ok": True,
        "tree": {"name": "evil\x1b[2Jname", "value": "v\x00v",
                 "children": [{"name": "child\x1b[31m"}]},
        "windows": [{"title": "T\x07itle"}],
    }
    out = mark_untrusted("get_window_structure", result)
    assert out["untrusted"] is True
    assert out["tree"]["name"] == "evilname"
    assert out["tree"]["value"] == "vv"
    assert out["tree"]["children"][0]["name"] == "child"
    assert out["windows"][0]["title"] == "Title"


def test_mark_untrusted_skips_opaque_fields():
    result = {"ok": True, "data": "base64\x1b[31mstuff",
              "tree_token": "tt:ab\x00cd"}
    out = mark_untrusted("get_ocr", result)
    assert out["data"] == "base64\x1b[31mstuff"       # untouched
    assert out["tree_token"] == "tt:ab\x00cd"          # untouched
    assert out["untrusted"] is True


def test_mark_untrusted_noop_for_action_tools():
    result = {"ok": True, "action": "click_element"}
    out = mark_untrusted("click_element", result)
    assert "untrusted" not in out


def test_untrusted_tool_list_is_read_tools_only():
    # No destructive verb should be in the untrusted-marking list;
    # perception tools carry the screen text.
    for name in UNTRUSTED_RESULT_TOOLS:
        assert not _tools._is_input_tool(name)


# ── Wire-through: REST results carry the flag ────────────────────────────────


def test_windows_result_is_untrusted(client):
    data = client.get("/api/windows").get_json()
    assert data["ok"] is True
    assert data["untrusted"] is True


def test_structure_result_is_untrusted(client):
    data = client.get("/api/structure").get_json()
    assert data["ok"] is True
    assert data["untrusted"] is True


def test_observe_result_is_untrusted(client):
    data = client.get("/api/observe").get_json()
    assert data["ok"] is True
    assert data["untrusted"] is True


def test_action_receipt_not_flagged(client):
    ws = client.get("/api/windows").get_json()
    uid = ws["windows"][0]["window_uid"]
    r = client.post("/api/element/click", json={
        "window_uid": uid,
        "selector": 'Window/MenuBar/MenuItem[name="File"]',
    }).get_json()
    assert r["ok"] is True
    assert "untrusted" not in r


# ── §21 confirmation audit ───────────────────────────────────────────────────
#
# Every destructive verb that resolves a concrete element target must route
# through _check_confirmation when confirmation_required rules are set.
#
# Coordinate/global verbs carry no element identity, so the role/name-based
# rules of §21 cannot apply to them by construction. They are the documented
# exclusion list below; the completeness test asserts every input tool is
# accounted for in exactly one of the two groups.

ELEMENT_VERBS = [
    ("click_element",             {}),
    ("focus_element",             {}),
    ("set_value",                 {"value": "x"}),
    ("invoke_element",            {}),
    ("select_option",             {"option_name": "x"}),
    ("hover_element",             {"hover_ms": 1}),
    ("right_click_element",       {}),
    ("double_click_element",      {}),
    ("key_into_element",          {"keys": "ctrl+a"}),
    ("clear_text",                {}),
    ("click_element_and_observe", {}),
]

# §21 scope limit: rules match element role/name; these verbs act on raw
# coordinates / global input focus and never resolve an element, so no
# rule can name them a destructive target. (click_element_and_observe is
# gated via click_element; type/press_key composites wrap the excluded
# global verbs.) `drag` IS gated when either endpoint is element-addressed.
COORDINATE_VERB_EXCLUSIONS = {
    "click_at", "right_click_at", "double_click_at",
    "type_text", "press_key", "scroll",
    "hover_at", "bring_to_foreground",
    "type_and_observe", "press_key_and_observe",
}


@pytest.fixture()
def confirm_ctx(config, observer, renderer, describer):
    config["confirmation_required"] = [{"name_regex": "(?i)file"}]
    return _tools.ToolContext(observer=observer, renderer=renderer,
                              describer=describer, config=config)


def _file_menu_args(ctx):
    win = ctx.observer.list_windows()[0]
    return {"window_uid": win.window_uid,
            "selector": 'Window/MenuBar/MenuItem[name="File"]'}


@pytest.mark.parametrize("verb,extra", ELEMENT_VERBS)
def test_element_verb_requires_confirmation(confirm_ctx, verb, extra):
    args = {**_file_menu_args(confirm_ctx), **extra}
    r = _tools.dispatch(confirm_ctx, verb, args)
    assert r["ok"] is False, verb
    assert r["error"]["code"] == "ConfirmationRequired", verb


def test_drag_requires_confirmation_for_element_endpoint(confirm_ctx):
    base = _file_menu_args(confirm_ctx)
    r = _tools.dispatch(confirm_ctx, "drag", {
        "window_uid": base["window_uid"],
        "from": {"selector": base["selector"]},
        "to": {"x": 300, "y": 300},
    })
    assert r["ok"] is False
    assert r["error"]["code"] == "ConfirmationRequired"


def test_drag_requires_confirmation_for_destination_endpoint(confirm_ctx):
    base = _file_menu_args(confirm_ctx)
    r = _tools.dispatch(confirm_ctx, "drag", {
        "window_uid": base["window_uid"],
        "from": {"x": 100, "y": 100},
        "to": {"selector": base["selector"]},
    })
    assert r["ok"] is False
    assert r["error"]["code"] == "ConfirmationRequired"


def test_drag_with_valid_token_passes_gate(confirm_ctx):
    base = _file_menu_args(confirm_ctx)
    prop = _tools.dispatch(confirm_ctx, "propose_action", {
        "action": "drag",
        "args": base,
    })
    assert prop["ok"] is True
    r = _tools.dispatch(confirm_ctx, "drag", {
        "window_uid": base["window_uid"],
        "from": {"selector": base["selector"]},
        "to": {"x": 300, "y": 300},
        "confirm_token": prop["confirm_token"],
    })
    # The gate is satisfied; the drag itself may still fail on headless CI
    # (pyautogui unavailable) — but never with a confirmation error.
    if r["ok"] is False:
        assert r["error"]["code"] not in ("ConfirmationRequired",
                                          "ConfirmationInvalid")


def test_drag_with_bogus_token_rejected(confirm_ctx):
    base = _file_menu_args(confirm_ctx)
    r = _tools.dispatch(confirm_ctx, "drag", {
        "window_uid": base["window_uid"],
        "from": {"selector": base["selector"]},
        "to": {"x": 300, "y": 300},
        "confirm_token": "ct:doesnotexist",
    })
    assert r["ok"] is False
    assert r["error"]["code"] == "ConfirmationInvalid"


def test_confirmation_audit_covers_every_input_tool():
    gated = {v for v, _ in ELEMENT_VERBS} | {"drag"}
    accounted = gated | COORDINATE_VERB_EXCLUSIONS
    input_tools = {n for n in _tools.REGISTRY if _tools._is_input_tool(n)}
    unaccounted = input_tools - accounted
    assert not unaccounted, (
        f"input tools missing from the §21 audit: {sorted(unaccounted)}")

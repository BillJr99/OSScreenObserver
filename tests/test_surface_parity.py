"""[P2] REST ↔ MCP ↔ dispatch parity (design doc §27).

Every tool in tools.REGISTRY must be reachable with the same parameter
surface from both public surfaces:

  - REST: the generic tool console (POST /api/tool/<name>) forwards the
    JSON body verbatim to tools.dispatch, and GET /api/tools enumerates
    the registry. Most tools additionally have a dedicated route.
  - MCP: mcp_server advertises a schema in _TOOLS and forwards
    `arguments` verbatim to tools.dispatch for every REGISTRY tool.

Exceptions are explicit, documented allowlists below — any new tool that
is added to one surface but not the others fails these tests.
"""
from __future__ import annotations

import pytest

import mcp_server as _mcp
import tools as _tools

# REGISTRY tools with no dedicated MCP schema: pure aliases of click_at —
# MCP callers pass button="right" / double=true to click_at instead.
MCP_SCHEMA_EXCLUSIONS = {
    "right_click_at",
    "double_click_at",
}

# MCP tools handled by legacy composite handlers inside mcp_server._dispatch
# (not part of tools.REGISTRY); their REST equivalents are /api/sketch and
# /api/full_screenshot.
MCP_LEGACY_ONLY = {
    "get_screen_sketch",
    "get_full_screenshot",
}

# REGISTRY tools with no dedicated REST route (reachable via the generic
# console only): pure aliases of click_at — REST callers POST /api/action
# with {"action": "click_at", "button": "right"} / {"double": true}.
REST_DEDICATED_ROUTE_EXCLUSIONS = {
    "right_click_at",
    "double_click_at",
}

# tool name -> dedicated REST route (path prefix as registered in Flask).
REST_ROUTES = {
    "list_windows":               "/api/windows",
    "get_capabilities":           "/api/capabilities",
    "get_monitors":               "/api/monitors",
    "find_element":               "/api/find_element",
    "get_window_structure":       "/api/structure",
    "get_screenshot":             "/api/screenshot",
    "get_visible_areas":          "/api/visible_areas",
    "click_element":              "/api/element/click",
    "focus_element":              "/api/element/focus",
    "set_value":                  "/api/element/set_value",
    "invoke_element":             "/api/element/invoke",
    "select_option":              "/api/element/select",
    "click_at":                   "/api/action",
    "type_text":                  "/api/action",
    "press_key":                  "/api/action",
    "scroll":                     "/api/action",
    "bring_to_foreground":        "/api/bring_to_foreground",
    "observe_window":             "/api/observe",
    "snapshot":                   "/api/snapshot",
    "snapshot_get":               "/api/snapshot/<sid>",
    "snapshot_diff":              "/api/snapshot/diff",
    "snapshot_drop":              "/api/snapshot/<sid>",
    "wait_for":                   "/api/wait_for",
    "wait_idle":                  "/api/wait_idle",
    "click_element_and_observe":  "/api/element/click_and_observe",
    "type_and_observe":           "/api/type_and_observe",
    "press_key_and_observe":      "/api/key_and_observe",
    "get_screenshot_cropped":     "/api/screenshot/cropped",
    "get_ocr":                    "/api/ocr",
    "get_screen_description":     "/api/description",
    "trace_start":                "/api/trace/start",
    "trace_stop":                 "/api/trace/stop",
    "trace_status":               "/api/trace/status",
    "replay_start":               "/api/replay/start",
    "replay_step":                "/api/replay/step",
    "replay_status":              "/api/replay/status",
    "replay_stop":                "/api/replay/stop",
    "load_scenario":              "/api/scenario/load",
    "assert_state":               "/api/assert_state",
    "get_budget_status":          "/api/budget_status",
    "get_redaction_status":       "/api/redaction_status",
    "propose_action":             "/api/propose_action",
    "hover_at":                   "/api/hover",
    "hover_element":              "/api/hover",
    "right_click_element":        "/api/element/right_click",
    "double_click_element":       "/api/element/double_click",
    "drag":                       "/api/drag",
    "key_into_element":           "/api/element/key",
    "clear_text":                 "/api/element/clear_text",
}

# Tools not invoked in the smoke loop: session-stateful (an active trace
# would then record every subsequent call in this process's global session).
SMOKE_SKIP = {"trace_start", "trace_stop"}

# Minimal args so no tool blocks (waits) or errors on missing input in a
# way unrelated to reachability.
SMOKE_ARGS = {
    "wait_for":  {"timeout_ms": 20, "predicate": {"kind": "window_exists",
                                                  "title_regex": "."}},
    "wait_idle": {"timeout_ms": 20, "quiet_ms": 5},
    "hover_at":  {"hover_ms": 1},
    "hover_element": {"hover_ms": 1,
                      "selector": 'MenuItem[name="File"]'},
}


def test_mcp_advertises_every_registry_tool():
    mcp_names = {t["name"] for t in _mcp._TOOLS}
    missing = set(_tools.REGISTRY) - mcp_names - MCP_SCHEMA_EXCLUSIONS
    assert not missing, f"REGISTRY tools missing an MCP schema: {sorted(missing)}"


def test_mcp_schema_exclusions_are_not_advertised_stale():
    # Keep the exclusion list honest: everything on it must exist in the
    # REGISTRY and must genuinely lack an MCP schema.
    mcp_names = {t["name"] for t in _mcp._TOOLS}
    for name in MCP_SCHEMA_EXCLUSIONS:
        assert name in _tools.REGISTRY
        assert name not in mcp_names


def test_every_mcp_tool_is_dispatchable():
    mcp_names = {t["name"] for t in _mcp._TOOLS}
    unknown = mcp_names - set(_tools.REGISTRY) - MCP_LEGACY_ONLY
    assert not unknown, f"MCP schemas with no dispatch target: {sorted(unknown)}"


def test_mcp_schemas_have_object_parameter_surface():
    # MCP forwards `arguments` verbatim to tools.dispatch, so every schema
    # must declare an object inputSchema (same dict-shaped surface REST
    # forwards from the JSON body).
    for t in _mcp._TOOLS:
        schema = t.get("inputSchema") or {}
        assert schema.get("type") == "object", t["name"]
        assert isinstance(schema.get("properties", {}), dict), t["name"]


def test_rest_console_enumerates_registry(client):
    data = client.get("/api/tools").get_json()
    assert data["ok"] is True
    assert data["tools"] == sorted(_tools.REGISTRY)


def test_every_registry_tool_has_dedicated_rest_route(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    for name in _tools.REGISTRY:
        if name in REST_DEDICATED_ROUTE_EXCLUSIONS:
            assert name not in REST_ROUTES
            continue
        assert name in REST_ROUTES, f"no documented REST route for {name}"
        assert REST_ROUTES[name] in rules, (
            f"{name}: documented route {REST_ROUTES[name]} not registered")


@pytest.mark.parametrize("name", sorted(_tools.REGISTRY))
def test_rest_console_reaches_tool(client, name):
    """POST /api/tool/<name> must reach dispatch for every REGISTRY tool
    (a structured envelope comes back — never Flask's 404/405 or the
    dispatcher's 'unknown tool' error)."""
    if name in SMOKE_SKIP:
        pytest.skip("session-stateful; reachability covered by url_map test")
    r = client.post(f"/api/tool/{name}", json=SMOKE_ARGS.get(name, {}))
    assert r.status_code != 404 or r.get_json() is not None
    data = r.get_json()
    assert isinstance(data, dict), name
    assert "ok" in data, name
    if data["ok"] is False:
        msg = (data.get("error") or {}).get("message", "")
        assert not msg.startswith("unknown tool"), name

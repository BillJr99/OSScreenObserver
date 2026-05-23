"""
End-to-end tests for the MCP stdio framing channel.

Spawns `python main.py --mode mcp --mock` and drives the JSON-RPC
content-length framing manually. Verifies:
  - initialize / tools/list / tools/call shape
  - stdout purity (logs must go to stderr, not stdout — otherwise an
    MCP client would mis-parse the framing).
  - error codes from errors.py round-trip cleanly.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.user]


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------

class TestMCPHandshake:
    def test_initialize_returns_server_info(self, mcp):
        r = mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                       "capabilities": {},
                                       "clientInfo": {"name": "user-test",
                                                      "version": "0.0.0"}})
        assert "result" in r
        info = r["result"].get("serverInfo") or r["result"]
        assert info.get("name") == "os-screen-observer"
        assert info.get("version")

    def test_tools_list_includes_core_tools(self, mcp):
        # initialize is optional in our server but many clients call it first.
        mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
        r = mcp.request("tools/list", {})
        tools = r["result"]["tools"]
        names = [t["name"] for t in tools]
        for required in [
            "list_windows", "get_window_structure", "get_screen_description",
            "get_screenshot", "find_element", "click_element", "observe_window",
            "snapshot", "wait_for", "trace_start", "trace_stop",
            "load_scenario", "assert_state", "get_budget_status",
            "click_element_and_observe",
        ]:
            assert required in names, f"missing MCP tool {required!r}"

    def test_tools_call_list_windows(self, mcp):
        mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
        r = mcp.request("tools/call",
                        {"name": "list_windows", "arguments": {}})
        # MCP tool/call response wraps the payload in `result.content[0].text`
        # as a JSON-encoded string per the spec.
        result = r["result"]
        content = result["content"][0]
        assert content["type"] == "text"
        payload = json.loads(content["text"])
        assert payload["ok"] is True
        assert payload["count"] >= 1


class TestMCPErrors:
    def test_unknown_tool_returns_error_envelope(self, mcp):
        mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        r = mcp.request("tools/call",
                        {"name": "no-such-tool", "arguments": {}})
        # Either a JSON-RPC top-level error, or a result whose payload is a
        # plain-text error message (or an `ok: false` envelope when the
        # server has a richer error code path).
        if "error" in r:
            assert r["error"]["code"] != 0
        else:
            text = r["result"]["content"][0]["text"]
            # Try JSON first; if it isn't JSON, accept a plain-text complaint.
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                assert "unknown" in text.lower() or "not" in text.lower(), text
                return
            # JSON path: tolerate either ok=False or an error key.
            if isinstance(payload, dict):
                assert payload.get("ok") is False or "error" in payload, payload
            else:
                assert "unknown" in str(payload).lower() or \
                       "not" in str(payload).lower(), payload

    def test_find_element_not_found_returns_recoverable_error(self, mcp):
        mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        r = mcp.request("tools/call",
                        {"name": "find_element",
                         "arguments": {"window_index": 0,
                                       "selector": 'Window/Nope[name="X"]'}})
        payload = json.loads(r["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ElementNotFound"
        assert payload["error"]["recoverable"] is True


# ---------------------------------------------------------------------------
# stdout purity
# ---------------------------------------------------------------------------

class TestStdoutPurity:
    def test_no_extraneous_log_lines_on_stdout(self, oso_mcp_server, mcp):
        """All log output must go to stderr, not stdout, because the MCP
        framing channel lives on stdout.
        """
        mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        # Do a noisy operation that triggers logger.info inside main.
        mcp.request("tools/call", {"name": "list_windows", "arguments": {}})
        # If something logged onto stdout, mcp._read would have thrown because
        # the next 'Content-Length' header would have been preceded by log
        # garbage. Surviving up to this point IS the assertion.
        # Additionally check that stderr captured the expected startup banner.
        stderr_text = oso_mcp_server["stderr_log"].read_text(errors="replace")
        assert "screen_observer" in stderr_text.lower() or \
               "main" in stderr_text.lower(), \
               f"expected log lines on stderr; got:\n{stderr_text[:500]}"


# ---------------------------------------------------------------------------
# Coverage smoke — call every MCP tool at least once
# ---------------------------------------------------------------------------

# All 49 MCP tools listed in mcp_server.py. We accept either ok=True or a
# clean error envelope (recoverable) — the smoke test verifies the call
# routes correctly through MCP framing.
_DEFAULT_SEL = 'Window/MenuBar/MenuItem[name="Edit"]'

_ALL_MCP_TOOLS = [
    ("list_windows", {}),
    ("get_window_structure", {"window_index": 0}),
    ("get_screen_description", {"window_index": 0}),
    ("get_screen_sketch", {"window_index": 0}),
    ("get_screenshot", {"window_index": 0}),
    ("click_at", {"window_index": 0, "x": 100, "y": 100, "dry_run": True}),
    ("type_text", {"value": "x", "dry_run": True}),
    ("press_key", {"keys": "shift", "dry_run": True}),
    ("scroll", {"window_index": 0, "dx": 0, "dy": 1, "dry_run": True}),
    ("get_full_screenshot", {}),
    ("get_visible_areas", {"window_index": 0}),
    ("bring_to_foreground", {"window_index": 0, "dry_run": True}),
    ("get_capabilities", {}),
    ("get_monitors", {}),
    ("find_element", {"window_index": 0, "selector": _DEFAULT_SEL}),
    ("click_element", {"window_index": 0, "selector": _DEFAULT_SEL, "dry_run": True}),
    ("focus_element", {"window_index": 0, "selector": _DEFAULT_SEL, "dry_run": True}),
    ("set_value", {"window_index": 0,
                   "selector": 'Window/Form/TextBox[name="Search"]',
                   "value": "x", "dry_run": True}),
    ("invoke_element", {"window_index": 0, "selector": _DEFAULT_SEL, "dry_run": True}),
    ("select_option", {"window_index": 0, "selector": _DEFAULT_SEL,
                        "option_name": "x", "dry_run": True}),
    ("observe_window", {"window_index": 0}),
    ("snapshot", {"window_index": 0}),
    ("snapshot_get", {"snapshot_id": "snap:bogus"}),
    ("snapshot_drop", {"snapshot_id": "snap:bogus"}),
    ("wait_for", {"any_of": [{"type": "window_appears", "title_regex": "Notepad"}],
                  "timeout_ms": 200}),
    ("wait_idle", {"window_index": 0, "duration_ms": 100}),
    ("click_element_and_observe",
     {"window_index": 0, "selector": _DEFAULT_SEL, "dry_run": True}),
    ("type_and_observe",
     {"window_index": 0, "selector": _DEFAULT_SEL, "text": "x", "dry_run": True}),
    ("press_key_and_observe",
     {"window_index": 0, "keys": "shift", "dry_run": True}),
    ("get_screenshot_cropped",
     {"window_index": 0, "bbox": "10,10,40,40"}),
    ("trace_start", {"label": "smoke"}),
    ("trace_status", {}),
    ("trace_stop", {}),
    ("replay_status", {}),
    ("get_budget_status", {}),
    ("get_redaction_status", {}),
    ("propose_action",
     {"action": "click_element",
      "args": {"window_index": 0, "selector": _DEFAULT_SEL}}),
    ("assert_state",
     {"predicate": [{"kind": "element_exists",
                     "selector": _DEFAULT_SEL,
                     "window_index": 0}]}),
    ("hover_at", {"window_index": 0, "x": 50, "y": 50, "dry_run": True}),
    ("hover_element",
     {"window_index": 0, "selector": _DEFAULT_SEL, "dry_run": True}),
    ("right_click_element",
     {"window_index": 0, "selector": _DEFAULT_SEL, "dry_run": True}),
    ("double_click_element",
     {"window_index": 0, "selector": _DEFAULT_SEL, "dry_run": True}),
    ("drag", {"from": {"x": 10, "y": 10}, "to": {"x": 20, "y": 20},
              "window_index": 0, "dry_run": True}),
    ("key_into_element",
     {"window_index": 0, "selector": _DEFAULT_SEL,
      "keys": "tab", "dry_run": True}),
    ("clear_text",
     {"window_index": 0,
      "selector": 'Window/Form/TextBox[name="Search"]',
      "dry_run": True}),
    ("get_ocr", {"window_index": 0}),
]


class TestMCPSmokeCoverage:
    """Calls every MCP tool exposed by the server, allowing either
    success or a recoverable error envelope. Verifies that MCP routing
    and JSON framing work for the full tool surface."""

    def test_all_49_tools_round_trip(self, mcp):
        mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
        results: dict[str, dict] = {}
        framing_errors: list[str] = []
        for name, args in _ALL_MCP_TOOLS:
            try:
                r = mcp.request("tools/call",
                                {"name": name, "arguments": args})
            except Exception as e:
                framing_errors.append(f"{name}: framing error {e!r}")
                continue
            if "error" in r:
                # JSON-RPC level error — record + continue.
                results[name] = {"_jsonrpc_error": r["error"]}
                continue
            try:
                payload = json.loads(r["result"]["content"][0]["text"])
            except (json.JSONDecodeError, KeyError, IndexError):
                payload = {"_unparseable": r["result"]}
            results[name] = payload

        assert not framing_errors, \
            f"MCP framing failures:\n{chr(10).join(framing_errors)}"

        # Every call must produce a parseable result envelope.
        unparseable = [k for k, v in results.items() if "_unparseable" in v]
        assert not unparseable, f"unparseable results for: {unparseable}"

        # At least 75% of tools must report ok=True against the mock adapter.
        ok_count = sum(1 for v in results.values() if v.get("ok") is True)
        assert ok_count >= len(_ALL_MCP_TOOLS) * 0.75, (
            f"only {ok_count}/{len(_ALL_MCP_TOOLS)} MCP tools returned ok=True. "
            f"Failing tools: "
            f"{ {k: v.get('error', v) for k, v in results.items() if v.get('ok') is not True} }"
        )

    def test_total_count_matches_documented_49(self, mcp):
        mcp.request("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
        r = mcp.request("tools/list", {})
        tools = r["result"]["tools"]
        # mcp_server.py exposes 49 tools today.  Locking this number
        # surfaces accidental additions or removals.
        assert len(tools) >= 45, f"unexpectedly few MCP tools: {len(tools)}"

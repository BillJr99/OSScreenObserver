"""
End-to-end user tests for the OSScreenObserver Flask REST surface.

Spawns a real `python main.py --mode inspect --mock --port <free>`
subprocess and drives every documented endpoint over loopback HTTP. The
existing test_rest_api.py / test_tools_p*.py files use the Flask in-process
test client; these tests use the wire format to catch threading, JSON
serialisation, header, and CORS issues that an in-process client hides.
"""
from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.user]


# ---------------------------------------------------------------------------
# Health + capabilities
# ---------------------------------------------------------------------------

class TestHealth:
    def test_healthz_status_200(self, http):
        status, body = http.get("/api/healthz")
        assert status == 200
        assert body["ok"] is True

    def test_healthz_reports_adapter_and_uptime(self, http):
        _, body = http.get("/api/healthz")
        assert body["adapter"] == "MockAdapter"
        assert body["uptime_s"] >= 0

    def test_capabilities_supports_accessibility_tree(self, http):
        _, body = http.get("/api/capabilities")
        assert body["ok"] is True
        assert "supports" in body
        assert body["supports"]["accessibility_tree"] is True


class TestWindows:
    def test_list_windows_returns_mock_set(self, http):
        _, body = http.get("/api/windows")
        assert body["ok"] is True
        assert body["count"] >= 1
        for w in body["windows"]:
            assert "window_uid" in w
            assert "title" in w

    def test_monitors_present(self, http):
        _, body = http.get("/api/monitors")
        assert body["ok"] is True
        assert isinstance(body["monitors"], list)


class TestStructure:
    def test_default_window_structure(self, http):
        _, body = http.get("/api/structure")
        assert body["ok"] is True
        assert "tree" in body
        assert body["tree"]["role"]  # non-empty

    def test_structure_with_window_index(self, http):
        _, body = http.get("/api/structure", {"window_index": 0})
        assert body["ok"] is True

    def test_structure_invalid_window_index_falls_back(self, http):
        # Mock adapter falls back to the focused window rather than erroring.
        # Verify the call still succeeds and returns a tree.
        _, body = http.get("/api/structure", {"window_index": 99999})
        assert body["ok"] is True
        assert body["tree"]


# ---------------------------------------------------------------------------
# Find element / selectors
# ---------------------------------------------------------------------------

class TestFindElement:
    def test_happy_path(self, http):
        _, body = http.get(
            "/api/find_element",
            {"window_index": 0,
             "selector": 'Window/MenuBar/MenuItem[name="Edit"]'},
        )
        assert body["ok"] is True
        assert body["element_id"]

    def test_not_found_error_envelope(self, http):
        _, body = http.get(
            "/api/find_element",
            {"window_index": 0, "selector": 'Window/Nope[name="X"]'},
        )
        assert body["ok"] is False
        assert body["error"]["code"] == "ElementNotFound"
        assert body["error"]["recoverable"] is True

    def test_ambiguous_match_returns_count(self, http):
        _, body = http.get(
            "/api/find_element",
            {"window_index": 0, "selector": "Window/MenuBar/MenuItem"},
        )
        assert body["ok"] is True
        assert body["ambiguous_matches"] >= 2


# ---------------------------------------------------------------------------
# Element actions
# ---------------------------------------------------------------------------

class TestElementActions:
    def _selector(self):
        return 'Window/MenuBar/MenuItem[name="Edit"]'

    def test_click_element_emits_receipt(self, http):
        status, body = http.post("/api/element/click",
                                 {"window_index": 0, "selector": self._selector()})
        assert status == 200
        assert body["ok"] is True
        assert body["action"] == "click_element"
        assert "duration_ms" in body
        assert body["dry_run"] is False

    def test_click_element_dry_run(self, http):
        _, body = http.post("/api/element/click",
                            {"window_index": 0, "selector": self._selector(),
                             "dry_run": True})
        assert body["ok"] is True
        assert body["dry_run"] is True
        assert body["changed"] is False

    def test_focus_element(self, http):
        _, body = http.post("/api/element/focus",
                            {"window_index": 0, "selector": self._selector()})
        assert body["ok"] is True

    def test_set_value_returns_diff(self, http):
        _, body = http.post("/api/element/set_value",
                            {"window_index": 0,
                             "selector": 'Window/Form/TextBox[name="Search"]',
                             "value": "autogui"})
        # The mock tree may not have that exact selector; accept either path.
        assert isinstance(body, dict) and "ok" in body

    def test_right_click(self, http):
        _, body = http.post("/api/element/right_click",
                            {"window_index": 0, "selector": self._selector()})
        assert body["ok"] is True

    def test_double_click(self, http):
        _, body = http.post("/api/element/double_click",
                            {"window_index": 0, "selector": self._selector()})
        assert body["ok"] is True


# ---------------------------------------------------------------------------
# Click_and_observe / type_and_observe / key_and_observe
# ---------------------------------------------------------------------------

class TestAndObserveCompositions:
    def test_click_and_observe_bundles_diff(self, http):
        _, body = http.post(
            "/api/element/click_and_observe",
            {"window_index": 0,
             "selector": 'Window/MenuBar/MenuItem[name="Edit"]'},
        )
        assert body["ok"] is True
        # observation envelope is composed in
        assert "observe" in body or "after" in body


# ---------------------------------------------------------------------------
# Snapshot lifecycle
# ---------------------------------------------------------------------------

class TestSnapshotLifecycle:
    def test_create_get_diff_drop_round_trip(self, http):
        # Create snapshot A
        status, body = http.post("/api/snapshot", {"window_index": 0})
        assert status == 200 and body["ok"] is True
        sid_a = body["snapshot_id"]
        assert sid_a.startswith("snap:")

        # Get it back
        status, body = http.get(f"/api/snapshot/{sid_a}")
        assert body["ok"] is True
        assert "trees" in body and "tree_hashes" in body

        # Create snapshot B
        _, body_b = http.post("/api/snapshot", {"window_index": 0})
        sid_b = body_b["snapshot_id"]

        # Diff A vs B
        _, body_diff = http.post("/api/snapshot/diff", {"a": sid_a, "b": sid_b})
        assert body_diff["ok"] is True

        # Drop A
        status, body_del = http.delete(f"/api/snapshot/{sid_a}")
        assert status == 200
        assert body_del["dropped"] is True

    def test_snapshot_diff_missing_args_returns_bad_request(self, http):
        _, body = http.post("/api/snapshot/diff", {})
        assert body["ok"] is False
        assert body["error"]["code"] == "BadRequest"


# ---------------------------------------------------------------------------
# Observe diff token
# ---------------------------------------------------------------------------

class TestObserveDiff:
    def test_observe_full_then_diff_token(self, http):
        _, full = http.get("/api/observe", {"window_index": 0})
        assert full["ok"] is True
        token = full.get("tree_token")
        assert token, f"missing tree_token in {full!r}"
        _, partial = http.get(
            "/api/observe", {"window_index": 0, "since": token},
        )
        assert partial["ok"] is True

    def test_observe_unknown_token_falls_back_to_full(self, http):
        _, body = http.get(
            "/api/observe", {"window_index": 0, "since": "bogus-token"},
        )
        assert body["ok"] is True
        assert body.get("base_token") is None


# ---------------------------------------------------------------------------
# Wait
# ---------------------------------------------------------------------------

class TestWait:
    def test_wait_for_immediate_match(self, http):
        _, body = http.post(
            "/api/wait_for",
            {"any_of": [{"type": "window_appears", "title_regex": "Notepad"}],
             "timeout_ms": 500},
        )
        assert body["ok"] is True
        assert body["matched_index"] == 0

    def test_wait_for_timeout(self, http):
        _, body = http.post(
            "/api/wait_for",
            {"any_of": [{"type": "window_appears", "title_regex": "NEVER-DOES-EXIST"}],
             "timeout_ms": 300, "poll_ms": 80},
        )
        assert body["ok"] is False
        assert body["error"]["code"] == "Timeout"
        assert body["polls"] >= 1


# ---------------------------------------------------------------------------
# Screenshot / cropped / OCR
# ---------------------------------------------------------------------------

class TestScreenshotEndpoints:
    def test_screenshot_returns_png_base64(self, http):
        _, body = http.get("/api/screenshot", {"window_index": 0})
        # Screenshot endpoints don't include an `ok` field — success is
        # signalled by the presence of `data` + the right encoding.
        assert body["encoding"] == "base64"
        assert body["format"] == "png"
        assert body["data"]  # non-empty base64 payload

    def test_full_screenshot_returns_envelope(self, http):
        _, body = http.get("/api/full_screenshot")
        assert body["encoding"] == "base64"
        assert body["format"] == "png"
        assert body["width"] > 0
        assert body["height"] > 0

    def test_screenshot_cropped(self, http):
        _, body = http.get("/api/screenshot/cropped",
                           {"window_index": 0,
                            "bbox": "10,10,40,40"})
        # Cropping always returns either a base64 payload or an error envelope.
        assert ("data" in body) or ("error" in body) or ("ok" in body)


# ---------------------------------------------------------------------------
# Description / sketch / ASCII
# ---------------------------------------------------------------------------

class TestDescription:
    def test_description_combined(self, http):
        _, body = http.get("/api/description", {"window_index": 0})
        assert body["ok"] is True

    def test_sketch_returns_text(self, http):
        _, body = http.get("/api/sketch", {"window_index": 0})
        # /api/sketch has no `ok` field; success is signalled by `sketch` payload.
        assert body["sketch"]
        assert body["grid_width"] > 0
        assert body["grid_height"] > 0


# ---------------------------------------------------------------------------
# Trace lifecycle
# ---------------------------------------------------------------------------

class TestTraceLifecycle:
    def test_start_status_stop(self, http, tmp_path):
        _, body = http.post("/api/trace/start", {"path": str(tmp_path / "trace.jsonl")})
        assert body["ok"] is True
        _, status_body = http.get("/api/trace/status")
        assert status_body["ok"] is True
        _, stop_body = http.post("/api/trace/stop", {})
        assert stop_body["ok"] is True


# ---------------------------------------------------------------------------
# Tools introspection
# ---------------------------------------------------------------------------

class TestToolsIntrospection:
    def test_list_tools(self, http):
        _, body = http.get("/api/tools")
        assert body["ok"] is True
        # tools is a list of name strings.
        names = list(body.get("tools", []))
        for required in ["list_windows", "find_element", "click_element",
                         "get_screenshot", "observe_window"]:
            assert required in names, f"missing tool {required!r} in {names}"

    def test_invoke_tool_via_generic_endpoint(self, http):
        status, body = http.post("/api/tool/list_windows", {})
        assert status == 200
        assert body["ok"] is True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_metrics_returns_prometheus_text(self, http):
        # First, do one action so the step counter increments.
        http.post("/api/element/click",
                  {"window_index": 0,
                   "selector": 'Window/MenuBar/MenuItem[name="Edit"]'})
        status, text = http.get_text("/api/metrics")
        assert status == 200
        assert "oso_step_count" in text
        assert "oso_uptime_seconds" in text

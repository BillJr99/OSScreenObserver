"""
Trace/replay round-trip over the live REST API.

Mirrors the in-process test_full_scenario_round_trip but goes through the
real subprocess so the trace file is actually written to disk and re-read
during replay.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

OSO_ROOT = Path(__file__).resolve().parents[2]
LOGIN_YAML = str(OSO_ROOT / "scenarios_examples" / "login.yaml")


class TestTraceLifecycle:
    def test_trace_writes_jsonl_file_and_step_count(self, http, oso_server):
        _, start = http.post("/api/trace/start", {"label": "user-trace-1"})
        assert start["ok"] is True
        trace_id = start["trace_id"]
        assert trace_id.startswith("trace-")

        # Generate a few traced calls.
        http.get("/api/windows")
        http.get("/api/structure", {"window_index": 0})
        http.post("/api/snapshot", {"window_index": 0})

        _, status = http.get("/api/trace/status")
        assert status["active_trace_id"] == trace_id
        assert status["step_count"] >= 3

        _, stop = http.post("/api/trace/stop", {})
        assert stop["ok"] is True
        assert stop["step_count"] >= 3
        # The path is relative to the server's CWD; resolve it.
        path = stop["path"]
        if not os.path.isabs(path):
            path = os.path.join(oso_server["cwd"], path)
        assert os.path.exists(path), f"trace file not found at {path}"
        with open(path) as f:
            lines = [l for l in f if l.strip()]
        assert lines, "trace file is empty"

    def test_status_when_no_active_trace(self, http):
        # Start + immediately stop, then status should reflect no active trace.
        http.post("/api/trace/start", {"label": "x"})
        http.post("/api/trace/stop", {})
        _, st = http.get("/api/trace/status")
        assert st.get("active_trace_id") in (None, "")


class TestReplayDivergenceFree:
    def test_record_login_then_replay_verify_no_divergence(self, http, tmp_path):
        http.post("/api/scenario/load", {"path": LOGIN_YAML})

        _, start = http.post("/api/trace/start", {"label": "login-record"})
        trace_dir = start["dir"]

        _, ws = http.get("/api/windows")
        uid = ws["windows"][0]["window_uid"]

        for name, text in (("Username", "alice"), ("Password", "hunter2")):
            _, fe = http.get("/api/find_element",
                             {"window_uid": uid,
                              "selector": f'Window/Edit[name="{name}"]'})
            http.post("/api/element/click",
                      {"window_uid": uid, "element_id": fe["element_id"]})
            http.post("/api/action", {"action": "type", "value": text})

        _, fe = http.get("/api/find_element",
                         {"window_uid": uid,
                          "selector": 'Window/Button[name="Login"]'})
        http.post("/api/element/click",
                  {"window_uid": uid, "element_id": fe["element_id"]})

        _, stop = http.post("/api/trace/stop", {})
        assert stop["step_count"] >= 8

        # Reset state and replay.
        http.post("/api/scenario/load", {"path": LOGIN_YAML})
        _, rs = http.post("/api/replay/start",
                          {"path": trace_dir, "mode": "verify"})
        assert rs["ok"] is True
        rid = rs["replay_id"]

        divergences = 0
        steps_taken = 0
        while True:
            _, rep = http.post("/api/replay/step", {"replay_id": rid})
            steps_taken += 1
            if rep.get("divergence"):
                divergences += 1
            if rep.get("finished"):
                break
            if steps_taken > 200:
                pytest.fail("replay did not finish within 200 steps")
        assert divergences == 0
        # Cleanly stop the replay (idempotent).
        http.post("/api/replay/stop", {"replay_id": rid})

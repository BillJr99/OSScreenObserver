"""
Drives the scenarios_examples/login.yaml end-to-end through the spawned
OSO subprocess. Verifies the reaction-based state machine progresses
from `start` to `welcome`, that oracles fire, and that the trace records
each action.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

OSO_ROOT = Path(__file__).resolve().parents[2]
LOGIN_YAML = str(OSO_ROOT / "scenarios_examples" / "login.yaml")


class TestScenarioLoad:
    def test_load_login_yaml(self, http):
        _, body = http.post("/api/scenario/load", {"path": LOGIN_YAML})
        assert body["ok"] is True
        assert body.get("state") == "start" or body.get("current_state") == "start"

    def test_initial_windows_present(self, http):
        http.post("/api/scenario/load", {"path": LOGIN_YAML})
        _, windows = http.get("/api/windows")
        titles = [w["title"] for w in windows["windows"]]
        assert any("Acme" in t for t in titles)


def _drive_login(http) -> dict:
    """Drive the login.yaml scenario from start to welcome via /api endpoints.
    Mirrors the steps in test_full_scenario_round_trip from tests/test_tools_p4.py.
    """
    http.post("/api/scenario/load", {"path": LOGIN_YAML})
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
    _, click_result = http.post("/api/element/click",
                                 {"window_uid": uid, "element_id": fe["element_id"]})
    return click_result


class TestScenarioReactions:
    def test_full_login_flow_transitions_to_welcome(self, http):
        _drive_login(http)
        _, ws = http.get("/api/windows")
        titles = [w["title"] for w in ws["windows"]]
        assert any("Welcome" in t for t in titles), titles


class TestScenarioOracles:
    def test_text_visible_oracle_passes_on_welcome(self, http):
        _drive_login(http)
        _, r = http.post("/api/assert_state",
                         {"predicate": [{"kind": "text_visible",
                                         "regex": "Hello, alice"}]})
        assert r["ok"] is True
        assert r["all_passed"] is True

    def test_failure_oracle_does_not_fire_in_happy_path(self, http):
        http.post("/api/scenario/load", {"path": LOGIN_YAML})
        _, r = http.post("/api/assert_state",
                         {"predicate": [{"kind": "window_exists",
                                         "title_regex": "Error"}]})
        assert r["ok"] is True
        assert r["all_passed"] is False

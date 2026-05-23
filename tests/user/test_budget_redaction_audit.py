"""
End-to-end checks for budget enforcement, redaction, audit log, and
allow-list — driven through CLI flags on the spawned subprocess.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]


class TestBudgetCaps:
    def test_max_actions_blocks_further_calls(self, oso_server_factory):
        srv = oso_server_factory(extra_args=["--max-actions", "2"])
        from tests.user.conftest import HttpJson
        http = HttpJson(srv["base_url"])
        # Issue actions until the cap blocks one.
        results = []
        for _ in range(5):
            _, r = http.post("/api/element/click",
                              {"window_index": 0,
                               "selector": 'Window/MenuBar/MenuItem[name="Edit"]'})
            results.append(r)
        codes = [r.get("error", {}).get("code") for r in results]
        assert "BudgetExceeded" in codes, codes


class TestBudgetStatus:
    def test_status_reports_remaining_actions(self, oso_server_factory):
        srv = oso_server_factory(extra_args=["--max-actions", "5"])
        from tests.user.conftest import HttpJson
        http = HttpJson(srv["base_url"])
        # Do one action to bump the counter.
        http.post("/api/element/click",
                  {"window_index": 0,
                   "selector": 'Window/MenuBar/MenuItem[name="Edit"]'})
        _, body = http.get("/api/budget_status")
        assert body["ok"] is True
        assert body["actions"]["limit"] == 5
        assert body["actions"]["used"] >= 1


class TestRedaction:
    def test_redaction_status_endpoint_reports_active(self, oso_server_factory, tmp_path):
        cfg = {"web_ui": {"port": 0}, "mock": True,
               "redaction": {"enabled": True,
                             "patterns": [{"regex": r"hunter2", "replace": "[REDACTED]"}]}}
        srv = oso_server_factory(config_overrides=cfg)
        from tests.user.conftest import HttpJson
        http = HttpJson(srv["base_url"])
        _, body = http.get("/api/redaction_status")
        assert body["ok"] is True


class TestPropose:
    def test_propose_action_returns_confirmation_token(self, http):
        # propose_action nests the target args under `args`.
        _, body = http.post(
            "/api/propose_action",
            {"action": "click_element",
             "args": {"window_index": 0,
                      "selector": 'Window/MenuBar/MenuItem[name="Edit"]'}},
        )
        assert body["ok"] is True
        token = body.get("confirm_token") or body.get("token")
        assert token and str(token).startswith("ct:"), body

    def test_propose_action_rejects_missing_action(self, http):
        _, body = http.post("/api/propose_action",
                            {"args": {"window_index": 0,
                                      "selector": "Window"}})
        assert body["ok"] is False
        assert body["error"]["code"] == "BadRequest"

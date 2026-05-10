"""Integration tests for P5 tools (budgets, redaction, audit, propose_action)."""
from __future__ import annotations

import os
import tempfile

import pytest


def test_budget_caps_actions(client, config):
    from budgets import BudgetStore
    from session import get_session
    get_session().budgets = BudgetStore(max_actions=2)

    ws = client.get("/api/windows").get_json()
    uid = ws["windows"][0]["window_uid"]
    r1 = client.post("/api/element/click", json={
        "window_uid": uid,
        "selector": 'Window/MenuBar/MenuItem[name="File"]',
    }).get_json()
    r2 = client.post("/api/element/click", json={
        "window_uid": uid,
        "selector": 'Window/MenuBar/MenuItem[name="Edit"]',
    }).get_json()
    r3 = client.post("/api/element/click", json={
        "window_uid": uid,
        "selector": 'Window/MenuBar/MenuItem[name="View"]',
    }).get_json()
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r3["ok"] is False
    assert r3["error"]["code"] == "BudgetExceeded"


def test_budget_status(client):
    from budgets import BudgetStore
    from session import get_session
    get_session().budgets = BudgetStore(max_actions=5)
    s = client.get("/api/budget_status").get_json()
    assert s["ok"] is True
    assert s["actions"]["limit"] == 5


def test_redaction(client, config):
    from redaction import Redactor
    from session import get_session
    config["redaction"] = {"enabled": True,
                            "element_name_patterns": ["scroll"]}
    get_session().redactor = Redactor(config)
    r = client.get("/api/structure?window_index=0").get_json()
    assert r["ok"] is True
    # We don't assert on exact content but redaction status should reflect activity
    rs = client.get("/api/redaction_status").get_json()
    assert rs["active"] is True
    assert rs["patterns_count"] >= 1


def test_allowlist_denies_action(client, config):
    config["actions"] = {"deny": ["type_text"]}
    r = client.post("/api/action",
                    json={"action": "type", "value": "x"}).get_json()
    assert r["ok"] is False
    assert r["error"]["code"] == "PermissionDenied"


def test_audit_log_written(client, config, tmp_path):
    from audit import AuditLogger
    from session import get_session
    config["logging"] = {"audit": True,
                          "audit_path": str(tmp_path / "audit.log"),
                          "level": "INFO"}
    au = AuditLogger.from_config(config)
    assert au is not None
    get_session().auditor = au
    client.get("/api/windows")
    log = (tmp_path / "audit.log").read_text()
    assert "tool=list_windows" in log


def test_propose_action_returns_token(client):
    ws = client.get("/api/windows").get_json()
    uid = ws["windows"][0]["window_uid"]
    r = client.post("/api/propose_action", json={
        "action": "click_element",
        "args": {"window_uid": uid,
                 "selector": 'Window/MenuBar/MenuItem[name="File"]'},
    }).get_json()
    assert r["ok"] is True
    assert r["confirm_token"].startswith("ct:")
    assert "would_target" in r

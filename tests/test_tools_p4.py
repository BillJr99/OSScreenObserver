"""Integration tests for P4 tools (trace, replay, scenarios, oracles)."""
from __future__ import annotations

import os
import tempfile
import shutil

import pytest


@pytest.fixture()
def trace_tmp():
    d = tempfile.mkdtemp(prefix="oso-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_trace_lifecycle(client, observer, config, trace_tmp):
    config["tracing"] = {"dir": trace_tmp, "screenshot_every_n_actions": 0}
    ts = client.post("/api/trace/start", json={"label": "t1"}).get_json()
    assert ts["ok"] is True
    assert ts["trace_id"].startswith("trace-")

    # A traced call.
    client.get("/api/windows").get_json()

    status = client.get("/api/trace/status").get_json()
    assert status["active_trace_id"] == ts["trace_id"]
    assert status["step_count"] >= 1

    stop = client.post("/api/trace/stop").get_json()
    assert stop["ok"] is True
    assert stop["step_count"] >= 1
    assert os.path.exists(stop["path"])


def test_scenario_load_and_assert(client, observer):
    sc = client.post("/api/scenario/load",
                     json={"path": "scenarios_examples/login.yaml"}).get_json()
    assert sc["ok"] is True
    assert sc["scenario"] == "login-happy-path"

    # Pre-state assertion fails (no welcome message yet).
    pre = client.post("/api/assert_state", json={
        "predicate": [{"kind": "text_visible", "regex": "Hello, alice"}],
    }).get_json()
    assert pre["ok"] is True
    assert pre["all_passed"] is False


def test_full_scenario_round_trip(client, observer, config, trace_tmp):
    config["tracing"] = {"dir": trace_tmp, "screenshot_every_n_actions": 0}
    client.post("/api/scenario/load",
                json={"path": "scenarios_examples/login.yaml"})
    ts = client.post("/api/trace/start").get_json()
    trace_dir = ts["dir"]

    ws = client.get("/api/windows").get_json()
    uid = ws["windows"][0]["window_uid"]

    fe = client.get(
        f'/api/find_element?window_uid={uid}'
        '&selector=Window/Edit[name="Username"]'
    ).get_json()
    client.post("/api/element/click",
                json={"window_uid": uid, "element_id": fe["element_id"]})
    client.post("/api/action", json={"action": "type", "value": "alice"})

    fe2 = client.get(
        f'/api/find_element?window_uid={uid}'
        '&selector=Window/Edit[name="Password"]'
    ).get_json()
    client.post("/api/element/click",
                json={"window_uid": uid, "element_id": fe2["element_id"]})
    client.post("/api/action", json={"action": "type", "value": "hunter2"})

    fe3 = client.get(
        f'/api/find_element?window_uid={uid}'
        '&selector=Window/Button[name="Login"]'
    ).get_json()
    client.post("/api/element/click",
                json={"window_uid": uid, "element_id": fe3["element_id"]})

    post = client.post("/api/assert_state", json={
        "predicate": [{"kind": "text_visible", "regex": "Hello, alice"}],
    }).get_json()
    assert post["all_passed"] is True

    stop = client.post("/api/trace/stop").get_json()
    assert stop["step_count"] >= 8

    # Replay verify with state reset.
    client.post("/api/scenario/load",
                json={"path": "scenarios_examples/login.yaml"})
    rs = client.post("/api/replay/start",
                     json={"path": trace_dir, "mode": "verify"}).get_json()
    rid = rs["replay_id"]
    div_count = 0
    while True:
        rep = client.post("/api/replay/step",
                          json={"replay_id": rid}).get_json()
        if rep.get("divergence"):
            div_count += 1
        if rep["finished"]:
            break
    assert div_count == 0


def test_oracle_unknown_kind(client):
    r = client.post("/api/assert_state", json={
        "predicate": [{"kind": "bogus_predicate"}],
    }).get_json()
    assert r["ok"] is True
    assert r["all_passed"] is False
    assert r["results"][0]["passed"] is False


def test_oracle_screenshot_similar_unsupported_surfaces_error_code(client,
                                                                   monkeypatch):
    """When scikit-image isn't installed, screenshot_similar must mark the
    predicate as PredicateUnsupported so callers can branch on it."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("skimage"):
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = client.post("/api/assert_state", json={
        "predicate": [{"kind": "screenshot_similar",
                       "reference_path": "/dev/null"}],
    }).get_json()
    assert r["ok"] is True
    assert r["all_passed"] is False
    entry = r["results"][0]
    assert entry["passed"] is False
    assert entry.get("error_code") == "PredicateUnsupported"
    assert isinstance(entry["observed"], dict)
    assert entry["observed"].get("unsupported") is True

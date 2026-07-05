"""[P2] Secure-defaults tests: loopback bind default, opt-in remote bind,
and config-driven CORS behavior.

The HTTP API is unauthenticated by design (no auth is added — binding
policy is the security control):
  - built-in default bind must be loopback (127.0.0.1);
  - --host / web_ui.host remain the explicit opt-in path to 0.0.0.0;
  - non-loopback binds must produce a prominent startup warning;
  - no CORS headers are sent unless web_ui.cors_origins is configured.
"""
from __future__ import annotations

import copy
import json

import main as _main
from main import _DEFAULT_CONFIG, bind_warning, load_config


# ── Bind default resolution ──────────────────────────────────────────────────


def test_builtin_default_bind_is_loopback():
    assert _DEFAULT_CONFIG["web_ui"]["host"] == "127.0.0.1"


def test_builtin_default_cors_origins_empty():
    assert _DEFAULT_CONFIG["web_ui"]["cors_origins"] == []


def test_load_config_without_host_key_resolves_loopback(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"web_ui": {"port": 6001}}))
    cfg = load_config(str(p))
    assert cfg["web_ui"]["host"] == "127.0.0.1"
    assert cfg["web_ui"]["port"] == 6001


def test_load_config_explicit_opt_in_preserved(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"web_ui": {"host": "0.0.0.0"}}))
    cfg = load_config(str(p))
    assert cfg["web_ui"]["host"] == "0.0.0.0"


def test_example_config_binds_loopback(tmp_path):
    # config.json.example is the bootstrap source of truth for first runs.
    import os
    example = os.path.join(os.path.dirname(_main.__file__),
                           "config.json.example")
    with open(example, encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["web_ui"]["host"] == "127.0.0.1"
    assert cfg["web_ui"]["cors_origins"] == []


# ── Non-loopback bind warning ────────────────────────────────────────────────


def test_bind_warning_none_for_loopback():
    assert bind_warning("127.0.0.1") is None
    assert bind_warning("localhost") is None
    assert bind_warning("::1") is None


def test_bind_warning_for_all_interfaces():
    w = bind_warning("0.0.0.0")
    assert w is not None
    assert "unauthenticated action API exposed to the network" in w


def test_bind_warning_for_lan_address():
    w = bind_warning("192.168.1.20")
    assert w is not None
    assert "192.168.1.20" in w


# ── CORS behavior via Flask test client ──────────────────────────────────────


def _make_client(cors_origins=None):
    from ascii_renderer import ASCIIRenderer
    from description import DescriptionGenerator
    from observer import ScreenObserver
    from web_inspector import create_web_app

    cfg = copy.deepcopy(_DEFAULT_CONFIG)
    cfg["mock"] = True
    if cors_origins is not None:
        cfg["web_ui"]["cors_origins"] = cors_origins
    app = create_web_app(ScreenObserver(cfg), ASCIIRenderer(cfg),
                         DescriptionGenerator(cfg), cfg)
    return app.test_client()


def test_no_cors_headers_by_default(fresh_session):
    client = _make_client()
    r = client.get("/api/windows", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200
    assert "Access-Control-Allow-Origin" not in r.headers


def test_no_cors_on_action_by_default(fresh_session):
    client = _make_client()
    r = client.post("/api/action",
                    json={"action": "click_at", "x": 10, "y": 10},
                    headers={"Origin": "http://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_no_cors_preflight_grant_on_action_by_default(fresh_session):
    client = _make_client()
    r = client.options(
        "/api/action",
        headers={"Origin": "http://evil.example",
                 "Access-Control-Request-Method": "POST"})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_cors_wildcard_opt_in(fresh_session):
    client = _make_client(cors_origins=["*"])
    r = client.get("/api/windows", headers={"Origin": "http://evil.example"})
    # flask-cors echoes the requesting origin for a "*" allowlist; either
    # form grants cross-origin access.
    assert r.headers.get("Access-Control-Allow-Origin") in (
        "*", "http://evil.example")


def test_cors_explicit_origin_allowed(fresh_session):
    client = _make_client(cors_origins=["http://localhost:3000"])
    r = client.get("/api/windows",
                   headers={"Origin": "http://localhost:3000"})
    assert (r.headers.get("Access-Control-Allow-Origin")
            == "http://localhost:3000")


def test_cors_explicit_origin_rejects_others(fresh_session):
    client = _make_client(cors_origins=["http://localhost:3000"])
    r = client.get("/api/windows", headers={"Origin": "http://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers

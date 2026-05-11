"""OCR config plumbing: ocr_util + load_config + healthz diagnostics."""
from __future__ import annotations

import json
import os


def test_ocr_util_configure_no_pytesseract(monkeypatch):
    """When pytesseract isn't importable, configure returns None safely."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pytesseract":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import importlib, ocr_util
    importlib.reload(ocr_util)
    assert ocr_util.configure({"ocr": {"tesseract_cmd": "/tmp/fake"}}) is None


def test_ocr_util_configure_applies_path(monkeypatch):
    import importlib, ocr_util
    importlib.reload(ocr_util)
    pyt = __import__("pytesseract")
    # Reset to known.
    pyt.pytesseract.tesseract_cmd = "tesseract"
    cmd = ocr_util.configure({"ocr": {"tesseract_cmd": "/usr/local/bin/tesseract"}})
    assert cmd == "/usr/local/bin/tesseract"
    assert pyt.pytesseract.tesseract_cmd == "/usr/local/bin/tesseract"


def test_ocr_util_configure_trims_quotes_and_env(monkeypatch):
    import importlib, ocr_util
    importlib.reload(ocr_util)
    monkeypatch.setenv("MYBIN", "/opt/bin")
    cmd = ocr_util.configure({"ocr": {"tesseract_cmd": '  "$MYBIN/tesseract" '}})
    assert cmd == "/opt/bin/tesseract"


def test_diagnose_when_path_missing(tmp_path):
    import importlib, ocr_util
    importlib.reload(ocr_util)
    fake = tmp_path / "definitely_not_here"
    d = ocr_util.diagnose({"ocr": {"tesseract_cmd": str(fake)}})
    assert d["pytesseract_installed"] is True
    assert d["configured_path"] == str(fake)
    assert d["configured_path_exists"] is False
    assert "does not exist" in (d["error"] or "")


def test_load_config_invalid_escape_recorded(tmp_path):
    """Unescaped Windows path in config should NOT silently fall back."""
    bad = tmp_path / "bad.json"
    bad.write_text('{"ocr": {"tesseract_cmd": "c:\\program files\\foo"}}')
    import importlib, main
    importlib.reload(main)
    cfg = main.load_config(str(bad))
    # Falls back to defaults so the process can still run.
    assert cfg == main._DEFAULT_CONFIG
    # But the error must be recorded for the healthz endpoint.
    status = main.config_load_status()
    assert status["config_path"] == str(bad)
    assert status["config_error"]
    assert "Invalid" in status["config_error"] or "escape" in status["config_error"]


def test_load_config_with_forward_slashes(tmp_path):
    """Forward-slash Windows paths are a valid workaround."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "ocr": {"tesseract_cmd": "c:/program files/tesseract-ocr/tesseract.exe"}
    }))
    import importlib, main
    importlib.reload(main)
    cfg = main.load_config(str(good))
    assert cfg["ocr"]["tesseract_cmd"].endswith("tesseract.exe")
    assert main.config_load_status()["config_error"] is None


def test_healthz_includes_diagnostics(client):
    r = client.get("/api/healthz").get_json()
    assert r["ok"] is True
    # ocr block surfaced regardless of whether tesseract is installed
    assert "ocr" in r
    assert "pytesseract_installed" in r["ocr"]


def test_install_hint_mentions_tesseract_cmd_and_escaping():
    """The shared hint must mention tesseract_cmd and JSON escaping."""
    from ocr_util import INSTALL_HINT
    assert "tesseract_cmd" in INSTALL_HINT
    assert "config.json" in INSTALL_HINT
    # Either the escaped-backslash form or the forward-slash form must be
    # spelled out so Windows users have a copy-pasteable fix.
    assert "\\\\" in INSTALL_HINT or "forward slashes" in INSTALL_HINT


def test_diagnose_error_includes_hint(tmp_path):
    """When the configured path doesn't exist, the hint is appended."""
    import importlib, ocr_util
    importlib.reload(ocr_util)
    d = ocr_util.diagnose({"ocr": {"tesseract_cmd": str(tmp_path / "no")}})
    assert d["error"] and "tesseract_cmd" in (d["error"] or "")
    assert "hint" in d


def test_ascii_renderer_applies_tesseract_cmd_from_config(tmp_path):
    """ASCIIRenderer must apply ocr.tesseract_cmd up-front so OCR overlay
    works even when no other module has touched pytesseract yet."""
    import importlib, ocr_util, ascii_renderer
    importlib.reload(ocr_util)
    # Construct a renderer with a known tesseract_cmd; it should be
    # applied to pytesseract immediately.
    target = str(tmp_path / "tesseract-test")
    ascii_renderer.ASCIIRenderer({
        "ascii_sketch": {"grid_width": 80, "grid_height": 20},
        "ocr": {"tesseract_cmd": target},
    })
    import pytesseract
    assert pytesseract.pytesseract.tesseract_cmd == target


def test_get_ocr_tool_error_includes_hint(client):
    """tools.get_ocr surfaces the install hint when the binary is missing."""
    # Configure a bogus tesseract_cmd so the call fails predictably.
    from main import _DEFAULT_CONFIG
    from observer import ScreenObserver
    from ascii_renderer import ASCIIRenderer
    from description import DescriptionGenerator
    from web_inspector import create_web_app
    from session import reset_session_for_tests
    reset_session_for_tests()
    cfg = dict(_DEFAULT_CONFIG)
    cfg["mock"] = True
    cfg["ocr"] = {"enabled": True, "tesseract_cmd": "/nope/tesseract"}
    app = create_web_app(ScreenObserver(cfg), ASCIIRenderer(cfg),
                         DescriptionGenerator(cfg), cfg)
    cl = app.test_client()
    r = cl.get("/api/ocr?window_index=0").get_json()
    assert r["ok"] is False
    # Either message or context.hint must mention tesseract_cmd.
    ctx = r["error"].get("context") or {}
    blob = " ".join([r["error"]["message"], str(ctx)])
    assert "tesseract_cmd" in blob

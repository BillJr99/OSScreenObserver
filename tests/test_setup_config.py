"""Tests for setup_config.py — config bootstrap and tesseract-path fixup."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import setup_config


def _read(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─── bootstrap_config ────────────────────────────────────────────────────────

def test_bootstrap_copies_example_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json.example").write_text('{"ocr": {}}', encoding="utf-8")
    assert not (tmp_path / "config.json").exists()
    setup_config.bootstrap_config()
    assert (tmp_path / "config.json").exists()
    assert _read(tmp_path / "config.json") == {"ocr": {}}


def test_bootstrap_leaves_existing_config_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text('{"existing": true}', encoding="utf-8")
    (tmp_path / "config.json.example").write_text('{"ocr": {}}', encoding="utf-8")
    setup_config.bootstrap_config()
    assert _read(tmp_path / "config.json") == {"existing": True}


def test_bootstrap_silent_when_example_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Neither file exists.
    setup_config.bootstrap_config()
    assert not (tmp_path / "config.json").exists()


# ─── fix_tesseract_path ──────────────────────────────────────────────────────

def _cfg(tmp_path, ocr=None):
    cfg = {"ocr": dict(ocr) if ocr else {}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def test_fix_path_no_op_when_configured_path_exists(tmp_path):
    real = tmp_path / "fake_tesseract"
    real.touch()
    path = _cfg(tmp_path, ocr={"tesseract_cmd": str(real)})
    with patch("setup_config._find_tesseract") as mock_find, \
         patch("setup_config._confirm") as mock_confirm:
        setup_config.fix_tesseract_path(path)
    mock_find.assert_not_called()
    mock_confirm.assert_not_called()
    assert _read(path)["ocr"]["tesseract_cmd"] == str(real)


def test_fix_path_no_op_when_unset_but_on_path(tmp_path):
    path = _cfg(tmp_path, ocr={"tesseract_cmd": None})
    with patch("setup_config._find_tesseract_on_path",
               return_value="/usr/bin/tesseract"), \
         patch("setup_config._confirm") as mock_confirm:
        setup_config.fix_tesseract_path(path)
    mock_confirm.assert_not_called()
    # Config unchanged.
    assert _read(path)["ocr"]["tesseract_cmd"] is None


def test_fix_path_updates_when_configured_path_broken(tmp_path):
    discovered = tmp_path / "real_tesseract"
    discovered.touch()
    path = _cfg(tmp_path, ocr={
        "tesseract_cmd": "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    })
    with patch("setup_config._find_tesseract", return_value=str(discovered)), \
         patch("setup_config._confirm", return_value=True):
        setup_config.fix_tesseract_path(path)
    assert _read(path)["ocr"]["tesseract_cmd"] == str(discovered)


def test_fix_path_respects_user_decline(tmp_path):
    discovered = tmp_path / "real_tesseract"
    discovered.touch()
    broken = "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    path = _cfg(tmp_path, ocr={"tesseract_cmd": broken})
    with patch("setup_config._find_tesseract", return_value=str(discovered)), \
         patch("setup_config._confirm", return_value=False):
        setup_config.fix_tesseract_path(path)
    # User said no — leave the broken path alone.
    assert _read(path)["ocr"]["tesseract_cmd"] == broken


def test_fix_path_no_op_when_tesseract_missing(tmp_path):
    path = _cfg(tmp_path, ocr={"tesseract_cmd": "/nonexistent/tesseract"})
    with patch("setup_config._find_tesseract", return_value=None), \
         patch("setup_config._confirm") as mock_confirm:
        setup_config.fix_tesseract_path(path)
    mock_confirm.assert_not_called()
    # Config unchanged — the launcher's install step already warned.
    assert _read(path)["ocr"]["tesseract_cmd"] == "/nonexistent/tesseract"


def test_fix_path_creates_ocr_section_if_missing(tmp_path):
    discovered = tmp_path / "real_tesseract"
    discovered.touch()
    p = tmp_path / "config.json"
    p.write_text('{"vlm": {"enabled": true}}', encoding="utf-8")
    with patch("setup_config._find_tesseract_on_path", return_value=None), \
         patch("setup_config._find_tesseract", return_value=str(discovered)), \
         patch("setup_config._confirm", return_value=True):
        setup_config.fix_tesseract_path(str(p))
    cfg = _read(p)
    assert cfg["ocr"]["tesseract_cmd"] == str(discovered)
    # Other sections preserved.
    assert cfg["vlm"] == {"enabled": True}


def test_fix_path_atomic_no_stray_tempfile(tmp_path):
    discovered = tmp_path / "real_tesseract"
    discovered.touch()
    path = _cfg(tmp_path, ocr={"tesseract_cmd": "/broken/path"})
    with patch("setup_config._find_tesseract", return_value=str(discovered)), \
         patch("setup_config._confirm", return_value=True):
        setup_config.fix_tesseract_path(path)
    # No leftover temp file from the atomic save.
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []

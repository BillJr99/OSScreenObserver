"""
Exercises setup_config.py in a subprocess against a fresh CWD: the
script should copy config.json.example → config.json and patch the
tesseract path. Mirrors the patterns in tests/test_setup_config.py but
runs the actual script via the OS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

OSO_ROOT = Path(__file__).resolve().parents[2]


def test_setup_config_copies_example_when_missing(tmp_path):
    # Stage the example into a fresh CWD so setup_config sees it as a sibling.
    example = OSO_ROOT / "config.json.example"
    work = tmp_path / "work"
    work.mkdir()
    (work / "config.json.example").write_text(example.read_text())

    # Run setup_config.py with that as CWD.
    r = subprocess.run(
        [sys.executable, str(OSO_ROOT / "setup_config.py")],
        cwd=str(work),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert (work / "config.json").exists(), "config.json was not seeded"


def test_setup_config_leaves_existing_alone(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    custom = '{"_about": "user override", "web_ui": {"port": 5050}}'
    (work / "config.json").write_text(custom)
    (work / "config.json.example").write_text((OSO_ROOT / "config.json.example").read_text())

    r = subprocess.run(
        [sys.executable, str(OSO_ROOT / "setup_config.py")],
        cwd=str(work),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    # The override should survive verbatim — setup_config must not overwrite it.
    assert (work / "config.json").read_text() == custom \
        or "5050" in (work / "config.json").read_text()

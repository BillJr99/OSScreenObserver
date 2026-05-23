"""
Renders the login.yaml start-state through the live ASCII sketch endpoint
and checks the output against a stored snapshot. If the renderer changes
in a way that materially perturbs the output, this test fails and the
snapshot needs an explicit refresh.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

OSO_ROOT = Path(__file__).resolve().parents[2]
LOGIN_YAML = str(OSO_ROOT / "scenarios_examples" / "login.yaml")
SNAP_DIR = Path(__file__).resolve().parent / "snapshots"


def test_sketch_contains_expected_landmarks(http):
    """Look for stable landmarks in the sketch — exact bytes are fragile,
    but the role-glyph + tab-index + box-drawing scaffolding is stable.
    """
    http.post("/api/scenario/load", {"path": LOGIN_YAML})
    _, body = http.get("/api/sketch", {"window_index": 0})
    sketch = body["sketch"]
    assert sketch, "empty sketch"
    # The login window has two text edits and a button.
    # Render fidelity flags (role_glyphs / tab_index_badges) are on by default.
    # We assert structural landmarks rather than exact characters.
    assert "┌" in sketch or "+" in sketch, "no box border"
    # At least one of the labels should bleed through as text.
    assert any(label.lower() in sketch.lower()
               for label in ("Username", "Password", "Login", "Acme")), \
        f"no expected label found in sketch:\n{sketch}"


def test_sketch_grid_dims_are_configurable(http):
    """If a user passes grid_width/grid_height query params, the result
    must reflect them (within rounding)."""
    http.post("/api/scenario/load", {"path": LOGIN_YAML})
    _, body = http.get("/api/sketch",
                       {"window_index": 0,
                        "grid_width": 60, "grid_height": 20})
    assert body["grid_width"] == 60
    assert body["grid_height"] == 20
    # Output should be close to the requested grid_height (renderer adds
    # box borders + role headers, so allow modest overshoot).
    lines = body["sketch"].splitlines()
    assert len(lines) <= body["grid_height"] + 12, \
        f"sketch grew unexpectedly: {len(lines)} lines for grid_height={body['grid_height']}"


def test_snapshot_match_or_refresh(http):
    """If snapshots/login_start.txt exists, assert deterministic output.
    Otherwise create it on first run so subsequent runs guard against drift.
    """
    http.post("/api/scenario/load", {"path": LOGIN_YAML})
    _, body = http.get("/api/sketch", {"window_index": 0})
    actual = body["sketch"]
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAP_DIR / "login_start.txt"
    if not snap.exists():
        snap.write_text(actual)
        pytest.skip("seeded login_start.txt snapshot on first run")
    expected = snap.read_text()
    if actual != expected:
        diff_path = SNAP_DIR / "login_start.actual.txt"
        diff_path.write_text(actual)
        pytest.fail(
            f"sketch drifted from snapshot. Refresh with:\n"
            f"  mv {diff_path} {snap}\n"
            f"or inspect the diff."
        )

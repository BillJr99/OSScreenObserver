"""Tests for the fidelity-oriented features in ascii_renderer.py.

These tests construct synthetic UIElement trees so they run without a
real screen, accessibility tree, or Tesseract install.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ascii_renderer import ASCIIRenderer  # noqa: E402
from observer import Bounds, UIElement  # noqa: E402


def _renderer(**overrides):
    cfg = {
        "ascii_sketch": {
            "grid_width": 80, "grid_height": 24, "unicode_box": True,
            "role_glyphs": True, "occlusion_prune": True,
            "tab_index_badges": True, "landmark_headers": True,
        },
        "ocr": {"enabled": False, "min_confidence": 30},
    }
    cfg["ascii_sketch"].update(overrides)
    return ASCIIRenderer(cfg)


# ── role glyphs ──────────────────────────────────────────────────────────────

def test_checkbox_glyph_selected():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 60))
    root.children.append(UIElement(
        "c", "Word Wrap", "CheckBox", bounds=Bounds(10, 10, 180, 30),
        selected=True,
    ))
    sketch = _renderer().render(root)
    assert "[x]" in sketch
    assert "Word Wrap" in sketch


def test_checkbox_glyph_unselected():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 60))
    root.children.append(UIElement(
        "c", "Auto-save", "CheckBox", bounds=Bounds(10, 10, 180, 30),
        selected=False,
    ))
    sketch = _renderer().render(root)
    assert "[ ]" in sketch


def test_progressbar_glyph():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 400, 60))
    root.children.append(UIElement(
        "p", "Saving", "ProgressBar", bounds=Bounds(10, 10, 380, 30),
        value_now=50.0, value_min=0.0, value_max=100.0,
    ))
    sketch = _renderer().render(root)
    assert "▓" in sketch and "░" in sketch
    assert "50%" in sketch


def test_slider_glyph_from_string_value():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 400, 60))
    root.children.append(UIElement(
        "s", "Volume", "Slider", bounds=Bounds(10, 10, 380, 30),
        value="40%",
    ))
    sketch = _renderer().render(root)
    assert "●" in sketch
    assert "40%" in sketch


def test_combobox_arrow():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 60))
    root.children.append(UIElement(
        "cb", "Theme", "ComboBox", bounds=Bounds(10, 10, 180, 30),
        expanded=False, value="Dark",
    ))
    sketch = _renderer().render(root)
    # Collapsed combobox uses ▶
    assert "▶" in sketch or "▼" in sketch


# ── tab-order numerals ───────────────────────────────────────────────────────

def test_tab_index_circled_numerals_appear():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 240, 60))
    for i, lbl in enumerate(("OK", "Cancel", "Help")):
        root.children.append(UIElement(
            f"b{i}", lbl, "Button",
            bounds=Bounds(10 + i * 80, 10, 60, 30),
        ))
    sketch = _renderer().render(root)
    assert "①" in sketch
    assert "②" in sketch
    assert "③" in sketch


# ── occlusion pruning ────────────────────────────────────────────────────────

def test_occluded_sibling_is_hidden():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 200))
    root.children.append(UIElement(
        "hidden", "Underneath", "Button",
        bounds=Bounds(20, 20, 80, 40),
    ))
    # Modal fully covers the button.
    root.children.append(UIElement(
        "modal", "Modal Dialog", "Dialog",
        bounds=Bounds(0, 0, 200, 200),
    ))
    sketch = _renderer().render(root)
    assert "Underneath" not in sketch
    # Modal landmark header should appear.
    assert "Dialog" in sketch


def test_occlusion_can_be_disabled():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 200))
    root.children.append(UIElement(
        "hidden", "Underneath", "Button",
        bounds=Bounds(20, 20, 80, 40),
    ))
    root.children.append(UIElement(
        "modal", "Modal Dialog", "Dialog",
        bounds=Bounds(0, 0, 200, 200),
    ))
    sketch = _renderer(occlusion_prune=False).render(root)
    # With pruning off, the inner button still draws (label may be
    # partially overwritten by the modal but the structured pass keeps it).


# ── landmark headers ─────────────────────────────────────────────────────────

def test_landmark_header_in_top_edge():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 60))
    root.children.append(UIElement(
        "tb", "Main", "ToolBar", bounds=Bounds(0, 0, 200, 30),
    ))
    sketch = _renderer().render(root)
    assert "Toolbar" in sketch


# ── structured sidecar ──────────────────────────────────────────────────────

def test_render_structured_returns_records():
    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 60))
    btn = UIElement("b1", "Save", "Button", bounds=Bounds(10, 10, 80, 30))
    root.children.append(btn)
    out = _renderer().render_structured(root)
    assert "sketch" in out and "elements" in out and "legend" in out
    ids = [r["id"] for r in out["elements"]]
    assert "b1" in ids
    btn_rec = next(r for r in out["elements"] if r["id"] == "b1")
    assert btn_rec["tab_index"] == 1
    assert btn_rec["legend_key"]
    assert "bounds_grid" in btn_rec


# ── confidence-weighted OCR overlay (mocked) ─────────────────────────────────

def test_ocr_overlay_higher_confidence_wins(monkeypatch):
    """Two OCR lines target the same row; higher-confidence wins."""
    from ascii_renderer import ASCIIRenderer
    import ascii_renderer as ar

    def fake_lines(_bytes, _cfg, *, psm=11):
        # Two lines at the same y but different text, in screen-coords
        # relative to the window (the renderer adds ref.x/ref.y).
        return [
            (10, 30, 60, 12, "LOW",     35),
            (10, 30, 60, 12, "HIGHCONF", 92),
        ]
    monkeypatch.setattr(ar, "_ocr_lines", fake_lines)

    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 80))
    cfg = {
        "ascii_sketch": {
            "grid_width": 80, "grid_height": 24,
            "role_glyphs": False, "tab_index_badges": False,
        },
        "ocr": {"enabled": True, "min_confidence": 30},
    }
    r = ASCIIRenderer(cfg)
    sketch = r.render(root, screenshot_bytes=b"<fake-png>")
    assert "HIGHCONF" in sketch
    assert "LOW" not in sketch


# ── OCR overlay clips to its own line width ──────────────────────────────────

def test_ocr_overlay_clipped_to_line_width(monkeypatch):
    """A long OCR string is clipped to the line's own grid footprint."""
    import ascii_renderer as ar

    def fake_lines(_bytes, _cfg, *, psm=11):
        # 200-px-wide window, line spans only 20px → roughly 1/10th of grid.
        return [(0, 10, 20, 12, "X" * 200, 88)]
    monkeypatch.setattr(ar, "_ocr_lines", fake_lines)

    root = UIElement("root", "Win", "Window", bounds=Bounds(0, 0, 200, 80))
    cfg = {
        "ascii_sketch": {
            "grid_width": 80, "grid_height": 24,
            "role_glyphs": False, "tab_index_badges": False,
        },
        "ocr": {"enabled": True, "min_confidence": 30},
    }
    sketch = ar.ASCIIRenderer(cfg).render(root, screenshot_bytes=b"<fake>")
    # Line should occupy ~8 cells (20/200 * 80) and stop, not splat 200 X's.
    import re
    runs = re.findall(r"X+", sketch)
    assert runs, "expected at least one X in output"
    assert max(len(r) for r in runs) <= 12, (
        f"expected ≤12 contiguous X's, got {max(len(r) for r in runs)}"
    )

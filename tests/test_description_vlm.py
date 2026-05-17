"""Tests for the VLM action in description.py.

Covers the pieces that don't require a live Ollama endpoint:

  * _tolerant_json_loads — fenced/garbage/partial input.
  * _prepare_image       — downscale math and the no-op fast paths.
  * _build_context_blocks — block assembly and the ``ground_with_*`` gates.
  * from_vlm              — grounded single-shot, with urllib.request mocked.
  * from_vlm_multipass    — pass ordering, image reuse on Pass 3, and
                            graceful pass-failure handling.
"""
from __future__ import annotations

import io
import json
from typing import Any, Dict, List
from unittest.mock import patch, MagicMock

import pytest

from description import (
    DescriptionGenerator,
    _tolerant_json_loads,
)
from observer import Bounds, UIElement


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _elem(role, name="", focused=False, children=None, **kw) -> UIElement:
    return UIElement(
        element_id=kw.get("element_id", f"id-{role}"),
        name=name, role=role,
        value=kw.get("value"),
        bounds=kw.get("bounds", Bounds(0, 0, 100, 100)),
        enabled=kw.get("enabled", True),
        focused=focused,
        keyboard_shortcut=kw.get("keyboard_shortcut"),
        description=kw.get("description"),
        children=children or [],
    )


def _png_bytes(w=200, h=100) -> bytes:
    """Generate a real PNG so Pillow operations work."""
    from PIL import Image
    img = Image.new("RGB", (w, h), color=(120, 200, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def cfg_vlm() -> dict:
    return {
        "vlm": {
            "enabled": True,
            "base_url": "http://localhost:11434",
            "api_key": None,
            "model": "qwen2.5vl:7b",
            "model_fast": "qwen2.5vl:3b",
            "max_tokens": 800,
            "temperature": 0.1,
            "timeout_s": 30,
            "mode": "multipass",
            "ground_with_tree":   True,
            "ground_with_ocr":    False,
            "ground_with_sketch": False,
            "ground_with_focus":  True,
            "tree_max_lines":     20,
        },
        "ocr":  {"enabled": False},
    }


# ─── _tolerant_json_loads ────────────────────────────────────────────────────

def test_tolerant_json_loads_plain():
    obj, err = _tolerant_json_loads('{"app": "VS Code", "controls": []}')
    assert err is None
    assert obj == {"app": "VS Code", "controls": []}


def test_tolerant_json_loads_strips_json_fence():
    raw = '```json\n{"app": "VS Code"}\n```'
    obj, err = _tolerant_json_loads(raw)
    assert err is None and obj == {"app": "VS Code"}


def test_tolerant_json_loads_strips_bare_fence():
    raw = '```\n{"a": 1}\n```'
    obj, err = _tolerant_json_loads(raw)
    assert err is None and obj == {"a": 1}


def test_tolerant_json_loads_salvages_from_prose():
    raw = "Here is the JSON you asked for:\n{\"app\": \"Slack\"}\nthanks!"
    obj, err = _tolerant_json_loads(raw)
    assert err is None and obj == {"app": "Slack"}


def test_tolerant_json_loads_rejects_non_object():
    obj, err = _tolerant_json_loads("[1, 2, 3]")
    assert obj is None
    assert err and "not an object" in err.lower()


def test_tolerant_json_loads_garbage_returns_error():
    obj, err = _tolerant_json_loads("definitely not json")
    assert obj is None
    assert err  # non-empty error message


def test_tolerant_json_loads_empty():
    obj, err = _tolerant_json_loads("")
    assert obj is None and err


def test_tolerant_json_loads_none():
    obj, err = _tolerant_json_loads(None)  # type: ignore[arg-type]
    assert obj is None and err


# ─── _prepare_image ──────────────────────────────────────────────────────────

def test_prepare_image_passes_through_when_small():
    src = _png_bytes(800, 600)
    out = DescriptionGenerator._prepare_image(src, max_dim=1600)
    assert out is src   # no-op fast path returns the same object


def test_prepare_image_downscales_long_edge_to_max_dim():
    src = _png_bytes(3200, 1600)
    out = DescriptionGenerator._prepare_image(src, max_dim=1600)
    assert out is not src
    from PIL import Image
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 1600
    # Aspect ratio preserved (2:1 → 1600x800).
    assert img.size == (1600, 800)


def test_prepare_image_zero_max_dim_passes_through():
    src = _png_bytes(3200, 1600)
    out = DescriptionGenerator._prepare_image(src, max_dim=0)
    assert out is src


def test_prepare_image_empty_input():
    out = DescriptionGenerator._prepare_image(b"", max_dim=1600)
    assert out == b""


# ─── _build_context_blocks ───────────────────────────────────────────────────

def test_build_context_blocks_emits_tree_and_focus(cfg_vlm):
    gen = DescriptionGenerator(cfg_vlm)
    root = _elem("Window", "Editor", children=[
        _elem("Button", "OK", focused=True),
        _elem("Button", "Cancel"),
    ])
    out = gen._build_context_blocks(root, None, None)
    assert "<ACCESSIBILITY_TREE>" in out
    assert "</ACCESSIBILITY_TREE>" in out
    assert "<FOCUSED_ELEMENT>" in out
    assert "OK" in out  # focused element name surfaces


def test_build_context_blocks_omits_blocks_when_flags_off(cfg_vlm):
    cfg_vlm["vlm"]["ground_with_tree"]  = False
    cfg_vlm["vlm"]["ground_with_focus"] = False
    gen = DescriptionGenerator(cfg_vlm)
    root = _elem("Window", focused=True)
    out = gen._build_context_blocks(root, None, None)
    assert "<ACCESSIBILITY_TREE>" not in out
    assert "<FOCUSED_ELEMENT>"     not in out


def test_build_context_blocks_truncates_tree(cfg_vlm):
    cfg_vlm["vlm"]["tree_max_lines"] = 3
    gen = DescriptionGenerator(cfg_vlm)
    # Build a long tree.
    kids = [_elem("Button", f"B{i}") for i in range(40)]
    root = _elem("Window", "many", children=kids)
    out = gen._build_context_blocks(root, None, None)
    assert "tree truncated" in out


def test_build_context_blocks_handles_no_root(cfg_vlm):
    gen = DescriptionGenerator(cfg_vlm)
    out = gen._build_context_blocks(None, None, None)
    assert out == ""


# ─── _post_vlm transport (mocked HTTP) ───────────────────────────────────────

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body
    def __enter__(self):     return self
    def __exit__(self, *_):  return False
    def read(self):          return self._body


def _mk_resp(content: str) -> _FakeResp:
    return _FakeResp(json.dumps(
        {"choices": [{"message": {"content": content}}]}
    ).encode("utf-8"))


def test_post_vlm_returns_assistant_text(cfg_vlm):
    gen = DescriptionGenerator(cfg_vlm)
    captured: Dict[str, Any] = {}
    def fake_open(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _mk_resp("hello")
    fake_opener = MagicMock()
    fake_opener.open.side_effect = fake_open
    with patch("description.urllib.request.build_opener", return_value=fake_opener):
        out = gen._post_vlm("hi", _png_bytes())
    assert out == "hello"
    assert captured["body"]["model"] == "qwen2.5vl:7b"
    assert captured["body"]["temperature"] == 0.1
    # Image was attached.
    contents = captured["body"]["messages"][0]["content"]
    assert any(c.get("type") == "image_url" for c in contents)


def test_post_vlm_returns_none_when_model_unset(cfg_vlm):
    cfg_vlm["vlm"]["model"] = None
    gen = DescriptionGenerator(cfg_vlm)
    assert gen._post_vlm("hi", None) is None


# ─── from_vlm (single-shot, grounded) ────────────────────────────────────────

def test_from_vlm_single_attaches_grounding(cfg_vlm):
    cfg_vlm["vlm"]["mode"] = "single"
    gen = DescriptionGenerator(cfg_vlm)
    root = _elem("Window", "Editor", children=[_elem("Button", "OK")])

    captured: Dict[str, Any] = {}
    def fake_open(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _mk_resp("structured output")
    fake_opener = MagicMock()
    fake_opener.open.side_effect = fake_open
    with patch("description.urllib.request.build_opener", return_value=fake_opener):
        out = gen.from_vlm(_png_bytes(), root=root)
    assert out == "structured output"
    text_block = next(c for c in captured["body"]["messages"][0]["content"]
                      if c.get("type") == "text")["text"]
    assert "<ACCESSIBILITY_TREE>" in text_block


def test_from_vlm_disabled_returns_none(cfg_vlm):
    cfg_vlm["vlm"]["enabled"] = False
    gen = DescriptionGenerator(cfg_vlm)
    assert gen.from_vlm(_png_bytes()) is None


# ─── from_vlm_multipass — pass ordering and image reuse ──────────────────────

def test_multipass_runs_three_passes_in_order(cfg_vlm):
    gen = DescriptionGenerator(cfg_vlm)
    root = _elem("Window", "App", children=[_elem("Button", "OK", focused=True)])

    calls: List[Dict[str, Any]] = []
    def fake_open(req, timeout=0):
        body = json.loads(req.data.decode("utf-8"))
        contents = body["messages"][0]["content"]
        has_image = any(c.get("type") == "image_url" for c in contents)
        text = next(c for c in contents if c.get("type") == "text")["text"]
        calls.append({"model": body["model"], "has_image": has_image,
                      "text_head": text[:80]})
        n = len(calls)
        # Pass 1 (scene), Pass 2 (controls), Pass 3 (actions).
        if n == 1:
            content = '{"app": "VS Code", "screen_type": "code-editor", ' \
                      '"primary_task": "Editing"}'
        elif n == 2:
            content = '{"focused": {"role": "button", "name": "OK"}, ' \
                      '"modal_open": false, "controls": ' \
                      '[{"role": "button", "name": "OK"}]}'
        else:
            content = '{"next_actions": [{"description": "Click OK", ' \
                      '"target_selector": "//button[@name=\'OK\']", ' \
                      '"rationale": "primary action", "risk": "low"}]}'
        return _mk_resp(content)

    fake_opener = MagicMock()
    fake_opener.open.side_effect = fake_open
    with patch("description.urllib.request.build_opener", return_value=fake_opener):
        env = gen.from_vlm_multipass(_png_bytes(), root=root)

    assert env is not None
    # Three passes, in order.
    assert len(calls) == 3
    assert calls[0]["model"] == "qwen2.5vl:3b"          # fast for Pass 1
    assert calls[1]["model"] == "qwen2.5vl:7b"          # primary for Pass 2
    assert calls[2]["model"] == "qwen2.5vl:7b"          # fallback for Pass 3
    # Image attached for Pass 1 + 2 only (Pass 3 is text-only).
    assert [c["has_image"] for c in calls] == [True, True, False]

    # Envelope merges fields from all three passes.
    assert env["app"]          == "VS Code"
    assert env["screen_type"]  == "code-editor"
    assert env["focused"]      == {"role": "button", "name": "OK"}
    assert env["modal_open"]   is False
    assert len(env["controls"]) == 1
    assert len(env["next_actions"]) == 1
    # Timing markers populated.
    assert env["_passes"]["scene_ms"]    >= 0
    assert env["_passes"]["controls_ms"] >= 0
    assert env["_passes"]["actions_ms"]  >= 0


def test_multipass_tolerates_failed_pass(cfg_vlm):
    """A garbled response from one pass leaves null fields, never aborts."""
    gen = DescriptionGenerator(cfg_vlm)
    root = _elem("Window", "App", children=[_elem("Button", "OK")])

    call_n = {"n": 0}
    def fake_open(req, timeout=0):
        call_n["n"] += 1
        # Pass 1 returns garbage; later passes return valid JSON.
        if call_n["n"] == 1:
            return _mk_resp("not json at all, sorry")
        if call_n["n"] == 2:
            return _mk_resp('{"controls": [{"role": "button"}]}')
        return _mk_resp('{"next_actions": []}')

    fake_opener = MagicMock()
    fake_opener.open.side_effect = fake_open
    with patch("description.urllib.request.build_opener", return_value=fake_opener):
        env = gen.from_vlm_multipass(_png_bytes(), root=root)
    assert env is not None
    # Scene pass failed → fields stay None, but envelope is still returned
    # and the later passes still landed their fields.
    assert env["app"] is None
    assert env["_passes"].get("scene_error")
    assert env["controls"] == [{"role": "button"}]


def test_multipass_returns_none_when_disabled(cfg_vlm):
    cfg_vlm["vlm"]["enabled"] = False
    gen = DescriptionGenerator(cfg_vlm)
    assert gen.from_vlm_multipass(_png_bytes()) is None


# ─── combined() routes to single vs multipass ────────────────────────────────

def test_combined_multipass_exposes_structured(cfg_vlm):
    gen = DescriptionGenerator(cfg_vlm)
    root = _elem("Window", "App", children=[_elem("Button", "OK")])

    def fake_open(req, timeout=0):
        return _mk_resp('{"app": "VS Code"}')
    fake_opener = MagicMock()
    fake_opener.open.side_effect = fake_open
    with patch("description.urllib.request.build_opener", return_value=fake_opener):
        out = gen.combined(root, _png_bytes())
    assert "vlm" in out
    assert "vlm_structured" in out
    assert isinstance(out["vlm_structured"], dict)
    # The string form is JSON-parseable.
    json.loads(out["vlm"])


def test_combined_single_mode_no_structured(cfg_vlm):
    cfg_vlm["vlm"]["mode"] = "single"
    gen = DescriptionGenerator(cfg_vlm)
    root = _elem("Window", "App")

    def fake_open(req, timeout=0):
        return _mk_resp("plain prose response")
    fake_opener = MagicMock()
    fake_opener.open.side_effect = fake_open
    with patch("description.urllib.request.build_opener", return_value=fake_opener):
        out = gen.combined(root, _png_bytes())
    assert out.get("vlm") == "plain prose response"
    assert "vlm_structured" not in out

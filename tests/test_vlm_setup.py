"""Tests for vlm_setup.ensure_vlm_model and save_model_to_config.

The picker is a TTY interaction, so these tests cover only the
deterministic branches:

  * Non-interactive mode (mcp/both) disables VLM when vlm.model is unset.
  * A pre-set vlm.model is left alone.
  * vlm.enabled=false short-circuits entirely.
  * save_model_to_config persists atomically and preserves other keys
    (including UTF-8 prompt text).
"""

import json
import os

import vlm_setup


def _cfg(tmp_path, **vlm):
    base = {
        "web_ui": {"host": "0.0.0.0", "port": 5001},
        "vlm": {"enabled": False, "base_url": "http://localhost:3000",
                "api_key": None, "model": None, "max_tokens": 1500,
                "prompt": "Describe — naïvely 😀"},   # exercise UTF-8
    }
    base["vlm"].update(vlm)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    return base, str(path)


def test_disabled_short_circuits(tmp_path):
    cfg, path = _cfg(tmp_path, enabled=False)
    vlm_setup.ensure_vlm_model(cfg, path, interactive_ok=True)
    assert cfg["vlm"]["enabled"] is False
    assert cfg["vlm"]["model"] is None


def test_model_already_set_is_preserved(tmp_path):
    cfg, path = _cfg(tmp_path, enabled=True, model="anthropic/claude-3-5-sonnet")
    vlm_setup.ensure_vlm_model(cfg, path, interactive_ok=True)
    assert cfg["vlm"]["enabled"] is True
    assert cfg["vlm"]["model"] == "anthropic/claude-3-5-sonnet"


def test_non_interactive_disables_when_model_missing(tmp_path):
    cfg, path = _cfg(tmp_path, enabled=True, model=None)
    vlm_setup.ensure_vlm_model(cfg, path, interactive_ok=False)
    assert cfg["vlm"]["enabled"] is False
    assert cfg["vlm"]["model"] is None


def test_save_model_atomic_preserves_other_keys(tmp_path):
    cfg, path = _cfg(tmp_path, enabled=True, model=None)
    vlm_setup.save_model_to_config(path, "openai/gpt-4o")
    with open(path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["vlm"]["model"] == "openai/gpt-4o"
    assert on_disk["vlm"]["enabled"] is True            # preserved
    assert on_disk["vlm"]["prompt"] == "Describe — naïvely 😀"
    assert on_disk["web_ui"]["host"] == "0.0.0.0"       # unrelated section preserved
    # No stray temp file should be left behind.
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []


def test_save_model_creates_vlm_section_if_missing(tmp_path):
    path = tmp_path / "minimal.json"
    path.write_text('{"web_ui": {"host": "127.0.0.1"}}', encoding="utf-8")
    vlm_setup.save_model_to_config(str(path), "some/model")
    with open(path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["vlm"] == {"model": "some/model"}
    assert on_disk["web_ui"]["host"] == "127.0.0.1"


def test_save_model_writes_alternate_slot(tmp_path):
    """The optional ``key=`` parameter persists multipass auxiliary models
    (model_fast, model_actions, model_verify) without clobbering the
    primary ``model`` slot."""
    cfg, path = _cfg(tmp_path, enabled=True, model="qwen2.5vl:7b")
    vlm_setup.save_model_to_config(path, "qwen2.5vl:3b", key="model_fast")
    vlm_setup.save_model_to_config(path, "llama3.2-vision:11b",
                                   key="model_verify")
    with open(path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["vlm"]["model"]        == "qwen2.5vl:7b"
    assert on_disk["vlm"]["model_fast"]   == "qwen2.5vl:3b"
    assert on_disk["vlm"]["model_verify"] == "llama3.2-vision:11b"
    # UTF-8 prompt round-trips alongside the new keys.
    assert on_disk["vlm"]["prompt"] == "Describe — naïvely 😀"

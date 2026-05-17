"""Tests for ollama_setup — runner detection, model inventory, and pull."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from ollama_setup import (
    _collect_model_names,
    _is_ollama_model,
    _list_local_models,
    ensure_models,
    ensure_runner,
)


# ─── _is_ollama_model ────────────────────────────────────────────────────────

def test_is_ollama_model_plain():
    assert _is_ollama_model("qwen2.5vl:7b") is True
    assert _is_ollama_model("llama3.2-vision:11b") is True
    assert _is_ollama_model("minicpm-v:8b") is True
    assert _is_ollama_model("moondream:latest") is True


def test_is_ollama_model_cloud_namespace():
    assert _is_ollama_model("anthropic/claude-3-5-sonnet") is False
    assert _is_ollama_model("openai/gpt-4o") is False
    assert _is_ollama_model("meta/llama3") is False


# ─── _collect_model_names ────────────────────────────────────────────────────

def test_collect_model_names_all_slots():
    vlm = {
        "model":         "qwen2.5vl:7b",
        "model_fast":    "qwen2.5vl:3b",
        "model_actions": "qwen2.5:14b",
        "model_verify":  "llama3.2-vision:11b",
    }
    names = [m for m, _ in _collect_model_names(vlm)]
    assert names == ["qwen2.5vl:7b", "qwen2.5vl:3b", "qwen2.5:14b",
                     "llama3.2-vision:11b"]


def test_collect_model_names_empty_slots_skipped():
    vlm = {"model": "qwen2.5vl:7b", "model_fast": None, "model_verify": ""}
    names = [m for m, _ in _collect_model_names(vlm)]
    assert names == ["qwen2.5vl:7b"]


def test_collect_model_names_no_slots():
    assert _collect_model_names({}) == []


# ─── _list_local_models ──────────────────────────────────────────────────────

_OLLAMA_LIST_OUTPUT = """\
NAME                     ID              SIZE    MODIFIED
qwen2.5vl:7b             abc123          5.0 GB  2 days ago
qwen2.5vl:3b             def456          2.1 GB  2 days ago
llama3.2-vision:11b      ghi789          8.0 GB  1 week ago
"""


def _mk_run(returncode=0, stdout=""):
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    return mock


def test_list_local_models_parses_ollama_output():
    with patch("subprocess.run", return_value=_mk_run(0, _OLLAMA_LIST_OUTPUT)):
        result = _list_local_models(["ollama"])
    assert "qwen2.5vl:7b" in result
    assert "qwen2.5vl:3b" in result
    assert "llama3.2-vision:11b" in result


def test_list_local_models_returns_empty_on_error():
    with patch("subprocess.run", return_value=_mk_run(1, "")):
        result = _list_local_models(["ollama"])
    assert result == set()


def test_list_local_models_passes_correct_command():
    captured = {}
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _mk_run(0, _OLLAMA_LIST_OUTPUT)
    with patch("subprocess.run", side_effect=fake_run):
        _list_local_models(["docker", "exec", "mybox", "ollama"])
    assert captured["cmd"] == ["docker", "exec", "mybox", "ollama", "list"]


# ─── ensure_runner ───────────────────────────────────────────────────────────

def _cfg_with_runner(tmp_path, runner_value):
    cfg = {"vlm": {"enabled": True, "ollama_runner": runner_value}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg, str(p)


def test_ensure_runner_returns_saved_list(tmp_path):
    cfg, path = _cfg_with_runner(tmp_path, ["ollama"])
    result = ensure_runner(cfg, path, interactive_ok=False)
    assert result == ["ollama"]


def test_ensure_runner_returns_empty_list_when_skip_saved(tmp_path):
    cfg, path = _cfg_with_runner(tmp_path, [])
    result = ensure_runner(cfg, path, interactive_ok=False)
    assert result == []


def test_ensure_runner_non_interactive_returns_empty_when_unset(tmp_path):
    cfg = {"vlm": {"enabled": True}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    result = ensure_runner(cfg, str(p), interactive_ok=False)
    assert result == []


def test_ensure_runner_parses_string_runner(tmp_path):
    cfg, path = _cfg_with_runner(tmp_path, "docker exec mybox ollama")
    result = ensure_runner(cfg, path, interactive_ok=False)
    assert result == ["docker", "exec", "mybox", "ollama"]


# ─── ensure_models ───────────────────────────────────────────────────────────

def _cfg_full(tmp_path, **vlm_extra):
    vlm = {
        "enabled": True,
        "model":         "qwen2.5vl:7b",
        "model_fast":    "qwen2.5vl:3b",
        "model_actions": None,
        "model_verify":  None,
        "ollama_runner": ["ollama"],
        **vlm_extra,
    }
    cfg = {"vlm": vlm}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg, str(p)


def test_ensure_models_skips_when_disabled(tmp_path):
    cfg = {"vlm": {"enabled": False}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    # Should not call subprocess at all.
    with patch("subprocess.run") as mock_run:
        ensure_models(cfg, str(p), interactive_ok=False)
    mock_run.assert_not_called()


def test_ensure_models_skips_when_runner_empty(tmp_path):
    cfg, path = _cfg_full(tmp_path, ollama_runner=[])
    with patch("subprocess.run") as mock_run:
        ensure_models(cfg, path, interactive_ok=False)
    mock_run.assert_not_called()


def test_ensure_models_pulls_missing(tmp_path):
    cfg, path = _cfg_full(tmp_path)
    # Local Ollama has qwen2.5vl:7b but not qwen2.5vl:3b.
    local_output = "NAME\nqwen2.5vl:7b   abc  5GB  1d\n"

    pull_calls = []

    def fake_popen(cmd, **kw):
        pull_calls.append(cmd)
        proc = MagicMock()
        proc.stdout = iter(["pulling manifest\n", "success\n"])
        proc.returncode = 0
        proc.wait.return_value = 0
        return proc

    with patch("subprocess.run", return_value=_mk_run(0, local_output)), \
         patch("subprocess.Popen", side_effect=fake_popen):
        ensure_models(cfg, path, interactive_ok=False)

    assert any("qwen2.5vl:3b" in str(c) for c in pull_calls), \
        f"Expected pull for qwen2.5vl:3b; got {pull_calls}"
    # Should NOT pull qwen2.5vl:7b (already present).
    for c in pull_calls:
        assert "qwen2.5vl:7b" not in str(c) or "3b" in str(c), \
            f"Unexpected pull for qwen2.5vl:7b: {c}"


def test_ensure_models_nothing_to_pull(tmp_path):
    cfg, path = _cfg_full(tmp_path)
    local_output = (
        "NAME\n"
        "qwen2.5vl:7b   abc  5GB  1d\n"
        "qwen2.5vl:3b   def  2GB  1d\n"
    )
    with patch("subprocess.run", return_value=_mk_run(0, local_output)), \
         patch("subprocess.Popen") as mock_popen:
        ensure_models(cfg, path, interactive_ok=False)
    mock_popen.assert_not_called()


def test_ensure_models_skips_cloud_models(tmp_path):
    cfg, path = _cfg_full(tmp_path,
                          model="anthropic/claude-3-5-sonnet",
                          model_fast="openai/gpt-4o")
    with patch("subprocess.run", return_value=_mk_run(0, "NAME\n")), \
         patch("subprocess.Popen") as mock_popen:
        ensure_models(cfg, path, interactive_ok=False)
    # Cloud-namespaced models are not Ollama-pullable — no pull attempted.
    mock_popen.assert_not_called()


def test_ensure_models_deduplicates(tmp_path):
    # model and model_fast are the same — pull only once.
    cfg, path = _cfg_full(tmp_path,
                          model="qwen2.5vl:7b",
                          model_fast="qwen2.5vl:7b")
    pull_calls = []

    def fake_popen(cmd, **kw):
        pull_calls.append(cmd)
        proc = MagicMock()
        proc.stdout = iter([])
        proc.returncode = 0
        proc.wait.return_value = 0
        return proc

    with patch("subprocess.run", return_value=_mk_run(0, "NAME\n")), \
         patch("subprocess.Popen", side_effect=fake_popen):
        ensure_models(cfg, path, interactive_ok=False)

    # Only one pull call, not two.
    assert len(pull_calls) == 1

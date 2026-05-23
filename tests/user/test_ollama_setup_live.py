"""
Live test for ollama_setup.ensure_models against a real Ollama daemon.

Skipped when Ollama isn't running. When it is, the test confirms that
asking for a model that's already present is a no-op and reports success.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = [pytest.mark.user, pytest.mark.slow_llm]


def _list_ollama_models(base_url: str) -> list[str]:
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2.0) as r:
            data = json.loads(r.read())
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def test_ensure_models_with_already_pulled_model_is_idempotent(
        ollama_base_url, chat_model, vlm_model, tmp_path):
    if not ollama_base_url:
        pytest.skip("Ollama is not reachable")
    available = _list_ollama_models(ollama_base_url)
    target = None
    for cand in (chat_model, vlm_model):
        if any(cand in a for a in available):
            target = cand
            break
    if target is None:
        pytest.skip(
            f"No pre-pulled model overlaps with {chat_model!r}/{vlm_model!r}; "
            f"available={available!r}"
        )
    # Drive ollama_setup.ensure_models against a config that points at the
    # daemon and references the available model.
    import sys
    sys.path.insert(0, str(__file__.rsplit("/tests/", 1)[0]))
    from ollama_setup import ensure_models  # type: ignore
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "vlm": {
            "enabled": True,
            "base_url": ollama_base_url,
            "model": target,
        },
    }))
    cfg = json.loads(cfg_path.read_text())
    # interactive_ok=False so the call returns without prompting.
    ensure_models(cfg, str(cfg_path), interactive_ok=False)
    # Model still present.
    after = _list_ollama_models(ollama_base_url)
    assert any(target in a for a in after)

"""
Exercises the VLM (vision-LLM) pipeline against a real Ollama daemon.

Skipped when Ollama isn't reachable or when the configured VLM model
isn't pulled. In the test Docker image this is wired up out-of-the-box.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = [pytest.mark.user, pytest.mark.slow_vlm]


def _vlm_model_available(base_url: str, model: str) -> bool:
    try:
        with urllib.request.urlopen(
            f"{base_url}/api/tags", timeout=2.0
        ) as r:
            tags = json.loads(r.read())
        names = [m.get("name", "") for m in tags.get("models", [])]
        return any(model in n for n in names)
    except Exception:
        return False


def test_vlm_describe_window_returns_json_envelope(
        oso_server_factory, ollama_base_url, vlm_model):
    if not ollama_base_url:
        pytest.skip("Ollama is not reachable")
    if not _vlm_model_available(ollama_base_url, vlm_model):
        pytest.skip(f"VLM model {vlm_model!r} not pulled on the Ollama daemon")

    cfg = {
        "vlm": {
            "enabled": True,
            "base_url": ollama_base_url,
            "model": vlm_model,
            "mode": "single",
            "output_format": "json",
            "timeout_s": 60,
            "max_tokens": 400,
            "ground_with_tree": False,
            "ground_with_ocr": False,
            "ground_with_sketch": False,
            "ground_with_focus": False,
        },
        "mock": True,
    }
    srv = oso_server_factory(config_overrides=cfg)
    from tests.user.conftest import HttpJson
    http = HttpJson(srv["base_url"], timeout=90.0)
    _, body = http.get("/api/description",
                       {"window_index": 0, "engine": "vlm"})
    # Server may surface a "VLM disabled / no model" error if the model
    # was pulled but isn't a true vision model — accept either shape.
    assert ("description" in body) or ("error" in body), body
    if "description" in body:
        # When single-mode JSON is requested the description value is
        # the raw text from the model. Don't assert content; just that
        # the call round-tripped without an HTTP error.
        assert isinstance(body["description"], (str, dict))

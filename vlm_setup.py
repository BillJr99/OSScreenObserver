"""
vlm_setup.py — Interactive VLM model picker and OpenWebUI/Ollama client helpers.

The VLM modality (Claude Vision, GPT-4V, llava, etc.) is reached through an
OpenAI-compatible chat-completions endpoint. Two path prefixes are tried
automatically in order:

  1. ``/api/v1`` — OpenWebUI's path convention (default).
  2. ``/v1``     — Ollama's native OpenAI-compat path and the standard
                   OpenAI / LiteLLM convention.

This module:

  * Fetches the list of models from ``{base_url}/<prefix>/models``.
  * Presents a paginated numbered picker on the controlling terminal.
  * Persists the chosen model back to ``config.json`` so subsequent
    launches start non-interactively.

The picker is only invoked when:
  * ``vlm.enabled`` is true in config, AND
  * ``vlm.model`` is unset/empty, AND
  * the run mode does not own stdin (i.e. not ``mcp`` / ``both``), AND
  * stdin is a TTY.

Otherwise VLM is automatically disabled for the run with a clear log
message; the operator can either edit config.json directly or launch
once with ``--mode inspect`` to walk through setup.
"""

import json
import os
import sys
import tempfile
import urllib.request
from typing import List, Optional, Tuple

# Tried in order; first one that returns a valid model list wins.
_API_PREFIXES = ["/api/v1", "/v1"]
_PAGE_SIZE = 20


def _resolve_api_key(cfg_key: Optional[str]) -> str:
    if cfg_key:
        return cfg_key
    return os.environ.get("OWUI_API_KEY", "")


def _try_fetch_models(base_url: str, prefix: str, headers: dict,
                      timeout: float) -> Tuple[List[str], Optional[str]]:
    """Single attempt against one prefix. Returns (model_ids, error)."""
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + prefix + "/models",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return [m["id"] for m in data.get("data", []) if "id" in m], None
    except Exception as e:
        return [], str(e)


def fetch_models(base_url: str, api_key: str,
                 timeout: float = 10.0) -> Tuple[List[str], Optional[str]]:
    """Return (model_ids, error_message). On success error_message is None.

    Tries ``/api/v1/models`` first (OpenWebUI convention), then falls back
    to ``/v1/models`` (Ollama / OpenAI / LiteLLM convention).
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last_err: Optional[str] = None
    for prefix in _API_PREFIXES:
        models, err = _try_fetch_models(base_url, prefix, headers, timeout)
        if models:
            return models, None
        last_err = err
    return [], last_err


def pick_model_paginated(models: List[str]) -> Optional[str]:
    """Paginated numbered picker (20 per page). Returns chosen id or None."""
    if not models:
        return None
    page = 0
    n_pages = (len(models) + _PAGE_SIZE - 1) // _PAGE_SIZE
    while True:
        start = page * _PAGE_SIZE
        end   = min(start + _PAGE_SIZE, len(models))
        print(f"\n  Available models (page {page + 1}/{n_pages}, "
              f"{len(models)} total):", file=sys.stderr)
        for i in range(start, end):
            print(f"    {i + 1:>3}. {models[i]}", file=sys.stderr)
        nav = []
        if page > 0:
            nav.append("p=prev")
        if page < n_pages - 1:
            nav.append("n=next")
        nav.append("<number>|<name> to pick")
        nav.append("q=skip")
        print("  [" + ", ".join(nav) + "]", file=sys.stderr)
        try:
            raw = input("  Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            continue
        if raw == "q":
            return None
        if raw == "n" and page < n_pages - 1:
            page += 1
            continue
        if raw == "p" and page > 0:
            page -= 1
            continue
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                return models[idx]
            print(f"  Number out of range (1–{len(models)})", file=sys.stderr)
            continue
        if raw in models:
            return raw
        try:
            confirm = input(f"  '{raw}' not in list — use anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if confirm == "y":
            return raw


def save_model_to_config(config_path: str, model: str,
                         *, key: str = "model") -> None:
    """Persist vlm.<key> back to *config_path*, preserving all other keys.

    Writes via a sibling temp file + atomic rename so an interrupted run
    (Ctrl-C, OOM, full disk on the final flush) cannot leave the user
    with a truncated config.json. Encoding is pinned to UTF-8 so the
    custom `vlm.prompt` survives a round-trip on platforms with a
    non-UTF-8 default locale (Windows in particular).

    *key* selects which slot to write — "model" (default, primary),
    "model_fast", "model_actions", or "model_verify" for the multipass
    pipeline auxiliary models.
    """
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("vlm", {})[key] = model
    dir_name = os.path.dirname(os.path.abspath(config_path)) or "."
    fd, tmp = tempfile.mkstemp(
        prefix=".config.", suffix=".json.tmp", dir=dir_name,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, config_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_vlm_model(config: dict, config_path: str, *,
                     interactive_ok: bool) -> None:
    """If vlm.enabled is true but vlm.model is unset, run interactive setup
    and save the chosen model. Edits *config* in place.

    *interactive_ok* must be False whenever stdin is owned by the MCP
    framing channel (modes `mcp`/`both`) — in that case VLM is disabled
    for this run if the model isn't already configured.
    """
    vlm = config.get("vlm")
    if not isinstance(vlm, dict) or not vlm.get("enabled"):
        return
    if vlm.get("model"):
        return

    if not interactive_ok or not sys.stdin.isatty():
        print("[vlm_setup] vlm.enabled=true but vlm.model is not configured "
              "and the current mode cannot prompt interactively. VLM is "
              "disabled for this run. Run `python main.py --mode inspect` "
              "once to pick a model, or set vlm.model in config.json.",
              file=sys.stderr)
        vlm["enabled"] = False
        return

    base_url = vlm.get("base_url") or "http://localhost:3000"
    api_key  = _resolve_api_key(vlm.get("api_key"))
    print(f"\n[vlm] No vlm.model configured. Fetching models from "
          f"{base_url} …", file=sys.stderr)
    models, err = fetch_models(base_url, api_key)
    if not models:
        print(f"[vlm] Could not list models ({err or 'empty response'}). "
              f"VLM disabled for this run. Check vlm.base_url and "
              f"vlm.api_key (or OWUI_API_KEY env var) in config.json.",
              file=sys.stderr)
        vlm["enabled"] = False
        return
    print("\n[vlm] Pick the PRIMARY model (used for Pass 2 / single-shot).",
          file=sys.stderr)
    chosen = pick_model_paginated(models)
    if not chosen:
        print("[vlm] No model chosen — VLM disabled for this run.",
              file=sys.stderr)
        vlm["enabled"] = False
        return
    vlm["model"] = chosen
    try:
        save_model_to_config(config_path, chosen, key="model")
        print(f"[vlm] Saved vlm.model = {chosen!r} to {config_path}",
              file=sys.stderr)
    except Exception as e:
        print(f"[vlm] (Could not write {config_path}: {e}; using for this "
              f"run only.)", file=sys.stderr)

    # Multipass auxiliaries are optional. Only prompt when the run is
    # actually configured for multipass and the slot is still empty — a
    # pre-set value from config.json is honoured without re-asking.
    if (vlm.get("mode") or "single").lower() != "multipass":
        return

    for slot, label, help_text in (
        ("model_fast",    "FAST",
         "Used for Pass 1 (scene) and per-widget crop labelling. A small "
         "model (e.g. qwen2.5vl:3b or moondream) is plenty. Skip to reuse "
         "the primary model."),
        ("model_actions", "ACTIONS",
         "Used for Pass 3 (next-action candidates). No image is sent on "
         "this pass, so a strong text-only LLM (e.g. qwen2.5:14b) is "
         "cheaper than a VLM. Skip to reuse the primary model."),
        ("model_verify",  "VERIFY",
         "OPTIONAL. Used for the verify pass that cross-checks pass-2 "
         "controls against the accessibility tree. Pick a different model "
         "family from the primary for a genuine second opinion. Skip to "
         "leave the verify pass disabled."),
    ):
        if vlm.get(slot):
            continue
        print(f"\n[vlm] Pick the {label} model (optional). {help_text}",
              file=sys.stderr)
        picked = pick_model_paginated(models)
        if not picked:
            continue
        vlm[slot] = picked
        try:
            save_model_to_config(config_path, picked, key=slot)
            print(f"[vlm] Saved vlm.{slot} = {picked!r} to {config_path}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[vlm] (Could not write {config_path}: {e}; using for "
                  f"this run only.)", file=sys.stderr)

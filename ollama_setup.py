"""
ollama_setup.py — Ollama runner detection, model inventory, and auto-pull.

Called from main.py in --mode inspect (interactive) when vlm.enabled=true
and the configured base_url points to a local Ollama instance.

Responsibilities
────────────────
1. Ask the user (once) how to invoke the ollama CLI:
     a) directly — ``ollama``
     b) inside a running Docker container —
        ``docker exec <container_name> ollama``
     c) any custom prefix the user types

   The chosen prefix is saved as ``vlm.ollama_runner`` in config.json so
   subsequent launches skip the question.

2. Discover which model slots are configured in the vlm section:
     vlm.model          — primary (Pass 2 / single-shot)
     vlm.model_fast     — Pass 1 + per-widget crop labels
     vlm.model_actions  — Pass 3 (text-only; can be a non-vision LLM)
     vlm.model_verify   — optional verify pass (different family recommended)

3. Run ``<runner> list`` to get locally-available models.

4. Pull any configured model that is not already present, printing a one-line
   progress indicator per model.  Non-Ollama model identifiers (those
   containing a ``/`` namespace prefix such as ``anthropic/claude-3-5-sonnet``
   or ``openai/gpt-4o``) are skipped — they are not pullable via the Ollama
   CLI and are assumed to be available through the configured base_url API.

5. Return success/failure quietly — a pull failure prints a warning but does
   not abort startup; the model will fail at inference time with a clear error.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tempfile
import json
import os
from typing import List, Optional, Set, Tuple

_DOCKER_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


# ─────────────────────────────────────────────────────────────────────────────
# Runner detection
# ─────────────────────────────────────────────────────────────────────────────

def _test_runner(runner_prefix: List[str]) -> bool:
    """Return True if ``<runner_prefix> list`` exits with code 0."""
    cmd = runner_prefix + ["list"]
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _detect_docker_containers() -> List[str]:
    """Return names of running Docker containers that have 'ollama' in their
    name, so we can offer them as quick-pick options."""
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0:
            return []
        return [n.strip() for n in r.stdout.splitlines()
                if n.strip() and "ollama" in n.lower()]
    except Exception:
        return []


def _ask_runner() -> Optional[List[str]]:
    """Interactively ask the user how to run the Ollama CLI.

    Returns a list of tokens (the runner prefix) or None if the user skips.
    """
    print(
        "\n[ollama_setup] How should OSScreenObserver invoke the Ollama CLI?",
        file=sys.stderr,
    )

    options: List[Tuple[str, List[str]]] = []

    # Option 1: native ollama on PATH
    if _test_runner(["ollama"]):
        options.append(("ollama  (detected on PATH)", ["ollama"]))

    # Option 2: Docker containers named with 'ollama'
    containers = _detect_docker_containers()
    for c in containers:
        prefix = ["docker", "exec", c, "ollama"]
        label  = f"docker exec {c} ollama  (container running)"
        options.append((label, prefix))

    options.append(("Custom — type your own prefix", None))
    options.append(("Skip — do not pull models automatically", []))

    for i, (label, _) in enumerate(options):
        print(f"  {i + 1}. {label}", file=sys.stderr)

    while True:
        try:
            raw = input("  Select [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw == "":
            raw = "1"
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                _, prefix = options[idx]
                if prefix is None:
                    # Custom entry
                    try:
                        custom = input(
                            "  Enter the full prefix (e.g. "
                            "'docker exec my_ollama ollama'): "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        return None
                    if not custom:
                        print("  (empty — skipping)", file=sys.stderr)
                        return []
                    tokens = shlex.split(custom)
                    if not _test_runner(tokens):
                        print(
                            f"  WARNING: '{' '.join(tokens)} list' did not "
                            f"succeed — check the prefix and try again.",
                            file=sys.stderr,
                        )
                        try:
                            ok = input("  Use it anyway? [y/N] ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            return None
                        if ok != "y":
                            continue
                    return tokens
                if prefix == []:           # skip
                    return []
                return prefix
        print(f"  Please enter a number 1–{len(options)}.", file=sys.stderr)


def ensure_runner(config: dict, config_path: str, *,
                  interactive_ok: bool) -> List[str]:
    """Return the Ollama runner prefix (list of tokens).

    If ``vlm.ollama_runner`` is already in config, return that.
    Otherwise, when interactive_ok, ask and save. Otherwise return [].
    """
    vlm = config.get("vlm") or {}
    saved = vlm.get("ollama_runner")
    if saved is not None:               # could be [] (skip) or a non-empty list
        if isinstance(saved, list):
            return saved
        if isinstance(saved, str) and saved:
            return shlex.split(saved)
        return []

    if not interactive_ok or not sys.stdin.isatty():
        return []

    runner = _ask_runner()
    if runner is None:
        return []

    # Persist the choice.
    vlm["ollama_runner"] = runner
    _atomic_save(config_path, lambda cfg: cfg.setdefault("vlm", {}).update(
        {"ollama_runner": runner}
    ))
    if runner:
        print(
            f"[ollama_setup] Runner saved: {' '.join(runner)!r}",
            file=sys.stderr,
        )
    else:
        print("[ollama_setup] Auto-pull skipped.", file=sys.stderr)
    return runner


# ─────────────────────────────────────────────────────────────────────────────
# Model inventory and pull
# ─────────────────────────────────────────────────────────────────────────────

def _collect_model_names(vlm_cfg: dict) -> List[Tuple[str, str]]:
    """Return [(slot_name, model_id), ...] for all non-empty model slots."""
    slots = [
        ("model",         "primary (Pass 2 / single-shot)"),
        ("model_fast",    "fast (Pass 1 + crop labels)"),
        ("model_actions", "actions (Pass 3, text-only OK)"),
        ("model_verify",  "verify (optional, second family)"),
    ]
    result = []
    for key, description in slots:
        val = vlm_cfg.get(key)
        if val and isinstance(val, str):
            result.append((val, description))
    return result


def _is_ollama_model(model_id: str) -> bool:
    """Return False for namespaced cloud-model IDs (anthropic/, openai/, etc.)
    that are not pullable via the Ollama CLI."""
    # Ollama models look like "qwen2.5vl:7b", "llama3.2-vision:11b", etc.
    # Cloud models look like "anthropic/claude-3-5-sonnet", "openai/gpt-4o".
    return "/" not in model_id


def _list_local_models(runner: List[str]) -> Set[str]:
    """Return the set of model IDs currently in the local Ollama library."""
    try:
        r = subprocess.run(
            runner + ["list"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return set()
        lines = r.stdout.strip().splitlines()
        names: Set[str] = set()
        for line in lines[1:]:          # skip header row
            parts = line.split()
            if parts:
                names.add(parts[0])     # first column is NAME (with tag)
        return names
    except Exception:
        return set()


def _pull_model(runner: List[str], model_id: str) -> bool:
    """Pull *model_id* using *runner*, streaming output to stderr.

    Returns True on success, False on error.
    """
    cmd = runner + ["pull", model_id]
    print(
        f"[ollama_setup] Pulling {model_id!r}  ({' '.join(cmd)})",
        file=sys.stderr,
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"  {line}", file=sys.stderr)
        proc.wait()
        if proc.returncode == 0:
            print(f"[ollama_setup] ✓ {model_id!r} ready.", file=sys.stderr)
            return True
        print(
            f"[ollama_setup] ✗ Pull failed for {model_id!r} "
            f"(exit {proc.returncode}) — inference will fail at runtime.",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(
            f"[ollama_setup] ✗ Could not run pull for {model_id!r}: {exc}",
            file=sys.stderr,
        )
        return False


def ensure_models(config: dict, config_path: str, *,
                  interactive_ok: bool) -> None:
    """Check all configured VLM model slots and pull any that are missing.

    Flow:
      1. Determine the runner prefix (ask if needed).
      2. Collect unique model IDs across all four slots.
      3. Skip cloud-namespaced IDs.
      4. Compare against locally-installed models.
      5. Pull each missing model, printing progress.
    """
    vlm = config.get("vlm") or {}
    if not vlm.get("enabled"):
        return

    runner = ensure_runner(config, config_path, interactive_ok=interactive_ok)
    if not runner:
        return  # user opted out or non-interactive

    pairs = _collect_model_names(vlm)
    if not pairs:
        return

    # Report the full model-role mapping.
    print("\n[ollama_setup] Configured VLM model slots:", file=sys.stderr)
    seen_ids: dict[str, str] = {}  # model_id → first description seen
    for model_id, description in pairs:
        role = f"  {description}"
        if model_id in seen_ids:
            role += f"  (same as {seen_ids[model_id]})"
        else:
            seen_ids[model_id] = description
        print(f"    {role}: {model_id}", file=sys.stderr)

    # Filter to Ollama-pullable models only; de-duplicate.
    to_check: List[str] = []
    skipped: List[str] = []
    seen: Set[str] = set()
    for model_id, _ in pairs:
        if model_id in seen:
            continue
        seen.add(model_id)
        if _is_ollama_model(model_id):
            to_check.append(model_id)
        else:
            skipped.append(model_id)

    if skipped:
        print(
            "[ollama_setup] Skipping cloud-model IDs (not Ollama-pullable): "
            + ", ".join(repr(m) for m in skipped),
            file=sys.stderr,
        )

    if not to_check:
        return

    print(
        f"\n[ollama_setup] Checking {len(to_check)} Ollama model(s)…",
        file=sys.stderr,
    )
    local = _list_local_models(runner)

    already_ok: List[str] = []
    to_pull:    List[str] = []
    for model_id in to_check:
        # Ollama list output uses full tag (e.g. "qwen2.5vl:7b"); also
        # handle the implicit :latest suffix.
        tag = model_id if ":" in model_id else model_id + ":latest"
        if tag in local or model_id in local:
            already_ok.append(model_id)
        else:
            to_pull.append(model_id)

    if already_ok:
        print(
            "[ollama_setup] Already available: "
            + ", ".join(repr(m) for m in already_ok),
            file=sys.stderr,
        )

    if not to_pull:
        print("[ollama_setup] All models present — nothing to pull.",
              file=sys.stderr)
        return

    print(
        f"[ollama_setup] Need to pull {len(to_pull)} model(s): "
        + ", ".join(repr(m) for m in to_pull),
        file=sys.stderr,
    )
    for model_id in to_pull:
        _pull_model(runner, model_id)


# ─────────────────────────────────────────────────────────────────────────────
# Config persistence helper
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_save(config_path: str, mutate) -> None:
    """Load config_path, call mutate(cfg), write atomically via rename."""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        mutate(cfg)
        dir_name = os.path.dirname(os.path.abspath(config_path)) or "."
        fd, tmp = tempfile.mkstemp(
            prefix=".config.", suffix=".json.tmp", dir=dir_name,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, config_path)
    except Exception as exc:
        print(f"[ollama_setup] Could not save config: {exc}", file=sys.stderr)

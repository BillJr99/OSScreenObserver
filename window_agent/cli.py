"""
Interactive CLI: banner, window picker, prompts, main().

Split out of window_agent.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from window_agent.client import (
    _OWU_PREFIX, LLMClient, _c, api_list_windows, api_observe,
    wait_for_server,
)
from window_agent.loop import run_agent
from window_agent.prompts import SYSTEM_PROMPT

# ─── Display helpers ──────────────────────────────────────────────────────────

_BANNER = r"""
  ___  ____ ____                            ___  _
 / _ \/ ___/ ___|  ___ _ __ ___  ___ _ __  / _ \| |__  ___  ___ _ ____   _____ _ __
| | | \___ \___ \ / __| '__/ _ \/ _ \ '_ \| | | | '_ \/ __|/ _ \ '__\ \ / / _ \ '__|
| |_| |___) |__) | (__| | |  __/  __/ | | | |_| | |_) \__ \  __/ |   \ V /  __/ |
 \___/|____/____/ \___|_|  \___|\___|_| |_|\___/|_.__/|___/\___|_|    \_/ \___|_|
"""

def print_banner():
    print(_c(_BANNER.strip("\n"), "cyan", "bold"))
    print(_c("  Window Inspection + LLM Agent  •  OSScreenObserver REST API\n", "dim"))


def print_window_list(windows: List[Dict]) -> None:
    print(_c(f"\n  {'#':>3}  {'Title':<50}  {'Process':<20}  {'Size'}", "bold"))
    print("  " + "─" * 90)
    for w in windows:
        b = w.get("bounds", {})
        size = f"{b.get('width', 0)}×{b.get('height', 0)}"
        focused = _c(" ◀", "green") if w.get("focused") else ""
        idx_s  = _c(f"{w['index']:>3}", "yellow")
        title  = w.get("title",   "")[:50]
        proc   = w.get("process", "")[:20]
        print(f"  {idx_s}  {title:<50}  {proc:<20}  {size}{focused}")
    print()


def print_window_view(data: Dict) -> None:
    """Print the sketch and description for a selected window."""
    window = data.get("window", "?")
    sketch = data.get("sketch", "")
    desc   = data.get("description", "")

    width = max((len(ln) for ln in sketch.splitlines()), default=0) + 4
    width = max(width, 60)
    header = f" SKETCH — {window} "
    pad = max(0, width - len(header) - 2)
    print()
    print(_c("┌" + header + "─" * pad + "┐", "blue"))
    for line in sketch.splitlines():
        print(_c("│ ", "blue") + line)
    print(_c("└" + "─" * (width - 2) + "┘", "blue"))

    if desc:
        print()
        print(_c("  ACCESSIBILITY DESCRIPTION", "bold"))
        print("  " + "─" * 50)
        for line in desc.splitlines():
            print("  " + line)
    print()

# ─── Interactive prompts ──────────────────────────────────────────────────────

def prompt(msg: str, default: str = "", secret: bool = False) -> str:
    if default:
        display = f"{msg} [{default}]: "
    else:
        display = f"{msg}: "
    if secret:
        import getpass
        val = getpass.getpass(display)
    else:
        val = input(display).strip()
    return val if val else default


def ask_connection() -> Tuple[str, str]:
    """Interactively collect OpenWebUI connection parameters (model chosen after fetch)."""
    print(_c("\n  ── OpenWebUI / LLM Connection ──────────────────────────────\n", "bold"))
    base_url = prompt("  OpenWebUI base URL", "http://localhost:3000")
    api_key  = prompt("  API key (leave blank if none)", secret=True)
    return base_url, api_key


def pick_model(models: List[str]) -> str:
    """Display a numbered menu of *models* and return the chosen model id."""
    default = models[0]
    print(_c("\n  Available models:\n", "bold"))
    for i, m in enumerate(models):
        print(_c(f"    {i + 1:>3}. ", "yellow") + m)
    print()
    while True:
        raw = input(_c(f"  Select model (number or name) [{default}]: ", "bold", "cyan")).strip()
        if not raw:
            return default
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                return models[idx]
            print(_c(f"  Please enter a number between 1 and {len(models)}.", "red"))
        elif raw in models:
            return raw
        else:
            confirm = input(_c(f"  '{raw}' not in list — use it anyway? [y/N] ", "yellow")).strip().lower()
            if confirm == "y":
                return raw


def list_models(llm_base: str, api_key: str) -> Tuple[List[str], Optional[str]]:
    """Fetch model list from /api/v1/models. Returns (models, error_message)."""
    try:
        headers: Dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            llm_base.rstrip("/") + f"{_OWU_PREFIX}/models",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return [m["id"] for m in data.get("data", [])], None
    except Exception as e:
        return [], str(e)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="OSScreenObserver interactive agent")
    parser.add_argument("--rest", default="http://127.0.0.1:5001",
                        help="Base URL of the OSScreenObserver REST server")
    args = parser.parse_args()
    rest = args.rest

    print_banner()

    # ── 1. REST server ────────────────────────────────────────────────────────
    print(_c(f"  REST server: {rest}\n", "dim"))
    if not wait_for_server(rest):
        print(_c(
            "\n  Could not reach the REST server. "
            "Start it with:  python main.py --mode inspect\n", "red"
        ))
        sys.exit(1)

    # ── 2. LLM connection ─────────────────────────────────────────────────────
    llm_base, api_key = ask_connection()

    print(_c(f"\n  Checking connection to {llm_base} …", "dim"), end="", flush=True)
    models, err = list_models(llm_base, api_key)
    if models:
        print(_c(f" OK  ({len(models)} model(s) available)", "green"))
        model = pick_model(models)
    elif err:
        print(_c(f" failed — {err}", "yellow"))
        model = prompt("  Model name", "llama3.2:3b")
    else:
        print(_c(" connected, but no models found", "yellow"))
        model = prompt("  Model name", "llama3.2:3b")

    llm = LLMClient(llm_base, api_key, model)

    # ── 3. Main window-selection loop ─────────────────────────────────────────
    conversation: List[Dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    selected_window: Optional[int] = None
    selected_uid: Optional[str] = None

    print()
    print(_c("  Commands:  <number> select window   r refresh   q quit", "dim"))

    while True:
        # Fetch and display window list
        try:
            wdata = api_list_windows(rest)
        except Exception as e:
            print(_c(f"\n  [Failed to list windows: {e}]", "red"))
            input("  Press Enter to retry… ")
            continue

        windows = wdata.get("windows", [])
        if not windows:
            print(_c("\n  No windows found. Press Enter to refresh.", "yellow"))
            input()
            continue

        print()
        print(_c("  ── Open Windows ─────────────────────────────────────────────", "bold"))
        print_window_list(windows)

        raw = input(_c("  Select window [number / r / q]: ", "bold")).strip().lower()

        if raw in ("q", "quit", "exit"):
            print(_c("\n  Goodbye.\n", "dim"))
            break

        if raw in ("r", "refresh", ""):
            continue

        try:
            chosen = int(raw)
        except ValueError:
            print(_c("  Invalid input.", "red"))
            continue

        valid_indices = [w["index"] for w in windows]
        if chosen not in valid_indices:
            print(_c(f"  Index {chosen} not in list.", "red"))
            continue

        selected_window = chosen
        chosen_win = next((w for w in windows if w["index"] == chosen), None)
        win_title   = chosen_win["title"] if chosen_win else str(chosen)
        selected_uid = chosen_win.get("window_uid") if chosen_win else None

        # ── 4. Window inspection sub-loop ────────────────────────────────────
        print(_c(f"\n  Loading window [{chosen}] {win_title} …", "dim"))
        try:
            view = api_observe(rest, selected_uid, selected_window)
        except Exception as e:
            print(_c(f"  [Failed to observe window: {e}]", "red"))
            continue

        print_window_view(view)

        print(_c("  Commands:  <task> send to LLM   v view window   r refresh   b back   q quit",
                 "dim"))
        print()

        while True:
            raw2 = input(_c("  Task / command: ", "bold", "cyan")).strip()

            if raw2.lower() in ("q", "quit", "exit"):
                print(_c("\n  Goodbye.\n", "dim"))
                sys.exit(0)

            if raw2.lower() in ("b", "back", ""):
                break

            if raw2.lower() in ("v", "view"):
                try:
                    view = api_observe(rest, selected_uid, selected_window)
                except Exception as e:
                    print(_c(f"  [Failed: {e}]", "red"))
                    continue
                print_window_view(view)
                continue

            if raw2.lower() in ("r", "refresh"):
                try:
                    view = api_observe(rest, selected_uid, selected_window)
                except Exception as e:
                    print(_c(f"  [Failed: {e}]", "red"))
                    continue
                print_window_view(view)
                continue

            # Treat anything else as a task for the LLM agent
            print()
            win_label = selected_uid or str(selected_window)
            print(_c(f"  ── Running agent for task on window [{win_label}] ──────────",
                     "magenta", "bold"))
            conversation = run_agent(llm, rest, raw2, selected_uid, conversation,
                                     default_index=selected_window)
            print()
            print(_c("  ── Agent finished ───────────────────────────────────────────",
                     "magenta"))
            print(_c("  Type 'v' to view the window's current state, or enter another task.",
                     "dim"))
            print()

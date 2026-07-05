"""
Agentic tool-calling loop and tool-result printing.

Split out of window_agent.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import json
import time
import traceback
import urllib.error
from typing import Any, Dict, List, Optional

from window_agent.client import LLMClient, _c
from window_agent.dispatch import dispatch_tool
from window_agent.tool_schemas import (
    SCREEN_TOOLS, _TOOL_BY_NAME, _TOOL_TIER, _initial_active_tools,
    _tool_defs_for,
)

# ─── Agentic loop ─────────────────────────────────────────────────────────────

MAX_TURNS = 30
_LLM_MAX_RETRIES = 3        # retries for transient network errors per turn
_LLM_RETRY_DELAY = 5.0      # seconds before first retry (doubles each attempt)

def run_agent(
    llm:            LLMClient,
    rest:           str,
    user_task:      str,
    default_uid:    Optional[str],
    history:        List[Dict],
    default_index:  Optional[int] = None,
) -> List[Dict]:
    """
    Run the agentic tool-calling loop.

    Appends messages to *history* in place and returns the updated history.
    Prints progress to stdout using ANSI colours.
    """
    history.append({"role": "user", "content": user_task})
    print()
    print(_c(f"  User: {user_task}", "cyan"))

    active_tools: set = _initial_active_tools(user_task)
    n_keyword = len(active_tools) - sum(
        1 for t, tier in _TOOL_TIER.items() if tier in ("always", "usually")
    )
    print(_c(
        f"  [Tools: {len(active_tools)} active"
        + (f" (+{n_keyword} from keywords)" if n_keyword > 0 else "")
        + f" / {len(SCREEN_TOOLS)} total]",
        "dim",
    ))

    for turn in range(MAX_TURNS):
        tool_defs = _tool_defs_for(active_tools)
        resp = None
        for attempt in range(_LLM_MAX_RETRIES + 1):
            try:
                resp = llm.chat(history, tools=tool_defs)
                break
            except (TimeoutError, ConnectionError, OSError) as e:
                if attempt < _LLM_MAX_RETRIES:
                    delay = _LLM_RETRY_DELAY * (2 ** attempt)
                    print(_c(f"\n  [LLM timeout/connection error — retrying in {delay:.0f}s: {e}]", "yellow"))
                    time.sleep(delay)
                else:
                    print(_c(f"\n  [LLM request failed after {_LLM_MAX_RETRIES + 1} attempts: {e}]", "red"))
                    return history
            except urllib.error.URLError as e:
                # URLError wraps OSError/TimeoutError — retry those too.
                cause = e.reason if hasattr(e, "reason") else e
                if isinstance(cause, (TimeoutError, ConnectionError, OSError)) and attempt < _LLM_MAX_RETRIES:
                    delay = _LLM_RETRY_DELAY * (2 ** attempt)
                    print(_c(f"\n  [LLM network error — retrying in {delay:.0f}s: {e}]", "yellow"))
                    time.sleep(delay)
                else:
                    print(_c(f"\n  [LLM request failed: {e}]", "red"))
                    return history
            except Exception as e:
                print(_c(f"\n  [LLM error: {e}]", "red"))
                traceback.print_exc()
                return history
        if resp is None:
            return history

        choices = resp.get("choices", [])
        if not choices:
            print(_c("  [Empty response from LLM]", "red"))
            break

        choice  = choices[0]
        message = choice.get("message", {})
        reason  = choice.get("finish_reason", "stop")

        # Accumulate assistant message into history
        history.append({"role": "assistant", **{k: v for k, v in message.items()
                                                 if k not in ("role",)}})

        # Print any text content the LLM produced
        content = message.get("content") or ""
        if content and content.strip():
            print()
            print(_c("  Assistant:", "green", "bold"))
            for line in content.strip().splitlines():
                print(_c(f"    {line}", "white"))

        if reason != "tool_calls":
            # LLM is done
            break

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            print(_c("  [finish_reason=tool_calls but no tool_calls in message]", "yellow"))
            break

        tool_results: List[Dict] = []
        for tc in tool_calls:
            fn_name = tc.get("function", {}).get("name", "unknown")
            raw_args = tc.get("function", {}).get("arguments", "{}")
            try:
                fn_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                fn_args = {}

            # Pretty-print the call
            arg_str = ", ".join(f"{k}={v!r}" for k, v in fn_args.items()) if fn_args else ""
            print()
            print(_c(f"  → {fn_name}({arg_str})", "yellow", "bold"))

            # Handle meta-tools locally; dispatch everything else to REST.
            if fn_name == "list_available_tools":
                inactive = [
                    {"name": n, "description": _TOOL_BY_NAME[n]["function"]["description"]}
                    for n in _TOOL_BY_NAME
                    if n not in active_tools
                ]
                result = {"available": inactive, "count": len(inactive)}
            elif fn_name == "request_tools":
                requested = fn_args.get("names", [])
                added, unknown = [], []
                for name in requested:
                    if name in _TOOL_BY_NAME and name not in active_tools:
                        active_tools.add(name)
                        added.append(name)
                    elif name not in _TOOL_BY_NAME:
                        unknown.append(name)
                result = {
                    "ok": True,
                    "added": added,
                    "unknown": unknown,
                    "note": (
                        f"Activated {len(added)} tool(s). "
                        "They are available in your tool list from the next turn."
                        + (f" Unknown: {unknown}" if unknown else "")
                    ),
                }
                if added:
                    print(_c(f"    ↳ activated: {', '.join(added)}", "dim"))
            else:
                try:
                    result = dispatch_tool(fn_name, fn_args, rest, default_uid, default_index)
                except Exception as e:
                    result = {"error": str(e)}

            # Print a short summary of the result
            _print_tool_result(fn_name, result)

            tool_results.append({
                "role":         "tool",
                "tool_call_id": tc.get("id", ""),
                "content":      json.dumps(result),
            })

        # Add all tool results as a batch before the next LLM call
        history.extend(tool_results)

    else:
        print(_c(f"\n  [Reached maximum {MAX_TURNS} turns]", "yellow"))

    return history


def _print_tool_result(tool_name: str, result: Any) -> None:
    """Print a concise, human-readable summary of a tool result."""
    if isinstance(result, dict):
        if result.get("error"):
            print(_c(f"    ✗ error: {result['error']}", "red"))
            return

        if tool_name == "list_windows":
            windows = result.get("windows", [])
            print(_c(f"    ← {len(windows)} window(s):", "dim"))
            for w in windows[:8]:
                flag = " [FOCUSED]" if w.get("focused") else ""
                uid  = f"  uid={w['window_uid']}" if w.get("window_uid") else ""
                print(_c(f"      [{w['index']}] {w['title']}{flag}{uid}", "dim"))
            if len(windows) > 8:
                print(_c(f"      … and {len(windows) - 8} more", "dim"))

        elif tool_name in ("observe_window",):
            sketch = result.get("sketch", "")
            if sketch:
                first_lines = sketch.splitlines()[:5]
                for ln in first_lines:
                    print(_c(f"    │ {ln}", "dim"))
                extra = len(sketch.splitlines()) - 5
                if extra > 0:
                    print(_c(f"    │ … ({extra} more lines)", "dim"))

        elif tool_name == "get_screen_sketch":
            sketch = result.get("sketch", "")
            lines  = sketch.splitlines()
            for ln in lines[:6]:
                print(_c(f"    │ {ln}", "dim"))
            if len(lines) > 6:
                print(_c(f"    │ … ({len(lines) - 6} more lines)", "dim"))

        elif tool_name in ("click_at", "type_text", "press_key", "scroll"):
            ok = result.get("success", False)
            sym = "✓" if ok else "✗"
            color = "green" if ok else "red"
            note = f" ({result['note']})" if "note" in result else ""
            err  = f" — {result['error']}" if "error" in result else ""
            print(_c(f"    ← {sym} {tool_name}{note}{err}", color))

        elif tool_name == "get_element_tree":
            count = result.get("element_count", "?")
            window = result.get("window", "")
            print(_c(f"    ← {count} elements in '{window}'", "dim"))

        elif tool_name == "get_screen_description":
            desc = result.get("description", "")
            preview = (desc[:120] + "…") if len(desc) > 120 else desc
            print(_c(f"    ← {preview}", "dim"))

        else:
            # Generic: print first 200 chars of JSON
            raw = json.dumps(result)
            print(_c(f"    ← {raw[:200]}{'…' if len(raw) > 200 else ''}", "dim"))
    else:
        raw = str(result)
        print(_c(f"    ← {raw[:200]}{'…' if len(raw) > 200 else ''}", "dim"))

#!/usr/bin/env python3
"""
window_agent.py — Interactive window inspection and LLM agent for OSScreenObserver.

Usage:
    python window_agent.py [--rest http://127.0.0.1:5001]

At startup you are prompted for:
    OpenWebUI base URL  (e.g. http://localhost:3000)
    OpenWebUI API key
    Model name          (e.g. llama3.2:3b or mistral)

The program then:
    1. Polls the REST server until it responds.
    2. Lists all open windows; you choose one.
    3. Displays the ASCII sketch + accessibility description.
    4. Opens an interactive prompt where you can type a task for the LLM.
    5. The LLM runs an agentic loop, calling screen tools (observe, click,
       type, press keys, get OCR, etc.) until the task is done or it stops.

No API credentials are saved to disk.
"""

import argparse
import json
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ─── ANSI colour helpers ──────────────────────────────────────────────────────

_NO_COLOR = not sys.stdout.isatty()

def _c(text: str, *codes: str) -> str:
    if _NO_COLOR:
        return text
    _MAP = {
        "bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
        "white": "\033[37m", "bright_white": "\033[97m",
    }
    return "".join(_MAP.get(c, "") for c in codes) + text + "\033[0m"

# ─── HTTP helpers (stdlib only, no extra deps) ────────────────────────────────

def _get(base: str, path: str, params: Optional[Dict] = None, timeout: int = 30) -> Any:
    url = base.rstrip("/") + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urllib.parse.urlencode(filtered)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise on any redirect so a POST is never silently converted to GET."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code,
                                     f"Redirect to {newurl} — update your base URL",
                                     headers, fp)

_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _post(base: str, path: str, data: Any, headers: Optional[Dict] = None,
          timeout: int = 60) -> Any:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

# ─── REST server poller ───────────────────────────────────────────────────────

def wait_for_server(rest_base: str, retries: int = 40, delay: float = 0.5) -> bool:
    print(_c("  Waiting for REST server …", "dim"), end="", flush=True)
    for _ in range(retries):
        try:
            _get(rest_base, "/api/windows", timeout=2)
            print(_c(" ready.", "green"))
            return True
        except Exception:
            print(".", end="", flush=True)
            time.sleep(delay)
    print(_c(" timed out.", "red"))
    return False

# ─── REST API wrappers ────────────────────────────────────────────────────────

def api_list_windows(rest: str) -> Dict:
    return _get(rest, "/api/windows")


def api_observe(rest: str, window_index: Optional[int]) -> Dict:
    params = {} if window_index is None else {"window_index": window_index}
    sketch = _get(rest, "/api/sketch", params)
    desc   = _get(rest, "/api/description", params)
    return {
        "window":      sketch.get("window", "unknown"),
        "sketch":      sketch.get("sketch", ""),
        "description": desc.get("description", ""),
    }


def api_element_tree(rest: str, window_index: Optional[int]) -> Dict:
    params = {} if window_index is None else {"window_index": window_index}
    return _get(rest, "/api/structure", params)


def api_description(rest: str, window_index: Optional[int], mode: str = "accessibility") -> Dict:
    params = {"mode": mode}
    if window_index is not None:
        params["window_index"] = window_index
    return _get(rest, "/api/description", params)


def api_sketch(rest: str, window_index: Optional[int],
               grid_width: Optional[int] = None, grid_height: Optional[int] = None,
               ocr: bool = False) -> Dict:
    params: Dict[str, Any] = {}
    if window_index is not None:
        params["window_index"] = window_index
    if grid_width is not None:
        params["grid_width"] = grid_width
    if grid_height is not None:
        params["grid_height"] = grid_height
    if ocr:
        params["ocr"] = "1"
    return _get(rest, "/api/sketch", params)


def api_screenshot(rest: str, window_index: Optional[int]) -> Dict:
    params = {} if window_index is None else {"window_index": window_index}
    return _get(rest, "/api/screenshot", params)


def api_full_screenshot(rest: str, window_index: Optional[int],
                        grid_width: Optional[int] = None,
                        grid_height: Optional[int] = None) -> Dict:
    params: Dict[str, Any] = {}
    if window_index is not None:
        params["window_index"] = window_index
    if grid_width is not None:
        params["grid_width"] = grid_width
    if grid_height is not None:
        params["grid_height"] = grid_height
    return _get(rest, "/api/full_screenshot", params)


def api_visible_areas(rest: str, window_index: int) -> Dict:
    return _get(rest, "/api/visible_areas", {"window_index": window_index})


def api_action(rest: str, payload: Dict) -> Dict:
    return _post(rest, "/api/action", payload)

# ─── Tool dispatcher (maps LLM tool names → REST calls) ──────────────────────

def dispatch_tool(tool_name: str, args: Dict, rest: str,
                  default_window: Optional[int]) -> Any:
    """
    Route a tool call from the LLM to the appropriate REST endpoint.
    Returns a Python object (will be JSON-serialised before sending back to LLM).
    """
    wi = args.get("window_index", default_window)

    if tool_name == "list_windows":
        return api_list_windows(rest)

    elif tool_name == "observe_window":
        return api_observe(rest, wi)

    elif tool_name == "get_element_tree":
        return api_element_tree(rest, wi)

    elif tool_name == "get_screen_description":
        mode = args.get("mode", "accessibility")
        return api_description(rest, wi, mode)

    elif tool_name == "get_screen_sketch":
        return api_sketch(rest, wi,
                          args.get("grid_width"),
                          args.get("grid_height"),
                          ocr=bool(args.get("ocr", False)))

    elif tool_name == "get_screenshot":
        result = api_screenshot(rest, wi)
        if "data" in result:
            result = {k: v for k, v in result.items() if k != "data"}
            result["note"] = (
                "Screenshot captured (base64 data omitted from tool result). "
                "Use get_screen_description with mode='ocr' or mode='vlm' for text content."
            )
        return result

    elif tool_name == "get_full_screenshot":
        result = api_full_screenshot(rest, wi, args.get("grid_width"), args.get("grid_height"))
        if "data" in result:
            result = {k: v for k, v in result.items() if k != "data"}
            result["note"] = "Screenshot captured (base64 data omitted). Sketch included above."
        return result

    elif tool_name == "get_visible_areas":
        return api_visible_areas(rest, wi if wi is not None else 0)

    elif tool_name == "click_at":
        payload = {
            "action": "click_at",
            "x": args["x"],
            "y": args["y"],
        }
        if "button" in args:
            payload["button"] = args["button"]
        if "double" in args:
            payload["double"] = args["double"]
        return api_action(rest, payload)

    elif tool_name == "type_text":
        return api_action(rest, {"action": "type", "value": args["text"]})

    elif tool_name == "press_key":
        return api_action(rest, {"action": "key", "value": args["keys"]})

    elif tool_name == "scroll":
        payload = {"action": "scroll"}
        for k in ("x", "y", "clicks"):
            if k in args:
                payload[k] = args[k]
        return api_action(rest, payload)

    else:
        return {"error": f"Unknown tool: {tool_name}"}

# ─── Tool definitions (OpenAI / OpenWebUI format) ─────────────────────────────

SCREEN_TOOLS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": (
                "Enumerate all visible top-level windows on the desktop. "
                "Returns index, title, process name, PID, and pixel geometry. "
                "Call this first to find the window_index needed by all other tools."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe_window",
            "description": (
                "Return the current visual state of a window as an ASCII sketch "
                "plus a prose accessibility description. "
                "Call before every action and after every action to verify the result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {
                        "type": "integer",
                        "description": "Index from list_windows (0-based). Omit for focused window.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_element_tree",
            "description": (
                "Return the full accessibility element tree as structured JSON. "
                "Each element has id, name, role, value, enabled, focused, "
                "keyboard_shortcut, and bounds {x, y, width, height} in absolute pixels. "
                "To click the center of an element: x + width//2, y + height//2. "
                "Use this when exact coordinates are needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {
                        "type": "integer",
                        "description": "Index from list_windows. Omit for focused window.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_screen_description",
            "description": (
                "Return a textual description of the window. "
                "mode='accessibility': serialize element tree to prose (default, instant). "
                "mode='ocr': extract visible text via Tesseract OCR on a screenshot. "
                "mode='vlm': send screenshot to a vision model (requires server config). "
                "mode='combined': all enabled modalities in one call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                    "mode": {
                        "type": "string",
                        "enum": ["accessibility", "ocr", "vlm", "combined"],
                        "description": "Analysis mode (default: accessibility).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_screen_sketch",
            "description": (
                "Render the accessibility element tree as an ASCII spatial layout diagram. "
                "Useful for understanding spatial relationships between controls. "
                "Set ocr=true to overlay Tesseract OCR text into blank grid cells for "
                "higher-fidelity representation of on-screen text (requires Tesseract)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                    "grid_width":   {"type": "integer", "description": "Output width in chars (default: 110)."},
                    "grid_height":  {"type": "integer", "description": "Output height in chars (default: 38)."},
                    "ocr":          {"type": "boolean", "description": "Enable OCR text overlay (default: false)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_screenshot",
            "description": (
                "Capture a screenshot of a window. Returns format/encoding metadata; "
                "the raw data is omitted from the tool result. "
                "For text content prefer get_screen_description with mode='ocr'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": (
                "Click at an absolute screen coordinate. "
                "Derive coordinates from get_element_tree bounds: "
                "click_x = x + width//2, click_y = y + height//2. "
                "Always call observe_window after clicking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x":      {"type": "integer", "description": "Absolute screen X in pixels."},
                    "y":      {"type": "integer", "description": "Absolute screen Y in pixels."},
                    "button": {"type": "string",  "enum": ["left", "right", "middle"]},
                    "double": {"type": "boolean", "description": "Double-click if true."},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "Type a string into the currently focused UI element. "
                "Click the target input field first to ensure it has focus."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type."}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": (
                "Press a key or key combination. Modifiers come first joined with '+'. "
                "Examples: 'enter', 'tab', 'escape', 'ctrl+s', 'ctrl+a', 'alt+f4'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string", "description": "Key or combination."}
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_screenshot",
            "description": (
                "Capture a screenshot AND render an ASCII sketch of a window in one call. "
                "The sketch uses OCR overlay for higher fidelity text. "
                "Screenshot pixel data is omitted from the tool result; the sketch is included. "
                "Prefer this over separate get_screenshot + get_screen_sketch calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer", "description": "Index from list_windows."},
                    "grid_width":   {"type": "integer", "description": "Sketch width in chars (default: 110)."},
                    "grid_height":  {"type": "integer", "description": "Sketch height in chars (default: 38)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_visible_areas",
            "description": (
                "Return visible (non-occluded, on-screen) bounding boxes for a window. "
                "Each region is {x, y, width, height} in absolute screen pixels. "
                "Use this to check that a click target is actually reachable before acting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer", "description": "Index from list_windows."},
                },
                "required": ["window_index"],
            },
        },
    },
]

SYSTEM_PROMPT = """\
You are a GUI automation agent operating on a live desktop.
You observe screen state through accessibility tools and execute mouse and keyboard actions.

COORDINATE RULE
All x, y values must come from get_element_tree bounds — never estimate or recall coordinates.
To click the centre of element with bounds {x, y, width, height}:
  click_x = x + width  // 2
  click_y = y + height // 2

OBSERVATION RULE
You are blind to the screen unless you call observe_window.
Call observe_window:
  - before deciding where to click or type
  - after every action, without exception, to confirm the result

WORKFLOW
1. list_windows — identify the target window_index
2. observe_window — understand current state
3. get_element_tree — get exact coordinates when needed
4. Execute exactly one action (click_at / type_text / press_key)
5. observe_window — verify the change
6. Repeat until the task is complete

If an action does not produce the expected result, do not repeat it;
re-observe and try an alternative approach.
"""

# ─── LLM client (OpenAI-compatible, OpenWebUI) ────────────────────────────────

class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.model    = model

    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict:
        payload: Dict[str, Any] = {
            "model":       self.model,
            "messages":    messages,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return _post(self.base_url, "/api/v1/chat/completions", payload, headers, timeout=120)

# ─── Agentic loop ─────────────────────────────────────────────────────────────

MAX_TURNS = 30

def run_agent(
    llm:            LLMClient,
    rest:           str,
    user_task:      str,
    window_index:   Optional[int],
    history:        List[Dict],
) -> List[Dict]:
    """
    Run the agentic tool-calling loop.

    Appends messages to *history* in place and returns the updated history.
    Prints progress to stdout using ANSI colours.
    """
    history.append({"role": "user", "content": user_task})
    print()
    print(_c(f"  User: {user_task}", "cyan"))

    for turn in range(MAX_TURNS):
        try:
            resp = llm.chat(history, tools=SCREEN_TOOLS)
        except urllib.error.URLError as e:
            print(_c(f"\n  [LLM request failed: {e}]", "red"))
            break
        except Exception as e:
            print(_c(f"\n  [LLM error: {e}]", "red"))
            traceback.print_exc()
            break

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

            # Dispatch to REST
            try:
                result = dispatch_tool(fn_name, fn_args, rest, window_index)
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
                print(_c(f"      [{w['index']}] {w['title']}{flag}", "dim"))
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
            mode = result.get("mode", "")
            desc = result.get("description", "")
            preview = (desc[:120] + "…") if len(desc) > 120 else desc
            print(_c(f"    ← [{mode}] {preview}", "dim"))

        else:
            # Generic: print first 200 chars of JSON
            raw = json.dumps(result)
            print(_c(f"    ← {raw[:200]}{'…' if len(raw) > 200 else ''}", "dim"))
    else:
        raw = str(result)
        print(_c(f"    ← {raw[:200]}{'…' if len(raw) > 200 else ''}", "dim"))

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


def ask_connection() -> Tuple[str, str, str]:
    """Interactively collect OpenWebUI connection parameters (model chosen after fetch)."""
    print(_c("\n  ── OpenWebUI / LLM Connection ──────────────────────────────\n", "bold"))
    base_url = prompt("  OpenWebUI base URL", "http://localhost:3000")
    api_key  = prompt("  API key (leave blank if none)", secret=True)
    return base_url, api_key, ""


def pick_model(models: List[str], fallback: str = "llama3.2:3b") -> str:
    """Display a numbered menu of *models* and return the chosen model id."""
    print(_c("\n  Available models:\n", "bold"))
    for i, m in enumerate(models):
        print(_c(f"    {i + 1:>3}. ", "yellow") + m)
    print()
    while True:
        raw = input(_c("  Select model (number or name): ", "bold", "cyan")).strip()
        if not raw:
            return fallback
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                return models[idx]
            print(_c(f"  Please enter a number between 1 and {len(models)}.", "red"))
        elif raw in models:
            return raw
        else:
            # Accept free-text entry for models not in the list
            confirm = input(_c(f"  '{raw}' not in list — use it anyway? [y/N] ", "yellow")).strip().lower()
            if confirm == "y":
                return raw


def list_models(llm_base: str, api_key: str) -> List[str]:
    """Try to fetch model list from /api/v1/models. Returns empty list on failure."""
    try:
        headers: Dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            llm_base.rstrip("/") + "/api/v1/models",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return [m["id"] for m in data.get("data", [])]
    except Exception as e:
        print(_c(f" failed ({e})", "red"))
        return []

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
    llm_base, api_key, _ = ask_connection()

    print(_c(f"\n  Checking connection to {llm_base} …", "dim"), end="", flush=True)
    models = list_models(llm_base, api_key)
    if models:
        print(_c(f" OK  ({len(models)} model(s) available)", "green"))
        model = pick_model(models)
    else:
        print(_c(" (could not list models)", "yellow"))
        model = prompt("  Model name", "llama3.2:3b")

    llm = LLMClient(llm_base, api_key, model)

    # ── 3. Main window-selection loop ─────────────────────────────────────────
    conversation: List[Dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    selected_window: Optional[int] = None

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
        win_title = next((w["title"] for w in windows if w["index"] == chosen), str(chosen))

        # ── 4. Window inspection sub-loop ────────────────────────────────────
        print(_c(f"\n  Loading window [{chosen}] {win_title} …", "dim"))
        try:
            view = api_observe(rest, selected_window)
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
                    view = api_observe(rest, selected_window)
                except Exception as e:
                    print(_c(f"  [Failed: {e}]", "red"))
                    continue
                print_window_view(view)
                continue

            if raw2.lower() in ("r", "refresh"):
                try:
                    view = api_observe(rest, selected_window)
                except Exception as e:
                    print(_c(f"  [Failed: {e}]", "red"))
                    continue
                print_window_view(view)
                continue

            # Treat anything else as a task for the LLM agent
            print()
            print(_c(f"  ── Running agent for task on window [{selected_window}] ──────────",
                     "magenta", "bold"))
            conversation = run_agent(llm, rest, raw2, selected_window, conversation)
            print()
            print(_c("  ── Agent finished ───────────────────────────────────────────",
                     "magenta"))
            print(_c("  Type 'v' to view the window's current state, or enter another task.",
                     "dim"))
            print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(_c("\n\n  Interrupted.\n", "dim"))

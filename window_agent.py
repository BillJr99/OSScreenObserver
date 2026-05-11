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

def _win_params(uid: Optional[str], index: Optional[int],
                title: Optional[str] = None) -> Dict[str, Any]:
    """Return the minimal window-selector dict: uid > index > title substring."""
    if uid:
        return {"window_uid": uid}
    if index is not None:
        return {"window_index": index}
    if title:
        return {"window_title": title}
    return {}


def api_list_windows(rest: str) -> Dict:
    return _get(rest, "/api/windows")


def api_observe(rest: str, uid: Optional[str], index: Optional[int],
                title: Optional[str] = None) -> Dict:
    params = _win_params(uid, index, title)
    sketch = _get(rest, "/api/sketch", params)
    desc   = _get(rest, "/api/description", params)
    return {
        "window":      sketch.get("window", "unknown"),
        "sketch":      sketch.get("sketch", ""),
        "description": desc.get("description", ""),
    }


def api_element_tree(rest: str, uid: Optional[str], index: Optional[int],
                     title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/structure", _win_params(uid, index, title))


def api_description(rest: str, uid: Optional[str], index: Optional[int],
                    title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/description", _win_params(uid, index, title))


def api_sketch(rest: str, uid: Optional[str], index: Optional[int],
               grid_width: Optional[int] = None, grid_height: Optional[int] = None,
               ocr: bool = False, title: Optional[str] = None) -> Dict:
    params: Dict[str, Any] = _win_params(uid, index, title)
    if grid_width is not None:
        params["grid_width"] = grid_width
    if grid_height is not None:
        params["grid_height"] = grid_height
    if ocr:
        params["ocr"] = "1"
    return _get(rest, "/api/sketch", params)


def api_screenshot(rest: str, uid: Optional[str], index: Optional[int],
                   title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/screenshot", _win_params(uid, index, title))


def api_full_screenshot(rest: str, uid: Optional[str], index: Optional[int],
                        grid_width: Optional[int] = None,
                        grid_height: Optional[int] = None,
                        title: Optional[str] = None) -> Dict:
    params: Dict[str, Any] = _win_params(uid, index, title)
    if grid_width is not None:
        params["grid_width"] = grid_width
    if grid_height is not None:
        params["grid_height"] = grid_height
    return _get(rest, "/api/full_screenshot", params)


def api_visible_areas(rest: str, uid: Optional[str], index: Optional[int],
                      title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/visible_areas", _win_params(uid, index, title))


def api_bring_to_foreground(rest: str, uid: Optional[str], index: Optional[int],
                            title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/bring_to_foreground", _win_params(uid, index, title))


def api_action(rest: str, payload: Dict) -> Dict:
    return _post(rest, "/api/action", payload)

# ─── Tool dispatcher (maps LLM tool names → REST calls) ──────────────────────

def dispatch_tool(tool_name: str, args: Dict, rest: str,
                  default_uid: Optional[str] = None,
                  default_index: Optional[int] = None) -> Any:
    """
    Route a tool call from the LLM to the appropriate REST endpoint.
    Returns a Python object (will be JSON-serialised before sending back to LLM).
    """
    uid: Optional[str]   = args.get("window_uid")
    wi: Optional[int]    = args.get("window_index") if "window_index" in args else None
    title: Optional[str] = args.get("window_title")
    # Apply defaults only when the LLM specified none of uid / index / title.
    if uid is None and wi is None and title is None:
        uid = default_uid
        wi  = default_index

    if tool_name == "list_windows":
        return api_list_windows(rest)

    elif tool_name == "observe_window":
        return api_observe(rest, uid, wi, title)

    elif tool_name == "get_element_tree":
        return api_element_tree(rest, uid, wi, title)

    elif tool_name == "get_screen_description":
        return api_description(rest, uid, wi, title)

    elif tool_name == "get_screen_sketch":
        raw_ocr = args.get("ocr", False)
        if isinstance(raw_ocr, str):
            ocr = raw_ocr.strip().lower() not in ("", "false", "0", "no")
        else:
            ocr = bool(raw_ocr)
        return api_sketch(rest, uid, wi, args.get("grid_width"), args.get("grid_height"), ocr=ocr, title=title)

    elif tool_name == "get_screenshot":
        result = api_screenshot(rest, uid, wi, title)
        if "data" in result:
            result = {k: v for k, v in result.items() if k != "data"}
            result["note"] = (
                "Screenshot captured (base64 data omitted from tool result). "
                "Use get_screen_description for text content (OCR + VLM when available)."
            )
        return result

    elif tool_name == "get_full_screenshot":
        result = api_full_screenshot(rest, uid, wi, args.get("grid_width"), args.get("grid_height"), title=title)
        if "data" in result:
            result = {k: v for k, v in result.items() if k != "data"}
            note = "Screenshot captured (base64 data omitted)."
            if result.get("sketch"):
                note += " Sketch included above."
            result["note"] = note
        return result

    elif tool_name == "get_visible_areas":
        if uid is None and wi is None and title is None:
            return {"error": "get_visible_areas requires a selected window (window_uid, window_index, or window_title)"}
        return api_visible_areas(rest, uid, wi, title)

    elif tool_name == "bring_to_foreground":
        if uid is None and wi is None and title is None:
            return {"error": "bring_to_foreground requires a selected window (window_uid, window_index, or window_title)"}
        return api_bring_to_foreground(rest, uid, wi, title)

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

    # ── P1: identity / discovery ────────────────────────────────────────────
    elif tool_name == "get_capabilities":
        return _get(rest, "/api/capabilities")

    elif tool_name == "get_monitors":
        return _get(rest, "/api/monitors")

    elif tool_name == "find_element":
        params: Dict[str, Any] = {"selector": args["selector"]}
        if "window_uid" in args:
            params["window_uid"] = args["window_uid"]
        elif wi is not None:
            params["window_index"] = wi
        return _get(rest, "/api/find_element", params)

    # ── P1 / P6: element-targeted actions ───────────────────────────────────
    elif tool_name in ("click_element", "focus_element", "invoke_element",
                       "set_value", "select_option",
                       "right_click_element", "double_click_element",
                       "hover_element", "clear_text", "key_into_element"):
        body = dict(args)
        if "window_index" not in body and "window_uid" not in body and wi is not None:
            body["window_index"] = wi
        path = {
            "click_element":         "/api/element/click",
            "focus_element":         "/api/element/focus",
            "invoke_element":        "/api/element/invoke",
            "set_value":             "/api/element/set_value",
            "select_option":         "/api/element/select",
            "right_click_element":   "/api/element/right_click",
            "double_click_element":  "/api/element/double_click",
            "hover_element":         "/api/hover",
            "clear_text":            "/api/element/clear_text",
            "key_into_element":      "/api/element/key",
        }[tool_name]
        return _post(rest, path, body)

    elif tool_name == "hover_at":
        return _post(rest, "/api/hover", {k: args[k] for k in args
                                          if k in ("x", "y", "hover_ms")})

    elif tool_name == "drag":
        body = dict(args)
        if "window_index" not in body and "window_uid" not in body and wi is not None:
            body["window_index"] = wi
        return _post(rest, "/api/drag", body)

    # ── P2: synchronisation and observation ─────────────────────────────────
    elif tool_name == "wait_for":
        body = dict(args)
        return _post(rest, "/api/wait_for", body)

    elif tool_name == "wait_idle":
        body = dict(args)
        if "window_index" not in body and "window_uid" not in body and wi is not None:
            body["window_index"] = wi
        return _post(rest, "/api/wait_idle", body)

    elif tool_name == "observe_window_diff":
        params: Dict[str, Any] = {}
        if "window_uid" in args:
            params["window_uid"] = args["window_uid"]
        elif wi is not None:
            params["window_index"] = wi
        if "since" in args:
            params["since"] = args["since"]
        if "format" in args:
            params["format"] = args["format"]
        return _get(rest, "/api/observe", params)

    elif tool_name in ("click_element_and_observe", "type_and_observe",
                       "press_key_and_observe"):
        path = {
            "click_element_and_observe": "/api/element/click_and_observe",
            "type_and_observe":          "/api/type_and_observe",
            "press_key_and_observe":     "/api/key_and_observe",
        }[tool_name]
        body = dict(args)
        if "window_index" not in body and "window_uid" not in body and wi is not None:
            body["window_index"] = wi
        return _post(rest, path, body)

    # ── P2: snapshots ───────────────────────────────────────────────────────
    elif tool_name == "snapshot":
        return _post(rest, "/api/snapshot", {})

    elif tool_name == "snapshot_get":
        return _get(rest, f"/api/snapshot/{args['snapshot_id']}")

    elif tool_name == "snapshot_diff":
        return _post(rest, "/api/snapshot/diff",
                     {k: args[k] for k in args if k in ("a", "b", "format")})

    elif tool_name == "snapshot_drop":
        sid = args["snapshot_id"]
        url = rest.rstrip("/") + f"/api/snapshot/{sid}"
        req = urllib.request.Request(url, method="DELETE")
        with _NO_REDIRECT_OPENER.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    # ── P4: tracing, replay, scenarios, oracles ─────────────────────────────
    elif tool_name == "trace_start":
        return _post(rest, "/api/trace/start",
                     {"label": args.get("label", "")})
    elif tool_name == "trace_stop":
        return _post(rest, "/api/trace/stop", {})
    elif tool_name == "trace_status":
        return _get(rest, "/api/trace/status")

    elif tool_name == "replay_start":
        return _post(rest, "/api/replay/start",
                     {k: args[k] for k in args
                      if k in ("path", "mode", "on_divergence")})
    elif tool_name == "replay_step":
        return _post(rest, "/api/replay/step",
                     {"replay_id": args["replay_id"]})
    elif tool_name == "replay_status":
        return _post(rest, "/api/replay/status",
                     {"replay_id": args["replay_id"]})
    elif tool_name == "replay_stop":
        return _post(rest, "/api/replay/stop",
                     {"replay_id": args["replay_id"]})

    elif tool_name == "load_scenario":
        return _post(rest, "/api/scenario/load", {"path": args["path"]})

    elif tool_name == "assert_state":
        return _post(rest, "/api/assert_state",
                     {"predicate": args.get("predicate", args.get("predicates", []))})

    # ── P5: safety & status ─────────────────────────────────────────────────
    elif tool_name == "get_budget_status":
        return _get(rest, "/api/budget_status")

    elif tool_name == "get_redaction_status":
        return _get(rest, "/api/redaction_status")

    elif tool_name == "propose_action":
        return _post(rest, "/api/propose_action",
                     {"action": args["action"], "args": args.get("args", {})})

    # ── P3 extras ───────────────────────────────────────────────────────────
    elif tool_name == "get_screenshot_cropped":
        # Pixel data is huge — same omission policy as get_screenshot.
        params: Dict[str, Any] = {}
        if "window_uid" in args:
            params["window_uid"] = args["window_uid"]
        elif wi is not None:
            params["window_index"] = wi
        for k in ("element_id", "padding_px", "max_width"):
            if k in args:
                params[k] = args[k]
        result = _get(rest, "/api/screenshot/cropped", params)
        if "data" in result:
            result = {k: v for k, v in result.items() if k != "data"}
            result["note"] = "Screenshot captured (base64 data omitted)."
        return result

    elif tool_name == "get_ocr":
        params: Dict[str, Any] = {}
        if "window_uid" in args:
            params["window_uid"] = args["window_uid"]
        elif wi is not None:
            params["window_index"] = wi
        if "element_id" in args:
            params["element_id"] = args["element_id"]
        return _get(rest, "/api/ocr", params)

    # ── Catch-all: drive any registered tool by name ────────────────────────
    elif tool_name == "call_tool":
        name = args["name"]
        body = args.get("args", {}) or {}
        return _post(rest, f"/api/tool/{name}", body)

    else:
        return {"error": f"Unknown tool: {tool_name}"}

# ─── Tool definitions (OpenAI / OpenWebUI format) ─────────────────────────────

# ─── Full tool catalogue (trimmed descriptions) ──────────────────────────────
# Each entry is the schema sent to the LLM.  Tiers and keyword groups are in
# _TOOL_TIER / _KEYWORD_GROUPS below — not in the dict itself so the JSON
# payload stays clean.

SCREEN_TOOLS: List[Dict] = [
    # ── Core observation ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": "List visible desktop windows. Returns index, title, process, PID, geometry, and window_uid. Prefer window_uid over index — index changes after every focus change.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe_window",
            "description": "ASCII sketch + accessibility description of a window. Call before and after every action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer", "description": "From list_windows. Omit for focused window."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_element_tree",
            "description": "Accessibility tree as JSON. Each node has id, name, role, value, enabled, focused, bounds {x,y,width,height}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_screen_description",
            "description": "Describe a window using all available sources: accessibility tree, OCR, and VLM (when enabled). Returns everything available in one call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                    "window_uid":   {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_screen_sketch",
            "description": "ASCII spatial layout of a window. ocr=true overlays Tesseract text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                    "grid_width":   {"type": "integer"},
                    "grid_height":  {"type": "integer"},
                    "ocr":          {"type": "boolean"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_screenshot",
            "description": "Capture a window screenshot. Pixel data omitted; use get_screen_description mode=ocr for text.",
            "parameters": {
                "type": "object",
                "properties": {"window_index": {"type": "integer"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_screenshot",
            "description": "Full-display screenshot + ASCII sketch with OCR for a window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                    "grid_width":   {"type": "integer"},
                    "grid_height":  {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_visible_areas",
            "description": "Non-occluded bounding boxes {x,y,width,height} for a window.",
            "parameters": {
                "type": "object",
                "properties": {"window_index": {"type": "integer"}},
                "required": ["window_index"],
            },
        },
    },

    # ── Core actions ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": "Click at absolute screen coordinates. Derive from get_element_tree: cx=x+w//2, cy=y+h//2.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x":      {"type": "integer"},
                    "y":      {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "double": {"type": "boolean"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the focused element. Click the field first.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a key or chord. Examples: enter, tab, escape, ctrl+s, alt+f4.",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string"}},
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll at (x,y). clicks>0 scrolls up, clicks<0 scrolls down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "clicks":       {"type": "integer"},
                    "x":            {"type": "integer"},
                    "y":            {"type": "integer"},
                    "window_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bring_to_foreground",
            "description": "Raise a window to the foreground by clicking its title bar. Window indices change after this call — always call list_windows again before using any index.",
            "parameters": {
                "type": "object",
                "properties": {"window_index": {"type": "integer"}},
                "required": ["window_index"],
            },
        },
    },

    # ── Element-targeted actions ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "find_element",
            "description": 'Resolve selector to element_id + bounds. XPath: Window/Pane/Button[name="OK"]. CSS: Window > Button.',
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": "Click by selector or element_id. Returns ActionReceipt (changed, new_dialogs, before/after hashes).",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":      {"type": "string"},
                    "element_id":    {"type": "string"},
                    "window_uid":    {"type": "string"},
                    "window_index":  {"type": "integer"},
                    "button":        {"type": "string", "enum": ["left", "right", "middle"]},
                    "count":         {"type": "integer"},
                    "dry_run":       {"type": "boolean"},
                    "confirm_token": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_element_and_observe",
            "description": "Click element then return post-click observation diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":      {"type": "string"},
                    "element_id":    {"type": "string"},
                    "window_uid":    {"type": "string"},
                    "window_index":  {"type": "integer"},
                    "button":        {"type": "string"},
                    "wait_after_ms": {"type": "integer"},
                    "since":         {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_and_observe",
            "description": "type_text + observe_window in one round-trip.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text":          {"type": "string"},
                    "wait_after_ms": {"type": "integer"},
                    "since":         {"type": "string"},
                    "window_uid":    {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key_and_observe",
            "description": "press_key + observe_window in one round-trip.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys":          {"type": "string"},
                    "wait_after_ms": {"type": "integer"},
                    "since":         {"type": "string"},
                    "window_uid":    {"type": "string"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click_element",
            "description": "Right-click an element by selector or element_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click_element",
            "description": "Double-click an element by selector or element_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Open a combo-box / list and select an option by name or zero-based index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                    "option_name":  {"type": "string"},
                    "option_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_value",
            "description": "Click an editable element, optionally clear it, then type a value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                    "value":        {"type": "string"},
                    "clear_first":  {"type": "boolean"},
                },
                "required": ["value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_element",
            "description": "Give keyboard focus to an element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "invoke_element",
            "description": "Invoke an element's primary action (UIA InvokePattern or click fallback).",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_text",
            "description": "Click an editable element then select-all + delete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_into_element",
            "description": "Click an element then press a key combination atomically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                    "keys":         {"type": "string"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover_at",
            "description": "Move mouse to (x, y) and hold hover_ms ms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"}, "y": {"type": "integer"},
                    "hover_ms": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover_element",
            "description": "Move mouse to an element's centre and hold hover_ms ms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector":     {"type": "string"},
                    "element_id":   {"type": "string"},
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                    "hover_ms":     {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag",
            "description": "Drag from one point/element to another. Each endpoint: {x,y} or {selector} or {element_id}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from":        {"type": "object"},
                    "to":          {"type": "object"},
                    "modifiers":   {"type": "array", "items": {"type": "string"}},
                    "duration_s":  {"type": "number"},
                    "window_index": {"type": "integer"},
                    "window_uid":   {"type": "string"},
                },
                "required": ["from", "to"],
            },
        },
    },

    # ── Synchronisation ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "wait_for",
            "description": "Block until a condition matches or timeout. Conditions: element_appears, element_disappears, text_visible, window_appears, tree_changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "any_of":     {"type": "array"},
                    "window_uid": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                    "poll_ms":    {"type": "integer"},
                },
                "required": ["any_of"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_idle",
            "description": "Block until the accessibility tree is stable for quiet_ms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                    "quiet_ms":     {"type": "integer"},
                    "timeout_ms":   {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe_window_diff",
            "description": "observe_window but returns only the diff since a tree_token.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_uid":   {"type": "string"},
                    "window_index": {"type": "integer"},
                    "since":        {"type": "string"},
                    "format":       {"type": "string", "enum": ["custom", "json-patch"]},
                },
                "required": [],
            },
        },
    },

    # ── Snapshots ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "snapshot",
            "description": "Capture all windows + trees. Returns snapshot_id.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_get",
            "description": "Retrieve a saved snapshot.",
            "parameters": {
                "type": "object",
                "properties": {"snapshot_id": {"type": "string"}},
                "required": ["snapshot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_diff",
            "description": "Diff two snapshots (added/removed windows + per-window tree diff).",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"}, "b": {"type": "string"},
                    "format": {"type": "string", "enum": ["custom", "json-patch"]},
                },
                "required": ["a", "b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_drop",
            "description": "Free a snapshot before its TTL expires.",
            "parameters": {
                "type": "object",
                "properties": {"snapshot_id": {"type": "string"}},
                "required": ["snapshot_id"],
            },
        },
    },

    # ── Tracing / replay ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "trace_start",
            "description": "Start recording tool calls to a JSONL trace file.",
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trace_stop",
            "description": "Close the active trace.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trace_status",
            "description": "Report whether a trace is active.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replay_start",
            "description": "Load a trace for replay (execute or verify mode).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":          {"type": "string"},
                    "mode":          {"type": "string", "enum": ["execute", "verify"]},
                    "on_divergence": {"type": "string", "enum": ["stop", "warn", "resume"]},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replay_step",
            "description": "Advance one row of the active replay.",
            "parameters": {
                "type": "object",
                "properties": {"replay_id": {"type": "string"}},
                "required": ["replay_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replay_status",
            "description": "Report replay position, total, finished, divergences.",
            "parameters": {
                "type": "object",
                "properties": {"replay_id": {"type": "string"}},
                "required": ["replay_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replay_stop",
            "description": "Free a replay handle.",
            "parameters": {
                "type": "object",
                "properties": {"replay_id": {"type": "string"}},
                "required": ["replay_id"],
            },
        },
    },

    # ── Testing / harness ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "load_scenario",
            "description": "Load a YAML scenario file into the mock adapter.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assert_state",
            "description": "Evaluate predicates against current state. Returns all_passed. Predicates: element_exists, value_equals, text_visible, window_focused, screenshot_similar, …",
            "parameters": {
                "type": "object",
                "properties": {
                    "predicate":  {"type": "array"},
                    "predicates": {"type": "array"},
                },
                "required": [],
            },
        },
    },

    # ── Safety / budget ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_budget_status",
            "description": "Remaining budget: actions, screenshots, vlm_tokens, …",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_redaction_status",
            "description": "Redaction enabled state and applied count.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": "Issue a confirm_token for a destructive action. Pass the token back to the action to proceed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "args":   {"type": "object"},
                },
                "required": ["action"],
            },
        },
    },

    # ── Discovery ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_capabilities",
            "description": "Features the server supports on this host.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_monitors",
            "description": "Monitors with bounds and scale factor.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },

    # ── OCR extras ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_screenshot_cropped",
            "description": "Cropped screenshot around an element or bbox, with optional max_width downscale.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                    "window_uid":   {"type": "string"},
                    "element_id":   {"type": "string"},
                    "padding_px":   {"type": "integer"},
                    "max_width":    {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ocr",
            "description": "Region-scoped OCR. Returns [{text, confidence, bbox}].",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {"type": "integer"},
                    "window_uid":   {"type": "string"},
                    "element_id":   {"type": "string"},
                },
                "required": [],
            },
        },
    },

    # ── Escape hatch ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "call_tool",
            "description": "Call any server tool by name with arbitrary JSON args.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args": {"type": "object"},
                },
                "required": ["name"],
            },
        },
    },
]

# ─── Tool index, tiers, and keyword groups ────────────────────────────────────

_TOOL_BY_NAME: Dict[str, Dict] = {
    t["function"]["name"]: t for t in SCREEN_TOOLS
}

# "always"   — sent every turn regardless of task
# "usually"  — sent by default (covers the vast majority of tasks)
# "on_demand"— omitted unless the task keywords or request_tools activates them
_TOOL_TIER: Dict[str, str] = {
    # always
    "list_windows":              "always",
    "observe_window":            "always",
    "get_element_tree":          "always",
    "find_element":              "always",
    "click_element":             "always",
    "type_text":                 "always",
    "press_key":                 "always",
    "scroll":                    "always",
    "click_element_and_observe": "always",
    # usually
    "bring_to_foreground":       "usually",
    "right_click_element":       "usually",
    "double_click_element":      "usually",
    "select_option":             "usually",
    "set_value":                 "usually",
    "wait_idle":                 "usually",
    "type_and_observe":          "usually",
    "press_key_and_observe":     "usually",
    "get_screen_description":    "usually",
    # on_demand (everything else)
    "get_screen_sketch":         "on_demand",
    "get_screenshot":            "on_demand",
    "get_full_screenshot":       "on_demand",
    "get_visible_areas":         "on_demand",
    "get_screenshot_cropped":    "on_demand",
    "get_ocr":                   "on_demand",
    "hover_at":                  "on_demand",
    "hover_element":             "on_demand",
    "drag":                      "on_demand",
    "key_into_element":          "on_demand",
    "clear_text":                "on_demand",
    "focus_element":             "on_demand",
    "invoke_element":            "on_demand",
    "click_at":                  "on_demand",
    "wait_for":                  "on_demand",
    "observe_window_diff":       "on_demand",
    "snapshot":                  "on_demand",
    "snapshot_get":              "on_demand",
    "snapshot_diff":             "on_demand",
    "snapshot_drop":             "on_demand",
    "trace_start":               "on_demand",
    "trace_stop":                "on_demand",
    "trace_status":              "on_demand",
    "replay_start":              "on_demand",
    "replay_step":               "on_demand",
    "replay_status":             "on_demand",
    "replay_stop":               "on_demand",
    "load_scenario":             "on_demand",
    "assert_state":              "on_demand",
    "get_budget_status":         "on_demand",
    "get_redaction_status":      "on_demand",
    "propose_action":            "on_demand",
    "get_capabilities":          "on_demand",
    "get_monitors":              "on_demand",
    "call_tool":                 "on_demand",
}

# Task keywords that unlock specific on_demand tools without a request_tools call.
_KEYWORD_GROUPS: Dict[str, List[str]] = {
    "drag":          ["drag"],
    "drop":          ["drag"],
    "move":          ["drag"],
    "hover":         ["hover_at", "hover_element"],
    "tooltip":       ["hover_at", "hover_element"],
    "screenshot":    ["get_full_screenshot", "get_screenshot"],
    "capture":       ["get_full_screenshot"],
    "sketch":        ["get_screen_sketch"],
    "ocr":           ["get_ocr", "get_screen_description"],
    "read text":     ["get_ocr"],
    "cropped":       ["get_screenshot_cropped"],
    "visible area":  ["get_visible_areas"],
    "occluded":      ["get_visible_areas"],
    "snapshot":      ["snapshot", "snapshot_get", "snapshot_diff", "snapshot_drop"],
    "compare":       ["snapshot", "snapshot_diff"],
    "diff":          ["observe_window_diff", "snapshot_diff"],
    "wait for":      ["wait_for"],
    "appear":        ["wait_for"],
    "loading":       ["wait_for"],
    "coordinates":   ["click_at"],
    "pixel":         ["click_at"],
    "clear":         ["clear_text"],
    "erase":         ["clear_text"],
    "focus":         ["focus_element"],
    "invoke":        ["invoke_element"],
    "trace":         ["trace_start", "trace_stop", "trace_status"],
    "record":        ["trace_start", "trace_stop", "trace_status"],
    "replay":        ["replay_start", "replay_step", "replay_status", "replay_stop"],
    "playback":      ["replay_start", "replay_step", "replay_status", "replay_stop"],
    "assert":        ["assert_state"],
    "verify state":  ["assert_state"],
    "budget":        ["get_budget_status"],
    "redact":        ["get_redaction_status"],
    "capabilities":  ["get_capabilities"],
    "monitors":      ["get_monitors"],
}

# Meta-tools always included — handled locally in the agent loop, never sent to REST.
_META_TOOLS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_available_tools",
            "description": (
                "List tools not yet active in this session, with one-line descriptions. "
                "Call request_tools with the names you need to activate them."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_tools",
            "description": (
                "Activate additional tools by name for the rest of this session. "
                "Use list_available_tools first to see what is available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tool names to activate.",
                    }
                },
                "required": ["names"],
            },
        },
    },
]


def _initial_active_tools(task: str) -> set:
    """Return the set of tool names to send on turn 1 based on tier + keywords."""
    active = {name for name, tier in _TOOL_TIER.items() if tier in ("always", "usually")}
    task_lower = task.lower()
    for kw, names in _KEYWORD_GROUPS.items():
        if kw in task_lower:
            active.update(names)
    return active


def _tool_defs_for(active: set) -> List[Dict]:
    """Build the tools list to send to the LLM from the active name set."""
    defs = [_TOOL_BY_NAME[n] for n in active if n in _TOOL_BY_NAME]
    return defs + _META_TOOLS


SYSTEM_PROMPT = """\
You are a GUI automation agent operating on a live desktop.
You observe screen state through accessibility tools and execute mouse and keyboard actions.

COORDINATE RULE
All x, y values must come from get_element_tree bounds — never estimate or recall coordinates.
To click the centre of an element with bounds {x, y, width, height}:
  click_x = x + width  // 2
  click_y = y + height // 2

OBSERVATION RULE
You are blind to the screen unless you call observe_window or get_screen_description.
Call observe_window before deciding where to act, and after every action to confirm the result.
Always read the window title from the observe_window result and confirm it matches the window
you intend to act on before proceeding.

FINDING ELEMENTS — IMPORTANT
The accessibility tree may be incomplete for web pages and some applications.
If a selector or element search fails with NOT FOUND:
  1. Call get_screen_description to get accessibility + OCR + visual text all at once.
  2. Use get_screenshot and inspect the image to identify element positions visually.
  3. Fall back to click_at with coordinates derived from element bounds in the sketch/screenshot.
Do not give up after one NOT FOUND — always try the screenshot/OCR path before reporting failure.

WINDOW INDEX INSTABILITY — CRITICAL
window_index values change every time a window is raised, minimised, or closed.
• Every window tool call returns window_uid in its response — capture it immediately and use it
  on all subsequent calls for that window instead of window_index.
• When you must call by window_index, the server auto-resolves it to the uid and returns
  window_uid in the result — read that value and switch to uid= from that point on.
• Never assume the same index still refers to the same window between tool calls.

BROWSER TAB SWITCHING
Browser tab bars appear as TabItem elements in the accessibility tree.
To switch to a different tab: observe_window, then click_element on the correct TabItem.
The window title updates to reflect the active tab after the click.

TASK COMPLETION
Complete every part of the user's task before stopping.
Do not ask for clarification or next steps mid-task when the task is unambiguous.
Only report done when all sub-tasks are finished.

WORKFLOW
1. list_windows — note window_uid for the target window; use uid on all future calls
2. bring_to_foreground(window_uid=…) — raise the window (result includes window_uid if you used index)
3. observe_window(window_uid=…) — verify window title matches; understand current state
4. get_element_tree(window_uid=…) — get exact coordinates when needed
5. Execute one action
6. observe_window — verify title still matches and the change occurred
7. Repeat until ALL sub-tasks are complete

TOOL AVAILABILITY
Only a subset of tools is active at session start to keep context short.
  • list_available_tools() — see what else exists
  • request_tools(names=[…]) — activate specific tools for this session

If an action does not produce the expected result, re-observe and try an alternative approach.
"""

# OpenWebUI OpenAI-compatible API prefix (centralised so both endpoints stay in sync)
_OWU_PREFIX = "/api/v1"

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
        return _post(self.base_url, f"{_OWU_PREFIX}/chat/completions", payload, headers, timeout=240)

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(_c("\n\n  Interrupted.\n", "dim"))

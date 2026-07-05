"""
Tool dispatcher: maps LLM tool names to REST calls.

Split out of window_agent.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Optional

from window_agent.client import (
    _NO_REDIRECT_OPENER, _get, _post,
    api_action, api_bring_to_foreground, api_description,
    api_element_tree, api_full_screenshot, api_list_windows,
    api_observe, api_screenshot, api_sketch, api_visible_areas,
)

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
        params = {}
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
        params = {}
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
        params = {}
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

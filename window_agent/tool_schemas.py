"""
LLM tool catalogue: schemas, tiers, keyword groups and
meta-tools for on-demand activation.

Split out of window_agent.py (P3); behavior is unchanged.
"""

from __future__ import annotations

from typing import Dict, List

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

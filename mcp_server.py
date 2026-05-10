"""
mcp_server.py — MCP stdio server (Model Context Protocol, 2024-11-05).

Implements the MCP protocol as JSON-RPC 2.0 over stdin/stdout so that any
MCP-capable client (Claude Desktop, Claude Code, etc.) can use this server
as a tool provider.

ALL output to stdout is MCP protocol JSON.  All logging goes to stderr so
that the MCP framing on stdout is never polluted.

Exposed tools
─────────────
  list_windows          Enumerate visible top-level windows.
  get_window_structure  Accessibility element tree (JSON).
  get_screen_description Prose description (accessibility / OCR / VLM / combined).
  get_screen_sketch     ASCII spatial layout diagram.
  get_screenshot        Screenshot as base64 PNG.
  click_at              Click at pixel coordinates.
  type_text             Type text into the focused element.
  press_key             Press a key or key combination.
"""

import json
import logging
import sys
import traceback
from typing import Any, Dict, List, Optional

from observer import ScreenObserver, WindowInfo
from ascii_renderer import ASCIIRenderer
from description import DescriptionGenerator
import tools as _tools

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema definitions
# ─────────────────────────────────────────────────────────────────────────────

_TOOLS: List[Dict] = [
    {
        "name": "list_windows",
        "description": (
            "Enumerate all visible top-level windows on the desktop. "
            "Returns index, title, process name, PID, geometry, and focus state. "
            "Use the returned index values to target subsequent tool calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_window_structure",
        "description": (
            "Return the accessibility element tree for a window as structured JSON. "
            "Each node carries id, name, role, value, bounds, enabled, focused, "
            "keyboard_shortcut, and a children array.  Supports server-side "
            "filtering: roles, exclude_roles, name_regex, visible_only, "
            "max_text_len, prune_empty, max_nodes (with page_cursor)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_index":  {"type": "integer"},
                "window_uid":    {"type": "string"},
                "roles":         {"type": "array", "items": {"type": "string"}},
                "exclude_roles": {"type": "array", "items": {"type": "string"}},
                "name_regex":    {"type": "string"},
                "visible_only":  {"type": "boolean"},
                "max_text_len":  {"type": "integer"},
                "prune_empty":   {"type": "boolean"},
                "max_nodes":     {"type": "integer"},
                "page_cursor":   {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_screen_description",
        "description": (
            "Generate a textual description of the current screen state. "
            "mode='accessibility' serializes the element tree into prose. "
            "mode='ocr' extracts visible text via Tesseract. "
            "mode='vlm' uses Claude Vision (requires API key). "
            "mode='combined' returns all enabled modalities. "
            "mode='auto' picks accessibility/OCR/VLM based on tree richness "
            "and config; the chosen mode is reported as effective_mode. "
            "Pass max_tokens for an approximate output cap and focus_element "
            "to describe a subtree."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_index":  {"type": "integer"},
                "window_uid":    {"type": "string"},
                "mode":          {"type": "string",
                                  "enum": ["accessibility", "ocr", "vlm",
                                           "combined", "auto"]},
                "max_tokens":    {"type": "integer"},
                "focus_element": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_screen_sketch",
        "description": (
            "Render the accessibility element tree as an ASCII spatial layout sketch. "
            "Each UI element appears as a labeled box; positions reflect actual screen "
            "geometry scaled to the character grid. Useful for understanding spatial "
            "relationships between controls without image processing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_index": {
                    "type": "integer",
                    "description": "Window index from list_windows. Omit for focused window.",
                },
                "grid_width": {
                    "type": "integer",
                    "description": "Sketch width in characters (default: 110).",
                },
                "grid_height": {
                    "type": "integer",
                    "description": "Sketch height in characters (default: 38).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_screenshot",
        "description": (
            "Capture a screenshot of a window (or the full primary monitor) "
            "and return it as a base64-encoded PNG. "
            "Note: this is a raw pixel image; use get_screen_description with "
            "mode='vlm' if you need an interpreted description."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_index": {
                    "type": "integer",
                    "description": "Window index from list_windows. Omit for full-screen capture.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "click_at",
        "description": (
            "Click at a specific pixel position on the screen. "
            "Obtain coordinates from element bounds in get_window_structure. "
            "Use button='left' (default), 'right', or 'middle'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Screen X coordinate."},
                "y": {"type": "integer", "description": "Screen Y coordinate."},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button (default: left).",
                },
                "double": {
                    "type": "boolean",
                    "description": "Double-click if true (default: false).",
                },
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into the currently focused UI element.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": (
            "Press a key or key combination. "
            "Examples: 'enter', 'tab', 'escape', 'ctrl+c', 'alt+f4', 'ctrl+shift+t'. "
            "Keys are separated by '+'; modifiers first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "Key or key combination string.",
                },
            },
            "required": ["keys"],
        },
    },
    {
        "name": "scroll",
        "description": (
            "Scroll the mouse wheel at an optional screen position. "
            "Positive clicks scroll up/forward; negative clicks scroll down/backward."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "clicks": {
                    "type": "integer",
                    "description": "Scroll amount (positive = up, negative = down). Default: 3.",
                },
                "x": {
                    "type": "integer",
                    "description": "Screen X coordinate to scroll at (optional).",
                },
                "y": {
                    "type": "integer",
                    "description": "Screen Y coordinate to scroll at (optional).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_full_screenshot",
        "description": (
            "Capture a screenshot of the entire virtual desktop (all monitors combined) "
            "and optionally render the accessibility element tree of a window as an ASCII "
            "spatial sketch with OCR overlay. "
            "Returns: window title, screenshot_scope='full_display', PNG as base64, "
            "image pixel dimensions, and the sketch string."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_index": {
                    "type": "integer",
                    "description": "Window index from list_windows. Omit for focused window.",
                },
                "grid_width": {
                    "type": "integer",
                    "description": "Sketch width in characters (default: 110).",
                },
                "grid_height": {
                    "type": "integer",
                    "description": "Sketch height in characters (default: 38).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_visible_areas",
        "description": (
            "Return the bounding rectangles of the portions of a window that are "
            "currently visible — i.e. not covered by other windows and within the "
            "monitor bounds. Each region is {x, y, width, height} in absolute screen pixels. "
            "Useful for verifying that a target coordinate is clickable without "
            "hitting an overlapping window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_index": {
                    "type": "integer",
                    "description": "Window index from list_windows.",
                },
            },
            "required": ["window_index"],
        },
    },
    {
        "name": "bring_to_foreground",
        "description": (
            "Bring a window to the foreground by clicking in its title-bar area. "
            "The tool computes the visible (non-occluded) region of the window, "
            "then clicks near the top-centre of that region (typically the title bar) "
            "to raise it above other windows. "
            "Returns the click coordinates and success status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_index": {
                    "type": "integer",
                    "description": "Window index from list_windows.",
                },
                "window_uid": {
                    "type": "string",
                    "description": "Stable window identifier from list_windows.",
                },
            },
            "required": [],
        },
    },

    # ── P1: identity, capabilities, element actions ──────────────────────────

    {
        "name": "get_capabilities",
        "description": (
            "Report the platform, adapter, and which features are supported in "
            "this process.  Call once at session start to choose tools that "
            "match the environment (e.g. accessibility_tree=false on a "
            "platform without a real AX adapter)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_monitors",
        "description": (
            "Enumerate monitors with bounds, scale factor, and logical/physical "
            "rectangles.  Useful for click coordinate-space conversion on "
            "high-DPI multi-monitor setups."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_element",
        "description": (
            "Resolve an element selector to a concrete element_id and bounds. "
            "Selector grammar accepts XPath-ish (Window/Pane/Button[name=\"OK\"]) "
            "or CSS-ish (Window > Pane Button[name=\"OK\"]).  Returns "
            "ambiguous_matches > 1 to flag brittle selectors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector":     {"type": "string"},
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "click_element",
        "description": (
            "Click an element identified by selector or element_id.  Returns "
            "an ActionReceipt with before/after tree hashes, changed flag, and "
            "any new dialogs that appeared."
        ),
        "inputSchema": {
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
    {
        "name": "focus_element",
        "description": "Give keyboard focus to an element.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector":     {"type": "string"},
                "element_id":   {"type": "string"},
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
                "dry_run":      {"type": "boolean"},
            },
            "required": [],
        },
    },
    {
        "name": "set_value",
        "description": (
            "Set the textual value of an editable element (focuses, selects all "
            "if clear_first=true, then types value)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector":     {"type": "string"},
                "element_id":   {"type": "string"},
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
                "value":        {"type": "string"},
                "clear_first":  {"type": "boolean"},
                "dry_run":      {"type": "boolean"},
            },
            "required": ["value"],
        },
    },
    {
        "name": "invoke_element",
        "description": (
            "Invoke an element's primary action.  On Windows this prefers the "
            "UIA InvokePattern; otherwise behaves like click_element."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector":     {"type": "string"},
                "element_id":   {"type": "string"},
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
                "dry_run":      {"type": "boolean"},
            },
            "required": [],
        },
    },
    {
        "name": "select_option",
        "description": (
            "Open a combo-box or list-style element and click the option named "
            "option_name (or at zero-based option_index)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector":     {"type": "string"},
                "element_id":   {"type": "string"},
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
                "option_name":  {"type": "string"},
                "option_index": {"type": "integer"},
                "dry_run":      {"type": "boolean"},
            },
            "required": [],
        },
    },

    # ── P2: sync, observe-with-diff, snapshots, composites ──────────────────

    {
        "name": "observe_window",
        "description": (
            "Return the current accessibility tree of a window.  Pass a "
            "tree_token from a previous observation as 'since' to get only "
            "what changed (custom diff format by default; pass "
            "format='json-patch' for RFC 6902).  An expired token returns the "
            "full tree with base_token=null."
        ),
        "inputSchema": {
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
    {
        "name": "snapshot",
        "description": (
            "Capture the current state of all windows + their accessibility "
            "trees and return a snapshot_id.  TTL 5 minutes; LRU 32."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "snapshot_get",
        "description": "Retrieve a previously captured snapshot by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"snapshot_id": {"type": "string"}},
            "required": ["snapshot_id"],
        },
    },
    {
        "name": "snapshot_diff",
        "description": (
            "Compare two snapshots.  Returns added/removed windows plus a "
            "per-window tree diff (custom format by default)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "a":      {"type": "string"},
                "b":      {"type": "string"},
                "format": {"type": "string", "enum": ["custom", "json-patch"]},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "snapshot_drop",
        "description": "Free a snapshot before its TTL expires.",
        "inputSchema": {
            "type": "object",
            "properties": {"snapshot_id": {"type": "string"}},
            "required": ["snapshot_id"],
        },
    },
    {
        "name": "wait_for",
        "description": (
            "Block (with polling) until any of the given conditions matches "
            "or timeout_ms elapses.  Conditions: element_appears, "
            "element_disappears, text_visible, window_appears, "
            "window_disappears, tree_changes (with since=tree_token), "
            "focused_changes."
        ),
        "inputSchema": {
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
    {
        "name": "wait_idle",
        "description": (
            "Block until the tree hash has been stable for quiet_ms (default "
            "750) or timeout_ms is reached.  Useful as a 'page settled' check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
                "quiet_ms":     {"type": "integer"},
                "timeout_ms":   {"type": "integer"},
                "poll_ms":      {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "click_element_and_observe",
        "description": (
            "Click an element, sleep wait_after_ms, then observe the window "
            "with since=<previous tree_token if supplied>.  One round-trip "
            "instead of two."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector":      {"type": "string"},
                "element_id":    {"type": "string"},
                "window_uid":    {"type": "string"},
                "window_index":  {"type": "integer"},
                "button":        {"type": "string"},
                "count":         {"type": "integer"},
                "wait_after_ms": {"type": "integer"},
                "since":         {"type": "string"},
                "dry_run":       {"type": "boolean"},
            },
            "required": [],
        },
    },
    {
        "name": "type_and_observe",
        "description": "type_text + observe_window in one call.",
        "inputSchema": {
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
    {
        "name": "press_key_and_observe",
        "description": "press_key + observe_window in one call.",
        "inputSchema": {
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

    # ── P3: filtering, cropping, region OCR, budgeted description ────────────
    {
        "name": "get_screenshot_cropped",
        "description": (
            "Capture a screenshot, optionally cropped to an element_id or "
            "explicit bbox (window-relative pixels) and downscaled to "
            "max_width.  Returns base64 PNG plus source_bbox in the response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
                "element_id":   {"type": "string"},
                "bbox":         {"type": "object"},
                "padding_px":   {"type": "integer"},
                "max_width":    {"type": "integer"},
            },
            "required": [],
        },
    },
    # ── P4: tracing, replay, scenarios, oracles ──────────────────────────────
    {
        "name": "trace_start",
        "description": (
            "Begin recording every tool call to traces/<trace_id>/trace.jsonl "
            "with periodic full + per-window screenshots."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "trace_stop",
        "description": "Close the active trace; returns trace path and step_count.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "trace_status",
        "description": "Report whether a trace is active and the current step count.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "replay_start",
        "description": (
            "Load a trace.jsonl (or directory containing one) and prepare to "
            "replay it.  mode='execute' re-issues each call; mode='verify' "
            "compares results using per-tool comparison rules and records "
            "divergences."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":          {"type": "string"},
                "mode":          {"type": "string", "enum": ["execute", "verify"]},
                "on_divergence": {"type": "string", "enum": ["stop", "warn", "resume"]},
            },
            "required": ["path"],
        },
    },
    {
        "name": "replay_step",
        "description": "Advance one row of the active replay.",
        "inputSchema": {
            "type": "object",
            "properties": {"replay_id": {"type": "string"}},
            "required": ["replay_id"],
        },
    },
    {
        "name": "replay_status",
        "description": "Report replay position, total, finished flag, divergences.",
        "inputSchema": {
            "type": "object",
            "properties": {"replay_id": {"type": "string"}},
            "required": ["replay_id"],
        },
    },
    {
        "name": "replay_stop",
        "description": "Discard a replay handle and free its resources.",
        "inputSchema": {
            "type": "object",
            "properties": {"replay_id": {"type": "string"}},
            "required": ["replay_id"],
        },
    },
    {
        "name": "load_scenario",
        "description": (
            "Load a YAML scenario file and attach it to the mock adapter.  "
            "Subsequent observations and actions are routed through the "
            "scenario state machine.  Requires --mock."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_budget_status",
        "description": (
            "Report remaining budget for actions, screenshots, VLM tokens, "
            "session_seconds, and actions_per_minute (sliding 60s window).  "
            "Returns configured=false when no budgets are set."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_redaction_status",
        "description": (
            "Report redaction enabled state, total patterns count, and how "
            "many redactions have been applied so far this session."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "propose_action",
        "description": (
            "For destructive actions matched by config.confirmation_required, "
            "issue a single-use confirm_token bound to the resolved element's "
            "(window_uid, selector, bbox).  The agent must call the actual "
            "action with the returned confirm_token within the TTL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "args":   {"type": "object"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "assert_state",
        "description": (
            "Evaluate a list of declarative predicates (AND).  Predicates: "
            "element_exists, element_absent, value_equals, value_matches, "
            "text_visible (mode=tree|ocr|auto), window_focused, "
            "window_exists, tree_hash_equals, screenshot_similar."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "predicate":  {"type": "array"},
                "predicates": {"type": "array"},
            },
            "required": [],
        },
    },
    {
        "name": "get_ocr",
        "description": (
            "Run OCR on a window or a region (element_id / bbox).  Returns "
            "[{text, confidence, bbox}].  Cheaper and more focused than "
            "get_screen_description for read-only verification."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_uid":   {"type": "string"},
                "window_index": {"type": "integer"},
                "element_id":   {"type": "string"},
                "bbox":         {"type": "object"},
            },
            "required": [],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# MCPServer
# ─────────────────────────────────────────────────────────────────────────────

class MCPServer:
    """
    MCP stdio server.

    Reads newline-delimited JSON-RPC 2.0 messages from stdin, dispatches
    to tool handlers, and writes responses to stdout.  All log output
    is directed to stderr to preserve the integrity of the MCP framing.
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(
        self,
        observer:  ScreenObserver,
        renderer:  ASCIIRenderer,
        describer: DescriptionGenerator,
        config:    Dict,
    ):
        self.observer  = observer
        self.renderer  = renderer
        self.describer = describer
        self.config    = config

    # ── Transport ─────────────────────────────────────────────────────────────

    def _emit(self, payload: Dict) -> None:
        """Write a JSON-RPC message to stdout."""
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._emit({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def _result(self, request_id: Any, result: Any) -> None:
        self._emit({"jsonrpc": "2.0", "id": request_id, "result": result})

    # ── Message dispatch ──────────────────────────────────────────────────────

    def _handle(self, msg: Dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        rid    = msg.get("id")     # None for notifications

        try:
            if method == "initialize":
                self._result(rid, {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "serverInfo": {
                        "name":    self.config["mcp"]["server_name"],
                        "version": self.config["mcp"]["version"],
                    },
                    "capabilities": {"tools": {}},
                })

            elif method in ("notifications/initialized", "ping"):
                if rid is not None:
                    self._result(rid, {})

            elif method == "tools/list":
                self._result(rid, {"tools": _TOOLS})

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments") or {}
                result    = self._dispatch(tool_name, arguments)
                self._result(rid, {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                })

            else:
                if rid is not None:
                    self._error(rid, -32601, f"Method not found: {method}")

        except Exception as e:
            print(f"[MCPServer:_handle] {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            if rid is not None:
                self._error(rid, -32603, str(e))

    # ── Tool dispatcher ───────────────────────────────────────────────────────

    def _dispatch(self, name: str, args: Dict) -> Any:
        """Route a tools/call to the appropriate handler."""
        # New centralised tools (P1+) live in tools.py.
        if name in _tools.REGISTRY:
            ctx = _tools.ToolContext(
                observer=self.observer, renderer=self.renderer,
                describer=self.describer, config=self.config,
            )
            return _tools.dispatch(ctx, name, args)

        try:
            windows = self.observer.list_windows()
            idx     = args.get("window_index")
            info    = self.observer.window_by_index(windows, idx)
            hwnd    = info.handle if info else None

            if name == "list_windows":
                return self._t_list_windows(windows)

            elif name == "get_window_structure":
                return self._t_structure(hwnd, info, args)

            elif name == "get_screen_description":
                return self._t_description(hwnd, info, args)

            elif name == "get_screen_sketch":
                return self._t_sketch(hwnd, info, args)

            elif name == "get_screenshot":
                return self._t_screenshot(hwnd, info)

            elif name == "click_at":
                return self._t_click_at(args)

            elif name == "type_text":
                return self.observer.perform_action("type", value=args.get("text", ""))

            elif name == "press_key":
                return self.observer.perform_action("key", value=args.get("keys", ""))

            elif name == "scroll":
                return self.observer.perform_action("scroll", value=args)

            elif name == "get_full_screenshot":
                return self._t_full_screenshot(hwnd, info, args)

            elif name == "get_visible_areas":
                return self._t_visible_areas(hwnd, info, windows)

            elif name == "bring_to_foreground":
                return self._t_bring_to_foreground(hwnd, info, windows)

            else:
                return {"error": f"Unknown tool: {name}"}

        except Exception as e:
            print(f"[MCPServer:_dispatch:{name}] {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return {"error": str(e)}

    # ── Individual tool handlers ──────────────────────────────────────────────

    def _t_list_windows(self, windows) -> Dict:
        return {
            "count": len(windows),
            "windows": [
                {"index": i, **w.to_dict()} for i, w in enumerate(windows)
            ],
        }

    def _t_structure(self, hwnd, info, args) -> Dict:
        tree = self.observer.get_element_tree(hwnd)
        if tree is None:
            return {"error": "Could not retrieve element tree for this window"}
        return {
            "window": info.title if info else "(focused)",
            "element_count": len(tree.flat_list()),
            "tree": tree.to_dict(),
        }

    def _t_description(self, hwnd, info, args) -> Dict:
        mode = args.get("mode", "accessibility")
        tree = self.observer.get_element_tree(hwnd)
        if tree is None:
            return {"error": "Could not retrieve element tree"}

        shot = self.observer.get_screenshot(hwnd)

        if mode == "accessibility":
            return {"mode": mode, "description": self.describer.from_tree(tree, info)}
        elif mode == "ocr":
            if shot is None:
                return {"error": "Screenshot unavailable for OCR"}
            return {"mode": mode, "description": self.describer.from_ocr(shot)}
        elif mode == "vlm":
            if shot is None:
                return {"error": "Screenshot unavailable for VLM"}
            return {"mode": mode, "description": self.describer.from_vlm(shot)}
        elif mode == "combined":
            return {"mode": mode, **self.describer.combined(tree, shot, info)}
        else:
            return {"error": f"Unknown mode: {mode}"}

    def _t_sketch(self, hwnd, info, args) -> Dict:
        tree = self.observer.get_element_tree(hwnd)
        if tree is None:
            return {"error": "Could not retrieve element tree"}

        ref = info.bounds if info else tree.bounds
        sketch = self.renderer.render(
            root          = tree,
            screen_bounds = ref,
            grid_width    = args.get("grid_width"),
            grid_height   = args.get("grid_height"),
        )
        return {
            "window": info.title if info else "(focused)",
            "grid_width":  args.get("grid_width",  self.renderer.default_width),
            "grid_height": args.get("grid_height", self.renderer.default_height),
            "sketch": sketch,
        }

    def _t_screenshot(self, hwnd, info) -> Dict:
        import base64
        shot = self.observer.get_screenshot(hwnd)
        if shot is None:
            return {"error": "Screenshot capture failed"}
        return {
            "window": info.title if info else "(full screen)",
            "format": "png",
            "encoding": "base64",
            "data": base64.b64encode(shot).decode(),
        }

    def _t_full_screenshot(self, hwnd, info, args) -> Dict:
        import base64
        # Always capture all monitors combined
        shot = self.observer.get_full_display_screenshot()
        if shot is None:
            return {"error": "Screenshot capture failed"}

        sketch = None
        tree = self.observer.get_element_tree(hwnd) if hwnd is not None else None
        if tree is not None:
            ref = info.bounds if info else self.observer.get_screen_bounds()
            # Crop the full-display PNG to window bounds for accurate OCR overlay.
            ocr_bytes = shot
            if info is not None:
                try:
                    import io as _io2
                    from PIL import Image as _Image2
                    full_img = _Image2.open(_io2.BytesIO(shot))
                    screen_b = self.observer.get_screen_bounds()
                    crop_box = (
                        max(0, info.bounds.x - screen_b.x),
                        max(0, info.bounds.y - screen_b.y),
                        min(full_img.width,  info.bounds.right  - screen_b.x),
                        min(full_img.height, info.bounds.bottom - screen_b.y),
                    )
                    buf2 = _io2.BytesIO()
                    full_img.crop(crop_box).save(buf2, format="PNG")
                    ocr_bytes = buf2.getvalue()
                except Exception:
                    pass
            sketch = self.renderer.render(
                root             = tree,
                screen_bounds    = ref,
                grid_width       = args.get("grid_width"),
                grid_height      = args.get("grid_height"),
                screenshot_bytes = ocr_bytes,
            )

        img_w = img_h = None
        try:
            import io as _io
            from PIL import Image as _Image
            _img = _Image.open(_io.BytesIO(shot))
            img_w, img_h = _img.size
        except Exception:
            pass

        return {
            "window":           info.title if info else "(full screen)",
            "screenshot_scope": "full_display",
            "format":           "png",
            "encoding":         "base64",
            "width":            img_w,
            "height":           img_h,
            "data":             base64.b64encode(shot).decode(),
            "sketch":           sketch,
        }

    def _t_visible_areas(self, hwnd, info, windows) -> Dict:
        if hwnd is None:
            return {"error": "window_index is required for get_visible_areas"}
        areas = self.observer.get_visible_areas(hwnd, windows)
        return {
            "window":          info.title if info else "(unknown)",
            "visible_regions": areas,
        }

    def _t_bring_to_foreground(self, hwnd, info, windows) -> Dict:
        if hwnd is None:
            return {"success": False,
                    "error": "window_index is required for bring_to_foreground"}
        result = self.observer.bring_to_foreground(hwnd, windows)
        result["window"] = info.title if info else "(unknown)"
        return result

    def _t_click_at(self, args) -> Dict:
        return self.observer.perform_action(
            "click_at",
            value={
                "x":      args.get("x", 0),
                "y":      args.get("y", 0),
                "button": args.get("button", "left"),
                "double": args.get("double", False),
            },
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Block on stdin, reading and processing JSON-RPC messages."""
        logger.info("[MCPServer:run] Listening on stdin (MCP mode)")
        print("[MCPServer] Ready — listening on stdin", file=sys.stderr)

        for raw_line in sys.stdin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                msg = json.loads(raw_line)
                self._handle(msg)
            except json.JSONDecodeError as e:
                print(f"[MCPServer:run] JSON parse error: {e}", file=sys.stderr)
            except Exception as e:
                print(f"[MCPServer:run] Unhandled error: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

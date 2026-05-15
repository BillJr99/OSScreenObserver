# OS Screen Observer — API Reference for Claude Code

This document describes how to advertise the OS Screen Observer tools to an
LLM and how to call the REST API that backs them. It covers tool definition
schemas, every endpoint's URL, method, query parameters, request body, and
the exact shape of every response including errors.

The server runs on `http://127.0.0.1:5001` by default. Most `/api/*` endpoints
return `Content-Type: application/json`; exceptions are `/api/metrics`
(returns `text/plain` Prometheus format) and `/` (returns HTML). There is no
authentication.

---

## Startup

Start the server before making any requests:

```bash
python main.py --mode inspect          # REST only
python main.py --mode both             # REST + MCP stdio simultaneously
python main.py --mock --mode inspect   # synthetic data, no OS access needed
```

The server is ready when `GET /api/healthz` returns a 200 response. Poll
with a short sleep until it succeeds:

```python
import time, httpx
for _ in range(20):
    try:
        httpx.get("http://127.0.0.1:5001/api/healthz", timeout=2).raise_for_status()
        break
    except Exception:
        time.sleep(0.5)
```

---

## Tool Advertisement Schemas (OpenAI / OpenWebUI v1 Format)

Declare these in the `tools` array of any `POST /v1/chat/completions` request.
The `parameters` block is JSON Schema; the `function` envelope is required by
the OpenAI wire format.

```python
SCREEN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": (
                "Enumerate all visible top-level windows on the desktop. "
                "Returns index, title, process name, PID, and pixel geometry. "
                "Call this first to find the window_index needed by all other tools. "
                "The index is positional and may change between calls if windows "
                "open or close; do not cache it across steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe_window",
            "description": (
                "Return the current visual state of a window as two complementary "
                "representations: an ASCII spatial sketch (element positions as "
                "labeled boxes, geometry preserved) and a prose accessibility "
                "description (roles, names, values, states). "
                "Call this immediately after launching any application, before "
                "deciding where to click or type, and after every action to verify "
                "the state changed as expected. "
                "Without calling this you have no reliable basis for choosing "
                "coordinates or confirming that an action succeeded."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {
                        "type": "integer",
                        "description": "Index from list_windows (0-based). Omit for the focused window.",
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
                "Return the complete accessibility element tree as structured JSON. "
                "Each element includes: id, name, role, value, enabled, focused, "
                "keyboard_shortcut, and bounds {x, y, width, height} in absolute "
                "screen pixels. "
                "To click the center of an element: x + width//2, y + height//2. "
                "Use this when observe_window does not give enough coordinate "
                "precision to target a specific control."
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
                "Return a combined textual description of the window using all "
                "available analysis sources: accessibility tree prose, OCR text "
                "(if pytesseract is installed), and VLM interpretation (if "
                "vlm.enabled=true with a configured vlm.base_url and vlm.model). "
                "The mode parameter is accepted for compatibility but is always "
                "treated as 'combined' — all enabled sources are included."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {
                        "type": "integer",
                        "description": "Index from list_windows. Omit for focused window.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["accessibility", "ocr", "vlm", "combined"],
                        "description": "Accepted for compatibility; always returns combined output.",
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
                "Render the accessibility element tree as an ASCII spatial layout "
                "diagram. Each element appears as a labeled box; positions and "
                "relative sizes reflect actual screen geometry scaled to the "
                "character grid. Useful for understanding spatial relationships "
                "between controls when the prose description is hard to parse."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {
                        "type": "integer",
                        "description": "Index from list_windows. Omit for focused window.",
                    },
                    "grid_width": {
                        "type": "integer",
                        "description": "Output width in characters (default: 72).",
                    },
                    "grid_height": {
                        "type": "integer",
                        "description": "Output height in characters (default: 24).",
                    },
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
                "Capture a screenshot of a window (or the full primary monitor) "
                "and return it as a base64-encoded PNG. Use when a vision-capable "
                "model needs to inspect pixel content directly. "
                "For an interpreted description, prefer get_screen_description "
                "with mode='vlm' instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_index": {
                        "type": "integer",
                        "description": "Index from list_windows. Omit for full-screen capture.",
                    }
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
                "Derive coordinates from element bounds in get_element_tree: "
                "click_x = bounds.x + bounds.width // 2, "
                "click_y = bounds.y + bounds.height // 2. "
                "Do not estimate or recall coordinates from memory. "
                "Always call observe_window after clicking to confirm the result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Absolute screen X in pixels."},
                    "y": {"type": "integer", "description": "Absolute screen Y in pixels."},
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
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "Type a string into the currently focused UI element character by "
                "character. Click the target input field first to ensure it has "
                "keyboard focus. Do not use this for key combinations such as "
                "ctrl+s; use press_key for those."
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
                "Press a key or key combination. Modifiers come first, joined with '+'. "
                "Examples: 'enter', 'tab', 'escape', 'ctrl+s', 'ctrl+a', "
                "'alt+f4', 'ctrl+shift+n', 'ctrl+alt+delete'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "Key or combination string.",
                    }
                },
                "required": ["keys"],
            },
        },
    },
]
```

---

## Dispatching Tool Calls

OpenWebUI returns tool calls with `finish_reason == "tool_calls"`. The
`function.arguments` field is a **JSON string**, not a dict. Parse it before
use. Some local models malform JSON; guard with a try/except.

```python
import json

for tool_call in response.choices[0].message.tool_calls:
    fn_name = tool_call.function.name
    try:
        fn_args = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError:
        fn_args = {}

    result = call_screen_api(fn_name, fn_args)   # see routing table below

    tool_result_messages.append({
        "role":         "tool",
        "tool_call_id": tool_call.id,             # must match exactly
        "content":      json.dumps(result),        # must be a string
    })
```

### Routing Table

Map tool names to REST calls:

| Tool name              | REST call                                              |
|------------------------|--------------------------------------------------------|
| `list_windows`         | `GET /api/windows`                                     |
| `observe_window`       | `GET /api/sketch` + `GET /api/description`             |
| `get_element_tree`     | `GET /api/structure`                                   |
| `get_screen_description` | `GET /api/description` (mode ignored; always combined)|
| `get_screen_sketch`    | `GET /api/sketch`                                      |
| `get_screenshot`       | `GET /api/screenshot`                                  |
| `click_at`             | `POST /api/action` `{"action":"click_at",...}`         |
| `type_text`            | `POST /api/action` `{"action":"type",...}`             |
| `press_key`            | `POST /api/action` `{"action":"key",...}`              |

---

## REST API Reference

### Common Conventions

- Base URL: `http://127.0.0.1:5001` (configurable via `config.json`)
- Content-Type: Most `/api/*` endpoints return `application/json`. Exceptions:
  - `GET /api/metrics` returns `text/plain` (Prometheus format)
  - `GET /` returns `text/html` (the web inspector UI)
- `window_index`: integer, 0-based, from `list_windows`. Optional on every
  endpoint; omit to use the currently focused window.
- Error shape: `{"error": "<message string>"}` with HTTP 400 or 500.

---

### GET /api/windows

Enumerate all visible top-level windows.

**Query parameters:** none.

**Response:**

```jsonc
{
  "is_mock": false,            // true when running with --mock flag
  "count":   3,
  "windows": [
    {
      "index":   0,            // use this as window_index in all other calls
      "handle":  "131456",     // platform handle (string); opaque, do not parse
      "title":   "Untitled — Notepad",
      "process": "notepad.exe",
      "pid":     4821,
      "bounds": {
        "x":      80,          // screen pixels, top-left corner of window
        "y":      60,
        "width":  800,
        "height": 600
      },
      "focused": true          // true for the foreground window
    },
    {
      "index":   1,
      "handle":  "263144",
      "title":   "GitHub — Google Chrome",
      "process": "chrome.exe",
      "pid":     7204,
      "bounds": { "x": 0, "y": 0, "width": 1920, "height": 1050 },
      "focused": false
    }
  ]
}
```

**Notes:**
- Windows are sorted: focused window first, then alphabetical by title.
- `index` is positional in this response array. If windows open or close
  between calls, indices shift. Do not cache across steps.

---

### GET /api/structure

Return the full accessibility element tree for a window.

**Query parameters:**

| Parameter      | Type    | Required | Description                          |
|----------------|---------|----------|--------------------------------------|
| `window_index` | integer | No       | From `list_windows`. Focused window if omitted. |

**Response:**

```jsonc
{
  "window":        "Untitled — Notepad",
  "element_count": 14,
  "tree": {
    "id":               "root",
    "name":             "Untitled — Notepad",
    "role":             "Window",
    "value":            null,
    "bounds": {
      "x": 80, "y": 60, "width": 800, "height": 600
    },
    "enabled":          true,
    "focused":          false,
    "keyboard_shortcut": null,
    "description":      null,
    "children": [
      {
        "id":    "root.0",
        "name":  "MenuBar",
        "role":  "MenuBar",
        "value": null,
        "bounds": { "x": 80, "y": 60, "width": 800, "height": 22 },
        "enabled": true,
        "focused": false,
        "keyboard_shortcut": null,
        "description": null,
        "children": [
          {
            "id":    "root.0.0",
            "name":  "File",
            "role":  "MenuItem",
            "value": null,
            "bounds": { "x": 80, "y": 60, "width": 56, "height": 22 },
            "enabled": true,
            "focused": false,
            "keyboard_shortcut": null,
            "description": null,
            "children": []
          }
          // ... more menu items
        ]
      },
      {
        "id":    "root.1",
        "name":  "Text Editor",
        "role":  "Document",
        "value": "Hello, world!\nThis is a test document.\n",
        "bounds": { "x": 80, "y": 82, "width": 800, "height": 514 },
        "enabled": true,
        "focused": true,          // this element has keyboard focus
        "keyboard_shortcut": null,
        "description": null,
        "children": []
      }
      // ... more children
    ]
  }
}
```

**Computing click coordinates from bounds:**
```python
center_x = element["bounds"]["x"] + element["bounds"]["width"]  // 2
center_y = element["bounds"]["y"] + element["bounds"]["height"] // 2
```

**Error:**
```json
{"error": "Could not retrieve element tree for this window"}
```

---

### GET /api/description

Return a combined textual description of the window using all available
analysis sources.

**Note:** The `mode` query parameter is accepted for compatibility but is
always ignored — the endpoint always returns combined output (every enabled
source: accessibility tree, OCR if configured, VLM if configured).

**Query parameters:**

| Parameter      | Type    | Required | Default          | Description                  |
|----------------|---------|----------|------------------|------------------------------|
| `window_index` | integer | No       | focused window   |                              |
| `mode`         | string  | No       | _(ignored)_      | Accepted but always ignored; always returns combined output. |

**Response:**

```jsonc
{
  "mode": "combined",
  "effective_mode": "combined",
  "accessibility": "Application : notepad.exe\nWindow      : Untitled — Notepad\n...",
  "ocr": "...",               // present when ocr.enabled=true in config
  "vlm": "..."                // present when vlm.enabled=true in config
}
```

**Notes:**
- `vlm` requires `vlm.enabled=true`, a reachable `vlm.base_url` (an
  OpenWebUI-compatible OpenAI chat-completions endpoint), and a
  `vlm.model` selected from that endpoint. The api key, if needed, can
  live in `vlm.api_key` or the `OWUI_API_KEY` environment variable.
  Returns a disabled / unconfigured message string if any of those are
  missing.
- `ocr` requires `pytesseract` installed and the Tesseract binary either on
  the system PATH or set as `ocr.tesseract_cmd` in `config.json`. On Windows
  the installer does **not** add it to PATH — set the full path explicitly,
  escaping backslashes (`"c:\\Program Files\\Tesseract-OCR\\tesseract.exe"`)
  or using forward slashes. Returns a disabled message string if not
  configured; `GET /api/healthz` reports the resolved path and any
  configuration error.

---

### GET /api/sketch

Render the accessibility element tree as an ASCII spatial layout diagram.

**Query parameters:**

| Parameter      | Type    | Required | Default | Description                          |
|----------------|---------|----------|---------|--------------------------------------|
| `window_index` | integer | No       | focused |                                      |
| `grid_width`   | integer | No       | 72      | Output width in characters           |
| `grid_height`  | integer | No       | 24      | Output height in characters          |

**Response:**

```jsonc
{
  "window":      "Untitled — Notepad",
  "grid_width":  72,
  "grid_height": 24,
  "sketch": "+------------------------------------------------------------------------+\n| Window \"Untitled — Notepad\"                                            |\n..."
}
```

**Notes:**
- Box-drawing characters are plain ASCII `+`, `-`, `|` by default (set
  `ascii_sketch.unicode_box = true` in config for `┌─┐│└┘`).
- Elements too small to hold a label are assigned a short key (A, B, …)
  and collected in a legend section appended below the grid.
- Coordinates in the sketch are proportional, not exact pixels; use
  `get_element_tree` for precise coordinates.

---

### GET /api/screenshot

Capture a screenshot and return it as a base64-encoded PNG.

**Query parameters:**

| Parameter      | Type    | Required | Default       | Description                    |
|----------------|---------|----------|---------------|--------------------------------|
| `window_index` | integer | No       | full monitor  | Omit for full primary monitor  |

**Response:**

```jsonc
{
  "window":   "Untitled — Notepad",
  "format":   "png",
  "encoding": "base64",
  "data":     "iVBORw0KGgoAAAANSUhEUgAA..."   // base64-encoded PNG bytes
}
```

**Decoding:**

```python
import base64, io
from PIL import Image

png_bytes = base64.b64decode(response["data"])
img = Image.open(io.BytesIO(png_bytes))
```

---

### POST /api/action

Execute an input action. The `action` field in the request body selects the
action type; the remaining fields vary by type.

**Request body — click_at:**

```jsonc
{
  "action": "click_at",
  "x":      480,         // required; absolute screen X in pixels
  "y":      300,         // required; absolute screen Y in pixels
  "button": "left",      // optional; "left" | "right" | "middle" (default: "left")
  "double": false        // optional; double-click if true (default: false)
}
```

**Request body — type:**

```jsonc
{
  "action": "type",
  "value":  "hello world"   // required; string to type into focused element
}
```

**Request body — key:**

```jsonc
{
  "action": "key",
  "value":  "ctrl+s"    // required; key or combination; modifiers first, joined with "+"
}
```

**Key string examples:**

| Intent                        | Value              |
|-------------------------------|-------------------|
| Enter / Return                | `"enter"`          |
| Tab                           | `"tab"`            |
| Escape                        | `"escape"`         |
| Save (Ctrl+S)                 | `"ctrl+s"`         |
| Select all (Ctrl+A)           | `"ctrl+a"`         |
| Copy (Ctrl+C)                 | `"ctrl+c"`         |
| Paste (Ctrl+V)                | `"ctrl+v"`         |
| Undo (Ctrl+Z)                 | `"ctrl+z"`         |
| Close window (Alt+F4)         | `"alt+f4"`         |
| New window (Ctrl+Shift+N)     | `"ctrl+shift+n"`   |
| Function key                  | `"f5"`             |
| Delete                        | `"delete"`         |

**Request body — scroll:**

```jsonc
{
  "action":  "scroll",
  "x":       480,        // optional; scroll position X
  "y":       300,        // optional; scroll position Y
  "clicks":  3           // optional; scroll units (positive = up, negative = down)
}
```

**Response (all action types):**

Success:
```jsonc
{
  "success": true,
  "action":  "click_at",   // echoes the action type
  "x":       480,          // echoes input parameters where applicable
  "y":       300
}
```

Failure:
```jsonc
{
  "success": false,
  "error":   "description of what went wrong"
}
```

Mock mode always returns `success: true` with a note field:
```jsonc
{
  "success": true,
  "action":  "click_at",
  "note":    "Mock adapter — no real OS action performed"
}
```

---

## Error Responses

All endpoints return errors as JSON. HTTP status codes:

| Code | Meaning                                           |
|------|---------------------------------------------------|
| 400  | Bad request (unknown mode, unsupported action)    |
| 500  | Server error (platform API failure, no screenshot)|

Error body:
```json
{"error": "human-readable description"}
```

Action endpoint errors use the action response shape instead:
```json
{"success": false, "error": "human-readable description"}
```

---

## Coordinate System

All coordinates are **absolute screen pixels** measured from the top-left
corner of the primary monitor. They are not window-relative.

Element bounds from `/api/structure`:
```
bounds.x      = left edge of element in screen pixels
bounds.y      = top edge of element in screen pixels
bounds.width  = element width in pixels
bounds.height = element height in pixels

center_x = bounds.x + bounds.width  // 2
center_y = bounds.y + bounds.height // 2
```

Pass `center_x` and `center_y` directly to the `x` and `y` fields of a
`click_at` action.

---

## The observe() Convenience Pattern

Rather than calling `/api/sketch` and `/api/description` separately, combine
them into a single observation bundle:

```python
import httpx

BASE = "http://127.0.0.1:5001"

def observe(window_index=None):
    params = {} if window_index is None else {"window_index": window_index}
    sketch = httpx.get(f"{BASE}/api/sketch",      params=params).json()
    desc   = httpx.get(f"{BASE}/api/description", params=params).json()
    return {
        "window":      sketch.get("window", "unknown"),
        "sketch":      sketch.get("sketch", ""),
        "description": desc.get("description", ""),
    }
```

Serialize the result into LLM context as:

```
CURRENT STATE — Untitled — Notepad

SKETCH
+------------------------------------------------------------------------+
| Window "Untitled — Notepad"                  ...
...
+------------------------------------------------------------------------+

DESCRIPTION
Application : notepad.exe
Window      : Untitled — Notepad
...
[14 elements total; focused → Document "Text Editor"]
```

---

## Required Call Sequence

The following sequence is mandatory for any GUI task. Deviating from it
produces unreliable behavior because coordinates will be stale or wrong.

```
1. list_windows
      → identify target window_index

2. observe_window(window_index)
      → understand current UI state

3. get_element_tree(window_index)          ← only when you need exact coords
      → derive click_x, click_y from bounds

4. click_at / type_text / press_key
      → execute one action

5. observe_window(window_index)            ← mandatory after every action
      → verify the state changed as expected

6. repeat from 3 or 2 until task complete
```

Never assume an action succeeded without an observation step after it. If
the state after the action does not match the expected state, re-observe
and try an alternative approach before repeating the same action.

---

## System Prompt for the LLM

Include this verbatim as the `system` message in every completion request:

```
You are a GUI automation agent operating on a live desktop.
You observe screen state through accessibility tools and execute
mouse and keyboard actions.

COORDINATE RULE
All x, y values are absolute screen pixels from get_element_tree bounds.
To click the center of an element with bounds {x, y, width, height}:
  click_x = x + width  // 2
  click_y = y + height // 2
Do not estimate, guess, or recall coordinates from memory or training data.

OBSERVATION RULE
You are blind to the screen unless you call observe_window.
Call observe_window:
  - immediately after launching any application
  - before deciding where to click or type
  - after every action, without exception, to confirm the result

WORKFLOW
1. list_windows — find the window_index for the target application
2. observe_window — understand the current state
3. get_element_tree — find exact coordinates when needed
4. execute exactly one action (click_at, type_text, or press_key)
5. observe_window — verify the state changed correctly
6. repeat until the task is complete

If you are uncertain what state the UI is in, always observe before acting.
If an action does not produce the expected result, do not repeat it;
re-observe and try an alternative approach.
```

---

## Quick Reference Card

```
GET  /api/windows
     → {count, windows: [{index, handle, title, process, pid, bounds, focused}]}

GET  /api/structure?window_index=N
     → {window, element_count, tree: {id, name, role, value, bounds, enabled, focused, children[]}}

GET  /api/description?window_index=N
     mode param ignored; always returns combined output
     → {mode:"combined", effective_mode:"combined", accessibility, ocr?, vlm?}

GET  /api/sketch?window_index=N&grid_width=W&grid_height=H
     → {window, grid_width, grid_height, sketch}

GET  /api/screenshot?window_index=N
     → {window, format:"png", encoding:"base64", data:"<base64>"}

POST /api/action
     {"action":"click_at", "x":N, "y":N, "button":"left", "double":false}
     {"action":"type",     "value":"text to type"}
     {"action":"key",      "value":"ctrl+s"}
     {"action":"scroll",   "x":N, "y":N, "clicks":N}
     → {"success":true|false, "action":"...", ...}  or  {"success":false, "error":"..."}

bounds:  {x, y, width, height}  — absolute screen pixels, top-left origin
click:   x + width//2,  y + height//2  — center of element
errors:  {"error": "message"}  HTTP 400|500
         {"success": false, "error": "message"}  for /api/action

NOTE: /api/metrics returns text/plain (Prometheus); / returns HTML
```

---

## v2 Surface — Agentic Loop & Harness Substrate

The endpoints documented above are stable and continue to work.  Version 2
adds richer endpoints driven by `agentic_features_design.md`.  Every v2
endpoint returns an object with `ok: true|false` plus a structured
`error: {code, message, recoverable, suggested_next_tool, context}` on
failure.  Legacy `success` / `error: "..."` fields are emitted alongside
the new shape so older clients keep working.

### Identity, capability, monitor probes

```
GET  /api/capabilities
     → {ok, platform, adapter, version, protocol_version,
        supports: {accessibility_tree, uia_invoke, occlusion_detection,
                   drag, ocr, vlm, redaction, scenarios, tracing,
                   replay, image_blur},
        config: {tree_max_depth, ascii_grid}}

GET  /api/monitors
     → {ok, monitors: [{index, primary, bounds, scale_factor,
                        logical_bounds, physical_bounds}]}

GET  /api/windows                       # extended
     → {ok, count, is_mock,
        windows: [{index, handle, title, process, pid, bounds, focused,
                   window_uid, monitor_index?, scale_factor?,
                   logical_bounds?, physical_bounds?}]}

GET  /api/find_element?selector=X&window_uid=Y[&window_index=N]
     → {ok, window_uid, element_id, selector, bounds,
        ambiguous_matches, all_matches:[{element_id, bounds, name, role}]}
```

Selector grammar (auto-detected):

- XPath-ish (default; starts with a role): `Window/Pane/Button[name="OK"]`
- CSS-ish (uses `>` or whitespace combinators): `Window > Pane Button[name~="Save.*"]`

Predicates: `name=`, `name~=` (regex, full match), `value=`, `value~=`,
`role=`, `keyboard_shortcut=`, `enabled=true|false`, `focused=true|false`,
`index=N`.  CSS `:nth-of-type(N)` accepted.

### Element-targeted actions

All return an `ActionReceipt`: `{ok, step_id, caused_by_step_id, action,
dry_run, target:{window_uid, element_id, selector, bounds},
before:{tree_hash, focused_selector}, after:{...}, changed, new_dialogs,
duration_ms}`.

```
POST /api/element/click
     {window_uid|window_index, element_id|selector,
      button?, count?, dry_run?, confirm_token?}

POST /api/element/focus          {window_*, element_id|selector, dry_run?}
POST /api/element/invoke         {window_*, element_id|selector, dry_run?}
POST /api/element/set_value      {window_*, element_id|selector, value, clear_first?, dry_run?}
POST /api/element/select         {window_*, element_id|selector, option_name|option_index, dry_run?}

POST /api/hover                  {x, y, hover_ms?}    or   element form
POST /api/element/right_click    {window_*, element_id|selector}
POST /api/element/double_click   {window_*, element_id|selector}
POST /api/element/key            {window_*, element_id|selector, keys}
POST /api/element/clear_text     {window_*, element_id|selector}
POST /api/drag
     {from:{x,y}|{selector}|{element_id},
      to:  {x,y}|{selector}|{element_id},
      modifiers?:[...], duration_s?, window_*}
```

### Observation with diff and synchronization

```
GET  /api/observe?window_*[&since=<tree_token>][&format=custom|json-patch]
     full:    {ok, format:"full",   tree, tree_token, base_token:null, ...}
     diff:    {ok, format:"custom"|"json-patch",
               changes:[...], tree_token, base_token, unchanged, tree_hash}

GET  /api/structure?window_*[&roles=R1,R2][&exclude_roles=...]
                    [&name_regex=...][&visible_only=true]
                    [&max_text_len=N][&prune_empty=true]
                    [&max_nodes=N][&page_cursor=…]
     → {ok, window, window_uid, element_count, node_count,
        tree, tree_hash, tree_token, truncated, next_cursor}

POST /api/wait_for
     {any_of:[
        {type:"element_appears",    selector:"..."},
        {type:"element_disappears", selector:"..."|element_id:"..."},
        {type:"text_visible",       regex:"..."},
        {type:"window_appears",     title_regex:"..."},
        {type:"window_disappears",  window_uid:"..."},
        {type:"tree_changes",       since:"<tree_token>"},
        {type:"focused_changes"}],
      timeout_ms?, poll_ms?, window_uid?}
     → match:   {ok, matched_index, matched_detail, elapsed_ms, polls}
     → timeout: {ok:false, error:{code:"Timeout",...}, elapsed_ms, polls}

POST /api/wait_idle  {window_*, quiet_ms?, timeout_ms?, poll_ms?}
```

### Composite action+observe

```
POST /api/element/click_and_observe   {…click args…, wait_after_ms?, since?}
POST /api/type_and_observe            {text, wait_after_ms?, since?, window_uid?}
POST /api/key_and_observe             {keys, wait_after_ms?, since?, window_uid?}
```
Returns the `ActionReceipt` plus `observation:` (the diff or full tree).

### Snapshots

```
POST   /api/snapshot               → {ok, snapshot_id, ts, summary}
GET    /api/snapshot/<id>          → {ok, windows, trees, tree_hashes, ts}
POST   /api/snapshot/diff          {a, b, format?}
                                   → {ok, windows_added, windows_removed,
                                       per_window_changes:{<uid>:{format,changes}}}
DELETE /api/snapshot/<id>          → {ok, dropped}
```

Snapshots have a 5-minute TTL and LRU-32 capacity per process.

### Tracing and replay

```
POST /api/trace/start    {label?}    → {ok, trace_id, started_at, dir}
POST /api/trace/stop                  → {ok, trace_id, path, step_count,
                                          duration_ms}
GET  /api/trace/status                → {ok, active_trace_id, step_count, dir}

POST /api/replay/start   {path, mode:"execute"|"verify",
                          on_divergence:"stop"|"warn"|"resume"}
                                       → {ok, replay_id, total, mode, label}
POST /api/replay/step    {replay_id}   → {ok, position, total, tool,
                                          divergence?, actual_summary, finished}
POST /api/replay/status  {replay_id}   → {ok, position, total, finished,
                                          divergences:[…]}
POST /api/replay/stop    {replay_id}   → {ok, stopped}
```

Trace files live under `traces/<trace_id>/`:
- `trace.jsonl`               one record per tool call
- `screenshots/step-NNNNN-full.png`     (cadence-gated)
- `screenshots/step-NNNNN-window.png`   (cadence-gated)

Per-tool comparison rules used by `mode="verify"` are defined in
`replay.py:COMPARE_FIELDS`.  Recorded result_summary fields outside the
rule set are ignored.

### Scenarios (mock mode)

```
POST /api/scenario/load   {path}    → {ok, scenario, state, states:[...]}
```

Or as a CLI flag: `python main.py --mock --scenario scenarios_examples/login.yaml`.

YAML schema (excerpt — full reference in `agentic_features_design.md` §15.5):

```yaml
name: …
initial_state: <state-name>
states:
  <name>:
    windows:
      - uid: mock:<id>
        title: …
        bounds: {x,y,width,height}
        tree: {role, name, value?, id?, bounds?, secret?, children: […]}
reactions:
  - on: {tool: type_text, target_id: u, text_regex: "(.+)"}
    when: [{id: …, value: …}, {id: …, value_not_empty: true}]
    set: [{id: …, value: "{text}"}]
    transition_to: <next-state>
oracles:
  success: [<predicate>]
  failure: [<predicate>]
```

Notes:
- `on`, `match`, or YAML's `True` (the `on` alias quirk) are all accepted.
- Reactions fire after the action would have succeeded, in declaration
  order; the first match wins.
- `{text}` interpolation pulls from `text_regex`'s first capture group.

### Oracles

```
POST /api/assert_state    {predicate: [
    {kind:"element_exists",   selector, window_uid?},
    {kind:"element_absent",   selector, window_uid?},
    {kind:"value_equals",     selector, expected},
    {kind:"value_matches",    selector, regex},
    {kind:"text_visible",     regex, mode?:"tree"|"ocr"|"auto"},
    {kind:"window_focused",   title_regex},
    {kind:"window_exists",    title_regex|window_uid},
    {kind:"tree_hash_equals", expected_hash},
    {kind:"screenshot_similar", reference_path, min_ssim?}
]}
    → {ok, all_passed, results:[{kind, passed, observed, args}]}
```

### Budgets, redaction, audit, propose_action

```
GET  /api/budget_status     → {ok, configured, actions, screenshots,
                                vlm_tokens, session_seconds, actions_per_minute}

GET  /api/redaction_status  → {ok, enabled, active, patterns_count,
                                applied_count, blur_screenshots}

POST /api/propose_action    {action, args:{...}}
                             → {ok, confirm_token, expires_at,
                                 would_target:{window_uid, selector, bounds,
                                               screenshot_b64}}
```

Budgets are enabled from CLI flags on `main.py`:

```
--max-actions N
--max-screenshots N
--max-vlm-tokens N
--max-session-seconds N
--actions-per-minute N
```

Redaction reads `config.redaction`:

```jsonc
{
  "enabled": true,
  "window_title_patterns": ["1Password"],
  "element_name_patterns": ["Password","PIN"],
  "element_role_patterns": ["PasswordEdit"],
  "ocr_text_patterns":     ["\\b\\d{3}-\\d{2}-\\d{4}\\b"],
  "replacement":           "[REDACTED]",
  "blur_screenshots":      false
}
```

Audit log opted in via `config.logging`:

```jsonc
{
  "logging": {
    "audit":            true,
    "audit_path":       "./audit.log",
    "audit_max_bytes":  10485760,
    "audit_backups":    3
  },
  "audit": {
    "redact_arg_keys": ["text","value","password","api_key","Authorization"]
  }
}
```

Allowlist via `config.actions`:

```jsonc
{
  "actions": {
    "allow":   ["click_element","focus_element","wait_for"],
    "deny":    ["press_key","type_text"],
    "default": "allow"
  }
}
```

Confirmation tokens via `config.confirmation_required`:

```jsonc
{
  "confirmation_required": [
    {"name_regex": "(?i)delete|remove|send|pay|sign"},
    {"role": "Button", "name_regex": "(?i)submit"}
  ],
  "confirmation": {"bbox_tolerance_px": 20, "ttl_seconds": 60}
}
```

### Health and metrics

```
GET /api/healthz            → {ok, uptime_s, step_count, adapter, version}
GET /api/metrics            → Prometheus text format (oso_step_count,
                               oso_uptime_seconds, oso_actions_used,
                               oso_screenshots_used, oso_active_trace)
                               Content-Type: text/plain
```

### Error envelope (v2 endpoints)

```json
{
  "ok": false,
  "success": false,
  "step_id": 99,
  "error": {
    "code": "ElementNotFound",
    "message": "no element matches selector …",
    "recoverable": true,
    "suggested_next_tool": "find_element",
    "context": {"selector": "...", "window_uid": "..."}
  }
}
```

Codes (full table with HTTP statuses in `errors.py`):
`ElementNotFound`, `ElementOccluded`, `ElementDisabled`, `WindowGone`,
`WindowOccluded`, `Timeout`, `PatternUnsupported`, `RateLimited`,
`BudgetExceeded`, `PermissionDenied`, `ConfirmationRequired`,
`ConfirmationInvalid`, `SnapshotExpired`, `ScenarioInvalid`,
`PlatformUnsupported`, `PredicateUnsupported`, `BadRequest`, `Internal`.

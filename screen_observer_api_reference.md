# OS Screen Observer — API Reference for Claude Code

This document describes how to advertise the OS Screen Observer tools to an
LLM and how to call the REST API that backs them. It covers tool definition
schemas, every endpoint's URL, method, query parameters, request body, and
the exact shape of every response including errors.

The server runs on `http://127.0.0.1:5001` by default. All endpoints return
`Content-Type: application/json`. There is no authentication.

---

## Startup

Start the server before making any requests:

```bash
python main.py --mode inspect          # REST only
python main.py --mode both             # REST + MCP stdio simultaneously
python main.py --mock --mode inspect   # synthetic data, no OS access needed
```

The server is ready when `GET /api/windows` returns a 200 response. Poll
with a short sleep until it succeeds:

```python
import time, httpx
for _ in range(20):
    try:
        httpx.get("http://127.0.0.1:5001/api/windows", timeout=2).raise_for_status()
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
                "Return a textual description of the window using one of three "
                "analysis modes. "
                "mode='accessibility': serialize the element tree to structured prose "
                "(instant, no extra API calls, best default). "
                "mode='ocr': extract visible text via Tesseract OCR on a screenshot "
                "(captures text not in the accessibility tree; requires pytesseract). "
                "mode='vlm': send the screenshot to a vision model for rich "
                "contextual interpretation (highest fidelity; requires ANTHROPIC_API_KEY "
                "and vlm.enabled=true in server config). "
                "mode='combined': return all enabled modalities in one call."
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
| `get_screen_description` | `GET /api/description?mode=<mode>`                   |
| `get_screen_sketch`    | `GET /api/sketch`                                      |
| `get_screenshot`       | `GET /api/screenshot`                                  |
| `click_at`             | `POST /api/action` `{"action":"click_at",...}`         |
| `type_text`            | `POST /api/action` `{"action":"type",...}`             |
| `press_key`            | `POST /api/action` `{"action":"key",...}`              |

---

## REST API Reference

### Common Conventions

- Base URL: `http://127.0.0.1:5001` (configurable via `config.json`)
- All responses: `Content-Type: application/json`
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

Return a textual description of the window via one of three analysis modes.

**Query parameters:**

| Parameter      | Type    | Required | Default          | Description                  |
|----------------|---------|----------|------------------|------------------------------|
| `window_index` | integer | No       | focused window   |                              |
| `mode`         | string  | No       | `accessibility`  | `accessibility` \| `ocr` \| `vlm` \| `combined` |

**Response — mode=accessibility:**

```jsonc
{
  "mode": "accessibility",
  "description": "Application : notepad.exe\nWindow      : Untitled — Notepad\nGeometry    : (80, 60)  800 × 600 px\n\nRoot: Window  \"Untitled — Notepad\"\n  └─ MenuBar\n     └─ MenuItem \"File\"  @(80,60) 56×22\n     └─ MenuItem \"Edit\"  @(138,60) 56×22\n  └─ Document \"Text Editor\" = 'Hello, world!\\nThis is a test...'  [FOCUSED]  @(80,82) 800×514\n  └─ StatusBar\n     └─ Text \"Position\" = 'Ln 1, Col 1'  @(80,614) 188×22\n\n[14 elements total; focused → Document \"Text Editor\"]"
}
```

**Response — mode=ocr:**

```jsonc
{
  "mode": "ocr",
  "description": "File  Edit  Format  View  Help\n\nHello, world!\nThis is a test document.\nLine 3 has some content here.\n\nLn 1, Col 1     100%     UTF-8     Windows (CRLF)"
}
```

**Response — mode=vlm:**

```jsonc
{
  "mode": "vlm",
  "description": "1. Application: Notepad (Windows built-in text editor)\n2. Main content: A plain-text document with three lines of content...\n3. UI controls: Menu bar with File/Edit/Format/View/Help items; vertical and horizontal scrollbars; status bar showing line/column position, zoom level, encoding, and line ending type\n4. Spatial layout: Menu bar spans full width at top; large editing area occupies most of the window; status bar at bottom\n5. Active element: The text editing area has keyboard focus (cursor visible at line 1, column 1)\n6. Natural next actions: Type text, use File > Save As to save, or use Format > Word Wrap to enable wrapping"
}
```

**Response — mode=combined:**

```jsonc
{
  "mode": "combined",
  "accessibility": "...",     // always present
  "ocr": "...",               // present when ocr.enabled=true in config
  "vlm": "..."                // present when vlm.enabled=true in config
}
```

**Notes:**
- `vlm` requires `ANTHROPIC_API_KEY` in the environment and
  `"vlm": {"enabled": true}` in `config.json`. Returns a disabled message
  string if not configured.
- `ocr` requires `pytesseract` installed and Tesseract on the system PATH.
  Returns a disabled message string if not configured.

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
  "sketch": "+------------------------------------------------------------------------+\n| Window \"Untitled — Notepad\"                                            |\n| +----------------------------------------------------------------------+ |\n| | MenuBar                                                              | |\n| | +--------+ +--------+ +----------+ +--------+ +--------+            | |\n| | | Menutem| |MenuItem| | MenuItem | |MenuItem| |MenuItem|            | |\n| | | \"File\" | | \"Edit\" | | \"Format\" | | \"View\" | | \"Help\" |            | |\n| | +--------+ +--------+ +----------+ +--------+ +--------+            | |\n| +----------------------------------------------------------------------+ |\n| +----------------------------------------------------------------------+ |\n| | Document \"Text Editor\" [hello, world!...] *FOCUSED*                 | |\n| |                                                                      | |\n| |                                                                      | |\n| +----------------------------------------------------------------------+ |\n| +----------------------------------------------------+ +--------------+ |\n| | StatusBar                                          | | ScrollBar    | |\n| | Text \"Position\" [Ln 1, Col 1]                      | |              | |\n| +----------------------------------------------------+ +--------------+ |\n+------------------------------------------------------------------------+"
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

GET  /api/description?window_index=N&mode=M
     M = accessibility | ocr | vlm | combined
     → {mode, description}   or   {mode, accessibility, ocr, vlm}

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
```

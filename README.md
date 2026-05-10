# OS Screen Observer

A prototype that exposes the operating system's UI accessibility tree,
textual descriptions, and ASCII spatial sketches through two simultaneous
interfaces:

- **Web inspector** (human-facing) — a browser-based dashboard at `localhost:5001`
- **MCP server** (AI-facing) — a stdio MCP server compatible with Claude Desktop and Claude Code

Both interfaces share the same underlying observer and can run simultaneously.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  main.py                                                                │
│  ┌──────────────────────┐      ┌───────────────────────────────────┐    │
│  │  Flask web inspector │      │  MCP stdio server                 │    │
│  │  (background thread) │      │  (main thread, stdin/stdout)      │    │
│  └──────────┬───────────┘      └──────────────────┬────────────────┘    │
│             │                                     │                     │
│             └──────────────┬──────────────────────┘                     │
│                            ▼                                            │
│                    ScreenObserver                                       │
│                   /      |       \                                      │
│          Accessibility  ASCII    Description                            │
│             Tree      Renderer   Generator                              │
│           (observer)             (description)                          │
│                                  ┌──── accessibility (tree prose)       │
│                                  ├──── ocr (Tesseract)                  │
│                                  └──── vlm (Claude Vision)              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Installation

### 1. Python environment

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Platform-specific setup

**Windows (full UIA support)**
```bash
pip install pywinauto pywin32 psutil
```

**macOS (screenshot only; AX tree is stub)**
```bash
# mss and pyautogui handle screenshot and actions.
# Full AX tree support requires pyobjc (contributions welcome).
pip install pyobjc  # optional
```

**Linux (screenshot only; AT-SPI tree is stub)**
```bash
sudo apt install wmctrl  # for window enumeration
# Full AT-SPI tree support requires pyatspi (contributions welcome).
pip install pyatspi  # optional
```

**OCR (optional, all platforms)**
```bash
# Install Tesseract:
#   Windows: https://github.com/tesseract-ocr/tesseract/releases
#   macOS:   brew install tesseract
#   Linux:   sudo apt install tesseract-ocr

pip install pytesseract
```

**VLM descriptions (optional, all platforms)**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
# Then set vlm.enabled = true in config.json
```

---

## Running

### Both interfaces (default)

```bash
python main.py
# Web inspector: http://127.0.0.1:5001
# MCP server:    stdin/stdout (for Claude Desktop)
```

### Web inspector only

```bash
python main.py --mode inspect
```

### MCP server only

```bash
python main.py --mode mcp
```

### Mock mode (no OS access required — useful for development)

```bash
python main.py --mock
# or
python main.py --mock --mode inspect
```

### Custom port

```bash
python main.py --port 8080
```

---

## Web Inspector

Open **http://127.0.0.1:5001** in a browser after starting with `--mode inspect`
or `--mode both`.

| Tab | Content |
|-----|---------|
| **STRUCTURE** | Interactive collapsible JSON tree of the accessibility element hierarchy |
| **DESCRIPTION** | Prose description; mode selector switches between accessibility / OCR / VLM / combined |
| **SKETCH** | ASCII spatial layout diagram (Unicode box-drawing characters) |
| **SCREENSHOT** | Pixel screenshot, visible-area bounding boxes, and ASCII sketch (all in one panel) |
| **ACTIONS** | Click at coordinates, type text, press key combinations |

The sidebar lists all visible windows. Click one to select it. All tabs
update to reflect the selected window. Enable **AUTO-REFRESH** to poll
every 3 seconds.

---

## MCP Integration (Claude Desktop)

Add the following block to your Claude Desktop configuration
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "os-screen-observer": {
      "command": "python",
      "args": [
        "/absolute/path/to/screen_observer/main.py",
        "--mode", "both"
      ]
    }
  }
}
```

To run with mock data during development:

```json
{
  "mcpServers": {
    "os-screen-observer": {
      "command": "python",
      "args": [
        "/absolute/path/to/screen_observer/main.py",
        "--mode", "both",
        "--mock"
      ]
    }
  }
}
```

Restart Claude Desktop after editing the config. The server will appear
in the tools menu. You can then ask Claude to:

- "List the windows currently open on my desktop."
- "Show me the accessibility tree for the focused window."
- "Give me an ASCII sketch of the Notepad window layout."
- "Describe what is on the screen using OCR."
- "Click at coordinates 400, 300."
- "Type 'hello world' into the focused field."

---

## Available MCP Tools

| Tool | Description |
|------|-------------|
| `list_windows` | Enumerate all visible top-level windows |
| `get_window_structure` | Full accessibility element tree as JSON |
| `get_screen_description` | Prose description (accessibility / ocr / vlm / combined) |
| `get_screen_sketch` | ASCII spatial layout diagram |
| `get_screenshot` | Screenshot as base64 PNG |
| `get_full_screenshot` | Screenshot + ASCII sketch in one call (sketch uses OCR overlay) |
| `get_visible_areas` | Visible (non-occluded, on-screen) bounding boxes for a window |
| `click_at` | Click at pixel coordinates |
| `type_text` | Type text into the focused element |
| `press_key` | Press a key combination (e.g., `ctrl+c`, `alt+f4`) |

---

## REST API Reference

The web inspector exposes the following endpoints (all `GET` unless noted):

| Endpoint | Params | Description |
|----------|--------|-------------|
| `GET /api/windows` | — | List all visible windows |
| `GET /api/structure` | `window_index` | Accessibility element tree (JSON) |
| `GET /api/description` | `window_index`, `mode` | Prose description |
| `GET /api/sketch` | `window_index`, `grid_width`, `grid_height`, `ocr` | ASCII layout sketch |
| `GET /api/screenshot` | `window_index` | Screenshot as base64 PNG |
| `GET /api/full_screenshot` | `window_index`, `grid_width`, `grid_height` | Screenshot + ASCII sketch (sketch uses OCR overlay) |
| `GET /api/visible_areas` | `window_index` *(required)* | Visible non-occluded bounding boxes |
| `POST /api/action` | JSON body `{action, …}` | Execute click / type / key / scroll |

### `GET /api/full_screenshot`

Returns a combined response so callers don't need two round-trips:

```json
{
  "window":   "Untitled — Notepad",
  "format":   "png",
  "encoding": "base64",
  "width":    800,
  "height":   600,
  "data":     "<base64 PNG>",
  "sketch":   "┌── Window ──…"
}
```

### `GET /api/visible_areas`

Returns the portions of a window that are visible on screen — not covered by
other windows and within the monitor area:

```json
{
  "window": "Untitled — Notepad",
  "visible_regions": [
    {"x": 80, "y": 60, "width": 800, "height": 400},
    {"x": 80, "y": 500, "width": 400, "height": 160}
  ]
}
```

Each entry is a rectangle in absolute screen pixels. If the window is fully
visible a single region covering the entire window is returned. If the window
is fully off-screen or completely covered, the list is empty.

---

## Configuration Reference (`config.json`)

```json
{
  "web_ui": {
    "host":  "127.0.0.1",   // bind address for Flask
    "port":  5001,           // HTTP port
    "debug": false
  },
  "mcp": {
    "server_name": "os-screen-observer",
    "version":     "0.1.0"
  },
  "ocr": {
    "enabled":         true,
    "tesseract_cmd":   null,   // e.g. "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    "min_confidence":  30      // 0–100; words below this threshold are discarded
  },
  "vlm": {
    "enabled":    false,       // set true + ANTHROPIC_API_KEY to enable
    "model":      "claude-sonnet-4-20250514",
    "max_tokens": 1500
  },
  "ascii_sketch": {
    "grid_width":  110,        // output width in characters
    "grid_height":  38,        // output height in characters
    "unicode_box": true        // false → plain ASCII +/-/| instead of ┌─┐│└┘
  },
  "tree": {
    "max_depth": 8             // maximum depth to traverse (Windows only)
  },
  "logging": {
    "level": "INFO"            // DEBUG / INFO / WARNING / ERROR
  },
  "mock":    false,            // force mock adapter regardless of platform
  "platform": "auto"          // "auto" | "Windows" | "Darwin" | "Linux"
}
```

---

## Project Layout

```
screen_observer/
├── main.py            Entry point; argument parsing; thread coordination
├── config.json        Runtime configuration
├── requirements.txt   Python dependencies
├── observer.py        Platform adapters + ScreenObserver facade
├── ascii_renderer.py  ASCII spatial sketch renderer
├── description.py     Textual description generator (tree / OCR / VLM)
├── mcp_server.py      MCP JSON-RPC 2.0 stdio server
└── web_inspector.py   Flask REST API + embedded single-page UI
```

---

## Platform Support Status

| Feature | Windows | macOS | Linux |
|---------|---------|-------|-------|
| Window enumeration | ✅ Full | ✅ (via AppleScript) | ✅ (via wmctrl) |
| Accessibility tree | ✅ Full UIA | 🔶 Stub | 🔶 Stub |
| Screenshot | ✅ | ✅ | ✅ |
| OCR | ✅ | ✅ | ✅ |
| VLM description | ✅ | ✅ | ✅ |
| ASCII sketch | ✅ Full | 🔶 Sketch from stub tree | 🔶 Sketch from stub tree |
| Input actions | ✅ | ✅ | ✅ |
| Mock mode | ✅ | ✅ | ✅ |

Full macOS AX tree support requires implementing `MacOSAdapter.get_element_tree()`
using `pyobjc` (`AXUIElementCreateSystemWide`, `kAXChildrenAttribute`, etc.).
Full Linux AT-SPI tree support requires implementing `LinuxAdapter.get_element_tree()`
using `pyatspi` (`pyatspi.Registry.getDesktop(0)`, `pyatspi.findAllDescendants()`).
Both are well-understood engineering tasks; the adapter stubs in `observer.py`
provide the correct extension points.

---

## Known Limitations (Prototype)

1. **Accessibility-dark applications** — Games, Electron apps with custom renderers,
   and applications that do not instrument UIA will produce sparse trees. The OCR
   and VLM modalities degrade more gracefully in these cases.

2. **Prompt injection risk** — Screen content is included verbatim in tool results.
   Malicious content on-screen could attempt to influence the AI's behavior. Apply
   appropriate trust boundaries when using this server in production contexts.

3. **Performance** — Full tree traversal on a complex window (e.g., a browser with
   many DOM-mapped UIA nodes) can take several seconds. The `tree.max_depth`
   config key limits traversal depth.

4. **Action safety** — The `click_at`, `type_text`, and `press_key` tools execute
   real input events. Apply appropriate authorization controls before exposing
   this server to an untrusted AI client in production.

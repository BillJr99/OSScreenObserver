# OS Screen Observer

A prototype that exposes the operating system's UI accessibility tree,
textual descriptions, and ASCII spatial sketches through two simultaneous
interfaces:

- **Web inspector** (human-facing) — a browser-based dashboard at `localhost:5001`
- **MCP server** (AI-facing) — a stdio MCP server compatible with Claude Desktop and Claude Code

Both interfaces share the same underlying observer and can run simultaneously.

---

## REST API

OSScreenObserver exposes a full REST API on port `5001` (configurable). Most `/api/*` endpoints return JSON; `/api/metrics` returns `text/plain` (Prometheus format) and `/` returns HTML.

> ### Security & Network Bind Defaults
>
> **The default bind host is now `0.0.0.0` (all network interfaces) on port `5001`.**
> This default is intended for **testing inside an isolated sandbox or container** (e.g., a disposable VM/dev container) where exposing the API to the container network is convenient and safe.
>
> **The REST API has NO authentication.** Any client that can reach the port can call every endpoint, including `/api/action`, which can click, type, and otherwise control the host desktop.
>
> **For local-only use on a workstation, override the bind to loopback:**
>
> ```bash
> # Command-line override (recommended for local dev)
> python main.py --mode both --host 127.0.0.1
> ```
>
> Or edit `config.json`:
>
> ```json
> {
>   "web_ui": { "host": "127.0.0.1", "port": 5001 }
> }
> ```
>
> Do **not** expose the default `0.0.0.0` bind on a workstation connected to an untrusted network (home Wi-Fi, café, corporate LAN, public cloud VM) without a firewall, reverse proxy with authentication, or VPN in front of it.
>
> **CORS warning:** The Flask server enables permissive CORS for all routes by default (`CORS(app)`). Any website running in the user's browser can send cross-origin requests to the API — including destructive `/api/action` calls. Restrict CORS origins or add an authentication/proxy layer before exposing the server to a multi-user environment.

### Startup modes

```bash
python main.py --mode inspect          # HTTP server only (web UI + REST API)
python main.py --mode both             # REST API + MCP stdio simultaneously
python main.py --mock --mode inspect   # Mock mode with synthetic data (no OS access)
python main.py --mock --scenario scenarios_examples/login.yaml  # Scenario-driven mock
```

### Health check (poll until ready)

```bash
curl http://127.0.0.1:5001/api/healthz
```

### Endpoint quick reference

| Method | Endpoint | Description |
|--------|----------|--------------|
| `GET` | `/api/windows` | List visible top-level windows |
| `GET` | `/api/structure` | Full accessibility element tree (JSON) |
| `GET` | `/api/description` | Combined screen description (accessibility + OCR + VLM) — `mode` query parameter is accepted but ignored — always returns combined output |
| `GET` | `/api/sketch` | ASCII spatial layout diagram |
| `GET` | `/api/screenshot` | Base64-encoded PNG screenshot |
| `POST` | `/api/action` | Execute click, type, key, or scroll action |
| `GET` | `/api/capabilities` | Server capabilities and platform info |
| `GET` | `/api/healthz` | Health and uptime |
| `GET` | `/api/metrics` | Prometheus-format metrics (`text/plain`) |

### Example workflow

```bash
# 1. List windows
curl http://127.0.0.1:5001/api/windows

# 2. Get combined description of focused window (all available sources)
curl http://127.0.0.1:5001/api/description

# 3. Get element tree for precise coordinates
curl http://127.0.0.1:5001/api/structure

# 4. Click a button at coordinates
curl -X POST http://127.0.0.1:5001/api/action \
  -H "Content-Type: application/json" \
  -d '{"action": "click_at", "x": 480, "y": 300}'
```

### Full API reference

See [screen_observer_api_reference.md](screen_observer_api_reference.md) for complete endpoint documentation including v2 agentic endpoints (snapshots, tracing, replay, scenarios, oracles, budgets, redaction). (Note: `/api/metrics` returns plain text and `/` returns an HTML page, not JSON; the reference doc has been updated to reflect these exceptions.)

### LLM tool integration

The REST API endpoints map directly to the `SCREEN_TOOLS` OpenAI/OpenWebUI function-calling schema documented in `screen_observer_api_reference.md`. Any system that supports OpenAI-compatible tool use can integrate OSScreenObserver using these tool schemas.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  main.py                                                                │
│  ┌──────────────────────┐      ┌───────────────────────────────────┐    │
│  │  Flask web inspector │      │  MCP stdio server                 │    │
│  │  (background thread) │      │  (main thread, stdin/stdout)      │    │
│  └──────────┤───────────┘      └──────────────────剌────────────────┘    │
│             │                                     │                     │
│             └──────────────┬──────────────────────┘                     │
│                            │                                            │
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
# macOS/Linux/WSL:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Platform-specific setup

**Windows (primary — full UIA support)**
```bash
pip install pywinauto pywin32 psutil
```

**macOS**
```bash
# mss and pyautogui handle screenshots and actions.
pip install pyobjc          # enables full AX accessibility tree
```

**Linux**
```bash
sudo apt install wmctrl     # window enumeration
pip install pyatspi         # enables full AT-SPI accessibility tree (optional)
```

**WSL (Windows Subsystem for Linux)**

The server auto-detects WSL and uses PowerShell for screenshots and window
listing when no X11 display is available. Set `DISPLAY` for X11 forwarding to
also enable accessibility tools.

### 3. Description sources (optional, all platforms)

`get_screen_description` always runs in *combined* mode and returns every
source that is available. The web inspector's Description tab shows which
sources ran and how to enable any that are missing.

**OCR (Tesseract)**
```bash
# Windows:  download from https://github.com/tesseract-ocr/tesseract/releases
# macOS:    brew install tesseract
# Linux:    sudo apt install tesseract-ocr

pip install pytesseract
```

On **Windows** the Tesseract installer does not add the binary to `PATH`.
Point the server at it in `config.json`:

```jsonc
{
  "ocr": {
    "enabled": true,
    // forward slashes work on Windows too:
    "tesseract_cmd": "C:/Program Files/Tesseract-OCR/tesseract.exe"
  }
}
```

If the JSON parser rejects the file (forgotten backslash escape) the server
logs a `[main:load_config]` error and reports `config_error` at `GET /api/healthz`.

**VLM / Claude Vision (optional, all platforms)**
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
|-----|------|
| **STRUCTURE** | Interactive collapsible JSON tree of the accessibility element hierarchy |
| **DESCRIPTION** | Combined description from all available sources (accessibility tree, OCR, VLM). Each source is shown in its own labeled section. A badge row at the top shows which sources ran and how to enable any that are missing. |
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
| `get_screen_description` | Combined description from all available sources (accessibility tree + OCR + VLM). No mode parameter needed — returns everything the platform supports. |
| `get_screen_sketch` | ASCII spatial layout diagram |
| `get_screenshot` | Screenshot as base64 PNG |
| `get_full_screenshot` | Screenshot + ASCII sketch in one call (sketch uses OCR overlay) |
| `get_visible_areas` | Visible (non-occluded, on-screen) bounding boxes for a window |
| `bring_to_foreground` | Raise a window using the platform focus API; falls back to title-bar click |
| `click_at` | Click at pixel coordinates |
| `type_text` | Type text into the focused element |
| `press_key` | Press a key combination (e.g., `ctrl+c`, `alt+f4`) |
| `scroll` | Scroll the mouse wheel at an optional screen position |

---

## REST API Reference

The web inspector exposes the following endpoints (all `GET` unless noted):

| Endpoint | Params | Description |
|----------|--------|-------------|
| `GET /api/windows` | — | List all visible windows |
| `GET /api/structure` | `window_index` | Accessibility element tree (JSON) |
| `GET /api/description` | `window_index` | Combined description (accessibility + OCR + VLM, whatever is available). `mode` query parameter is accepted but ignored — always returns combined output. |
| `GET /api/sketch` | `window_index`, `grid_width`, `grid_height`, `ocr` | ASCII layout sketch |
| `GET /api/screenshot` | `window_index` | Screenshot as base64 PNG |
| `GET /api/full_screenshot` | `window_index`, `grid_width`, `grid_height` | Screenshot + ASCII sketch (sketch uses OCR overlay) |
| `GET /api/visible_areas` | `window_index` *(required)* | Visible non-occluded bounding boxes |
| `GET /api/bring_to_foreground` | `window_index` *(required)* | Click the title bar to raise the window |
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

### `GET /api/bring_to_foreground`

Raises a window by clicking in its title-bar area. The server selects the
top-most visible region of the window and clicks ~20 px below its top edge:

```json
{
  "window":    "Untitled — Notepad",
  "success":   true,
  "action":    "click_at",
  "clicked_x": 960,
  "clicked_y": 80
}
```

`window_index` is required. If the window has no visible area the response
contains `"success": false` with an explanatory error message — the click is
**not** attempted in that case.

**Platform notes**

| Platform | Occlusion detection |
|----------|-----------------------|
| Windows  | Real Z-order via `win32gui`: a fully-covered window returns `success: false` |
| macOS / Linux | Z-order unavailable; the window is assumed to be on top, so the screen-clipped bounds are used. A fully-covered window may still produce a click that lands on the covering window. |

---

## Configuration Reference (`config.json`)

```json
{
  "web_ui": {
    "host":  "0.0.0.0",     // bind address; use "127.0.0.1" for loopback-only
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

| Feature | Windows | macOS | Linux | WSL |
|---------|---------|-------|-------|-----|
| Window enumeration | Full (`win32gui`) | Supported (`Quartz` / AppleScript) | Supported (`wmctrl`) | Supported (`wmctrl`) or PowerShell fallback |
| Accessibility tree | Full (UIA + pywinauto) | Supported (`pyobjc` AXUIElement) | Supported (`pyatspi`) | Stub (no X11 without DISPLAY) |
| Screenshot | `PrintWindow` → `mss` | `mss` | `mss` → `scrot` | `mss` (if DISPLAY) or PowerShell |
| OCR | yes | yes | yes | yes |
| VLM description | yes | yes | yes | yes |
| ASCII sketch | yes | yes | yes | yes |
| Input actions | yes | yes | yes | yes (`pyautogui` needs DISPLAY) |
| Mock mode | yes | yes | yes | yes |

`get_screen_description` always returns everything the current platform supports
in a single call — no mode parameter required. The web inspector's Description
tab shows which sources ran and how to enable missing ones.

All adapters degrade gracefully: if a library is not installed or a capability
is unavailable, the server continues running and returns whatever it can.
Optional dependencies for macOS (`pyobjc`) and Linux (`pyatspi`) are auto-installed
via `mac_adapter.py` / `linux_adapter.py` when present.

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

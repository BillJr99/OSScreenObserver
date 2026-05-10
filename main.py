"""
main.py — Entry point for the OS Screen Observer.

Usage
─────
  # Both MCP server (stdio) + web inspector (port 5001) simultaneously
  python main.py

  # Web inspector only (useful for manual exploration)
  python main.py --mode inspect

  # MCP stdio only (useful when launched by Claude Desktop)
  python main.py --mode mcp

  # Force mock adapter (no OS access required; safe in any environment)
  python main.py --mock

  # Custom config and port
  python main.py --config /path/to/config.json --port 5002

Threading model
───────────────
  "both" mode:   Flask runs on a background daemon thread (port 5001).
                 The MCP server runs on the main thread reading stdin/stdout.
                 Both share the same ScreenObserver, ASCIIRenderer, and
                 DescriptionGenerator instances (the observer layer is
                 stateless between calls, so no locking is needed).

  "inspect" mode: Flask runs on the main thread; no MCP server.

  "mcp" mode:    MCP server runs on the main thread; no Flask server.

ALL logging is directed to stderr so that the MCP framing on stdout is
never polluted regardless of mode.
"""

import argparse
import json
import logging
import os
import sys
import threading
import traceback


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "web_ui":  {"host": "127.0.0.1", "port": 5001, "debug": False},
    "mcp":     {"server_name": "os-screen-observer", "version": "0.1.0"},
    "ocr":     {"enabled": True, "tesseract_cmd": None, "min_confidence": 30},
    "vlm":     {"enabled": False, "model": "claude-sonnet-4-20250514", "max_tokens": 1500},
    "ascii_sketch": {"grid_width": 110, "grid_height": 38, "unicode_box": True},
    "tree":    {"max_depth": 8},
    "logging": {"level": "INFO"},
    "mock":    False,
    "platform": "auto",
}


def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            cfg = json.load(f)
        # Deep-merge with defaults so missing keys are always present
        merged = dict(_DEFAULT_CONFIG)
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        return merged
    except FileNotFoundError:
        print(f"[main:load_config] Config not found at {path!r}; using built-in defaults",
              file=sys.stderr)
        return dict(_DEFAULT_CONFIG)
    except Exception as e:
        print(f"[main:load_config] {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return dict(_DEFAULT_CONFIG)


def setup_logging(config: dict) -> None:
    level_name = config.get("logging", {}).get("level", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level   = level,
        format  = "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt = "%H:%M:%S",
        stream  = sys.stderr,   # ← critical: never pollute MCP stdout
    )


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog        = "screen_observer",
        description = "OS Screen Observer: MCP server + web inspection UI.",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
examples:
  python main.py                          # both MCP + web UI
  python main.py --mode inspect           # web UI only
  python main.py --mode mcp              # MCP stdio only
  python main.py --mock                   # mock data (no OS access needed)
  python main.py --mock --mode inspect --port 8080
        """,
    )
    p.add_argument("--mode",   choices=["mcp", "inspect", "both"], default="both",
                   help="Run mode (default: both)")
    p.add_argument("--config", default="config.json",
                   help="Path to JSON config file (default: config.json)")
    p.add_argument("--mock",   action="store_true",
                   help="Force mock adapter — no real OS access required")
    p.add_argument("--port",   type=int,
                   help="Override web UI port from config")
    p.add_argument("--host",
                   help="Override web UI bind host from config")
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    args   = build_parser().parse_args()
    config = load_config(args.config)

    # Command-line overrides
    if args.mock:
        config["mock"] = True
    if args.port:
        config["web_ui"]["port"] = args.port
    if args.host:
        config["web_ui"]["host"] = args.host

    setup_logging(config)
    logger = logging.getLogger("main")

    # ── Lazy imports (so logging is configured before module-level init runs)
    try:
        from observer     import ScreenObserver
        from ascii_renderer import ASCIIRenderer
        from description  import DescriptionGenerator
    except Exception as e:
        print(f"[main] Fatal import error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # ── Shared instances ─────────────────────────────────────────────────────
    try:
        observer  = ScreenObserver(config)
        renderer  = ASCIIRenderer(config)
        describer = DescriptionGenerator(config)
    except Exception as e:
        print(f"[main] Failed to initialize observer: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    adapter_type = "MOCK" if observer.is_mock else "LIVE"
    logger.info(f"[main] Observer ready (adapter: {adapter_type})")

    # ── Web inspector ────────────────────────────────────────────────────────
    if args.mode in ("inspect", "both"):
        from web_inspector import create_web_app

        host = config["web_ui"]["host"]
        port = config["web_ui"]["port"]
        app  = create_web_app(observer, renderer, describer, config)

        def _run_flask():
            try:
                # use_reloader=False is essential — reloader spawns a child process
                # that would conflict with the MCP stdio setup on the main thread.
                app.run(host=host, port=port, debug=False, use_reloader=False)
            except Exception as e:
                print(f"[main:flask_thread] {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

        flask_thread = threading.Thread(target=_run_flask, daemon=True, name="web-inspector")
        flask_thread.start()

        print(f"[screen_observer] Web inspector → http://{host}:{port}", file=sys.stderr)
        logger.info(f"[main] Web inspector running at http://{host}:{port}")

        if args.mode == "inspect":
            # In inspect-only mode the Flask thread is all there is; join it
            # so the process doesn't exit immediately.
            try:
                flask_thread.join()
            except KeyboardInterrupt:
                print("\n[screen_observer] Shutting down.", file=sys.stderr)
            return

    # ── MCP stdio server (runs on main thread) ───────────────────────────────
    if args.mode in ("mcp", "both"):
        from mcp_server import MCPServer

        server = MCPServer(observer, renderer, describer, config)
        try:
            server.run()
        except KeyboardInterrupt:
            print("\n[screen_observer] MCP server stopped.", file=sys.stderr)
        except Exception as e:
            print(f"[main:mcp_server] {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()

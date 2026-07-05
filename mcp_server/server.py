"""
MCP stdio server: JSON-RPC 2.0 transport, message
dispatch and legacy composite tool handlers.

Split out of mcp_server.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any, Dict

from ascii_renderer import ASCIIRenderer
from description import DescriptionGenerator
from observer import ScreenObserver
import tools as _tools

from mcp_server.tool_schemas import _TOOLS

logger = logging.getLogger(__name__)


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
            vlm_mode = (self.describer.vlm_cfg.get("mode") or "single").lower()
            if vlm_mode == "multipass":
                env = self.describer.from_vlm_multipass(
                    shot, root=tree, window=info,
                )
                if env is None:
                    return {"mode": mode,
                            "description": "[VLM unavailable — check vlm.base_url and vlm.model in config.json]"}
                return {"mode": mode,
                        "description": json.dumps(env, indent=2, ensure_ascii=False),
                        "vlm_structured": env}
            vlm_out = self.describer.from_vlm(shot, root=tree, window=info)
            if vlm_out is None:
                return {"mode": mode, "description": "[VLM unavailable — check vlm.base_url and vlm.model in config.json]"}
            return {"mode": mode, "description": vlm_out}
        elif mode == "combined":
            return {"mode": mode, **self.describer.combined(tree, shot, info)}
        else:
            return {"error": f"Unknown mode: {mode}"}

    def _t_sketch(self, hwnd, info, args) -> Dict:
        tree = self.observer.get_element_tree(hwnd)
        if tree is None:
            return {"error": "Could not retrieve element tree"}

        ref = info.bounds if info else tree.bounds
        result = self.renderer.render_structured(
            root          = tree,
            screen_bounds = ref,
            grid_width    = args.get("grid_width"),
            grid_height   = args.get("grid_height"),
        )
        out = {
            "window": info.title if info else "(focused)",
            "grid_width":  args.get("grid_width",  self.renderer.default_width),
            "grid_height": args.get("grid_height", self.renderer.default_height),
            "sketch": result["sketch"],
        }
        if args.get("structured"):
            out["elements"] = result["elements"]
            out["legend"]   = result["legend"]
        return out

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
                except Exception as e:
                    logger.debug(
                        f"[get_full_screenshot] window crop for OCR overlay "
                        f"failed; using full image: {e}")
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
        except Exception as e:
            logger.debug(f"[get_full_screenshot] size probe failed: {e}")

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

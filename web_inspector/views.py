"""
Flask route registration for the inspection API + UI.

Split out of web_inspector.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import base64
import logging
import traceback
from typing import Optional

from flask import Flask, jsonify, request

from ascii_renderer import ASCIIRenderer
from description import DescriptionGenerator
from errors import http_status_for
from observer import ScreenObserver
import tools as _tools

from web_inspector.assets import _HTML

logger = logging.getLogger(__name__)


def register_routes(
    app:       Flask,
    *,
    observer:  ScreenObserver,
    renderer:  ASCIIRenderer,
    describer: DescriptionGenerator,
    config:    dict,
    ctx:       _tools.ToolContext,
) -> None:
    """Define all routes on *app*, closing over the shared
    observer/renderer/describer instances (moved verbatim out of
    create_web_app)."""
    def _tool_response(name: str, args: dict):
        result = _tools.dispatch(ctx, name, args)
        if not result.get("ok", True):
            code = (result.get("error") or {}).get("code", "Internal")
            return jsonify(result), http_status_for(code)
        return jsonify(result)

    def _merge_query(extra: Optional[dict] = None) -> dict:
        out: dict = {k: v for k, v in request.args.items()}
        if "window_index" in out:
            try:
                out["window_index"] = int(out["window_index"])
            except (TypeError, ValueError):
                pass
        if extra:
            out.update(extra)
        return out

    # ── UI ────────────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return _HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    # ── API helpers ───────────────────────────────────────────────────────────

    def _window_from_args():
        """Resolve window_uid, window_index, or window_title → (WindowInfo, hwnd, windows).

        Priority: window_uid > window_index > window_title (substring match).
        """
        windows = observer.list_windows()
        res = observer.resolve_window(
            windows,
            window_uid=request.args.get("window_uid"),
            window_index=int(request.args["window_index"]) if "window_index" in request.args else None,
            window_title=request.args.get("window_title"),
        )
        info = res.info
        hwnd = info.handle if info else None
        return info, hwnd, windows

    # ── /api/windows ──────────────────────────────────────────────────────────

    @app.route("/api/windows")
    def api_windows():
        return _tool_response("list_windows", {})

    # ── /api/structure ────────────────────────────────────────────────────────

    @app.route("/api/structure")
    def api_structure():
        # Forwards through tools.dispatch so callers can use roles=,
        # name_regex=, prune_empty=, max_nodes=, page_cursor= filters.
        args = _merge_query()
        for key in ("roles", "exclude_roles"):
            if key in args and isinstance(args[key], str):
                args[key] = [s for s in args[key].split(",") if s]
        for bool_key in ("visible_only", "prune_empty"):
            if bool_key in args:
                args[bool_key] = str(args[bool_key]).lower() in ("1", "true", "yes")
        for int_key in ("max_text_len", "max_nodes", "depth"):
            if int_key in args:
                try:
                    args[int_key] = int(args[int_key])
                except (TypeError, ValueError):
                    args.pop(int_key, None)
        return _tool_response("get_window_structure", args)

    # ── /api/description ──────────────────────────────────────────────────────

    @app.route("/api/description")
    def api_description():
        args = _merge_query()
        if "max_tokens" in args:
            try:
                args["max_tokens"] = int(args["max_tokens"])
            except (TypeError, ValueError):
                args.pop("max_tokens", None)
        return _tool_response("get_screen_description", args)

    # ── /api/sketch ───────────────────────────────────────────────────────────

    @app.route("/api/sketch")
    def api_sketch():
        try:
            info, hwnd, _ = _window_from_args()
            tree = observer.get_element_tree(hwnd)
            if tree is None:
                return jsonify({"error": "Could not retrieve element tree"}), 500

            gw  = request.args.get("grid_width",  type=int)
            gh  = request.args.get("grid_height", type=int)
            ref = info.bounds if info else tree.bounds

            # Optional OCR overlay: pass ?ocr=1 to enable Tesseract text overlay.
            # Requires pytesseract + tesseract on PATH; silently skipped otherwise.
            shot_bytes: Optional[bytes] = None
            if request.args.get("ocr", "").strip() in ("1", "true", "yes"):
                shot_bytes = observer.get_screenshot(hwnd)

            want_structured = request.args.get("structured", "").strip() in (
                "1", "true", "yes",
            )
            result = renderer.render_structured(
                root             = tree,
                screen_bounds    = ref,
                grid_width       = gw,
                grid_height      = gh,
                screenshot_bytes = shot_bytes,
            )
            payload = {
                "window":      info.title if info else "(focused)",
                "grid_width":  gw or renderer.default_width,
                "grid_height": gh or renderer.default_height,
                "ocr_overlay": shot_bytes is not None,
                "sketch":      result["sketch"],
            }
            if want_structured:
                payload["elements"] = result["elements"]
                payload["legend"]   = result["legend"]
            return jsonify(payload)
        except Exception as e:
            print(f"[web_inspector:/api/sketch] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/screenshot ───────────────────────────────────────────────────────

    @app.route("/api/screenshot")
    def api_screenshot():
        try:
            info, hwnd, _ = _window_from_args()
            shot = observer.get_screenshot(hwnd)
            if shot is None:
                return jsonify({"error": "Screenshot capture failed"}), 500
            return jsonify({
                "window":   info.title if info else "(full screen)",
                "format":   "png",
                "encoding": "base64",
                "data":     base64.b64encode(shot).decode(),
            })
        except Exception as e:
            print(f"[web_inspector:/api/screenshot] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/full_screenshot ──────────────────────────────────────────────────

    @app.route("/api/full_screenshot")
    def api_full_screenshot():
        """All-monitor screenshot + optional ASCII sketch in one call."""
        try:
            info, hwnd, _ = _window_from_args()
            # Always capture the full virtual desktop (all monitors combined)
            shot = observer.get_full_display_screenshot()
            if shot is None:
                return jsonify({"error": "Screenshot capture failed"}), 500

            sketch: Optional[str] = None
            tree = observer.get_element_tree(hwnd) if hwnd is not None else None
            if tree is not None:
                gw  = request.args.get("grid_width",  type=int)
                gh  = request.args.get("grid_height", type=int)
                ref = info.bounds if info else observer.get_screen_bounds()
                # Crop the full-display PNG to the window's bounds so that OCR
                # word coordinates (which are window-relative in ascii_renderer)
                # align correctly with the sketch grid.
                ocr_bytes = shot
                if info is not None:
                    try:
                        import io as _io2
                        from PIL import Image as _Image2
                        full_img = _Image2.open(_io2.BytesIO(shot))
                        screen_b = observer.get_screen_bounds()
                        crop_box = (
                            info.bounds.x - screen_b.x,
                            info.bounds.y - screen_b.y,
                            info.bounds.right - screen_b.x,
                            info.bounds.bottom - screen_b.y,
                        )
                        crop_box = (
                            max(0, crop_box[0]),
                            max(0, crop_box[1]),
                            min(full_img.width,  crop_box[2]),
                            min(full_img.height, crop_box[3]),
                        )
                        buf2 = _io2.BytesIO()
                        full_img.crop(crop_box).save(buf2, format="PNG")
                        ocr_bytes = buf2.getvalue()
                    except Exception as e:
                        logger.debug(
                            f"[/api/full_screenshot] window crop for OCR "
                            f"overlay failed; using full image: {e}")
                sketch = renderer.render(
                    root             = tree,
                    screen_bounds    = ref,
                    grid_width       = gw,
                    grid_height      = gh,
                    screenshot_bytes = ocr_bytes,
                )

            try:
                import io as _io
                from PIL import Image as _Image
                _img = _Image.open(_io.BytesIO(shot))
                img_w, img_h = _img.size
            except Exception:
                img_w = img_h = None

            return jsonify({
                "window":          info.title if info else "(full screen)",
                "screenshot_scope": "full_display",
                "format":          "png",
                "encoding":        "base64",
                "width":           img_w,
                "height":          img_h,
                "data":            base64.b64encode(shot).decode(),
                "sketch":          sketch,
            })
        except Exception as e:
            print(f"[web_inspector:/api/full_screenshot] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/visible_areas ────────────────────────────────────────────────────

    @app.route("/api/visible_areas")
    def api_visible_areas():
        """Visible (non-occluded, on-screen) bounding boxes for a window."""
        try:
            info, hwnd, windows = _window_from_args()
            if hwnd is None:
                return jsonify({"error": "window_uid or window_index is required"}), 400
            areas = observer.get_visible_areas(hwnd, windows)
            return jsonify({
                "window":          info.title if info else "(unknown)",
                "visible_regions": areas,
            })
        except Exception as e:
            print(f"[web_inspector:/api/visible_areas] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/bring_to_foreground ──────────────────────────────────────────────

    @app.route("/api/bring_to_foreground")
    def api_bring_to_foreground():
        """Click the title bar of a window to bring it to the foreground."""
        try:
            info, hwnd, windows = _window_from_args()
            if hwnd is None:
                return jsonify({"success": False,
                                "error": "window_uid or window_index is required"}), 400
            result = observer.bring_to_foreground(hwnd, windows)
            result["window"]     = info.title if info else "(unknown)"
            result["window_uid"] = info.window_uid if info else None
            return jsonify(result)
        except Exception as e:
            print(f"[web_inspector:/api/bring_to_foreground] {e}")
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500

    # ── /api/action ───────────────────────────────────────────────────────────

    @app.route("/api/action", methods=["POST"])
    def api_action():
        body = request.get_json(force=True) or {}
        action = body.get("action", "")
        if action == "click_at":
            return _tool_response("click_at", {
                "x": body.get("x", 0), "y": body.get("y", 0),
                "button": body.get("button", "left"),
                "double": body.get("double", False),
            })
        if action == "type":
            return _tool_response("type_text", {"text": body.get("value", "")})
        if action == "key":
            return _tool_response("press_key", {"keys": body.get("value", "")})
        if action == "scroll":
            return _tool_response("scroll", body)
        return jsonify({"success": False, "ok": False,
                        "error": f"Unknown action: {action}"}), 400

    # ── P1: identity, capabilities, element-targeted actions ─────────────────

    @app.route("/api/capabilities")
    def api_capabilities():
        return _tool_response("get_capabilities", {})

    @app.route("/api/monitors")
    def api_monitors():
        return _tool_response("get_monitors", {})

    @app.route("/api/find_element")
    def api_find_element():
        return _tool_response("find_element", _merge_query())

    @app.route("/api/element/click", methods=["POST"])
    def api_element_click():
        return _tool_response("click_element", request.get_json(force=True) or {})

    @app.route("/api/element/focus", methods=["POST"])
    def api_element_focus():
        return _tool_response("focus_element", request.get_json(force=True) or {})

    @app.route("/api/element/set_value", methods=["POST"])
    def api_element_set_value():
        return _tool_response("set_value", request.get_json(force=True) or {})

    @app.route("/api/element/invoke", methods=["POST"])
    def api_element_invoke():
        return _tool_response("invoke_element", request.get_json(force=True) or {})

    @app.route("/api/element/select", methods=["POST"])
    def api_element_select():
        return _tool_response("select_option", request.get_json(force=True) or {})

    # ── P2: observe-with-diff, snapshots, wait_for ──────────────────────────

    @app.route("/api/observe")
    def api_observe():
        args = _merge_query()
        if "depth" in args:
            try:
                args["depth"] = int(args["depth"])
            except (TypeError, ValueError):
                args.pop("depth", None)
        if "changed_only" in args:
            args["changed_only"] = str(args["changed_only"]).lower() in (
                "1", "true", "yes")
        return _tool_response("observe_window", args)

    @app.route("/api/snapshot", methods=["POST"])
    def api_snapshot():
        return _tool_response("snapshot", request.get_json(silent=True) or {})

    @app.route("/api/snapshot/<sid>")
    def api_snapshot_get(sid: str):
        return _tool_response("snapshot_get", {"snapshot_id": sid})

    @app.route("/api/snapshot/diff", methods=["POST"])
    def api_snapshot_diff():
        return _tool_response("snapshot_diff", request.get_json(force=True) or {})

    @app.route("/api/snapshot/<sid>", methods=["DELETE"])
    def api_snapshot_drop(sid: str):
        return _tool_response("snapshot_drop", {"snapshot_id": sid})

    @app.route("/api/wait_for", methods=["POST"])
    def api_wait_for():
        return _tool_response("wait_for", request.get_json(force=True) or {})

    @app.route("/api/wait_idle", methods=["POST"])
    def api_wait_idle():
        return _tool_response("wait_idle", request.get_json(force=True) or {})

    @app.route("/api/element/click_and_observe", methods=["POST"])
    def api_element_click_observe():
        return _tool_response("click_element_and_observe",
                               request.get_json(force=True) or {})

    @app.route("/api/type_and_observe", methods=["POST"])
    def api_type_observe():
        return _tool_response("type_and_observe", request.get_json(force=True) or {})

    @app.route("/api/key_and_observe", methods=["POST"])
    def api_key_observe():
        return _tool_response("press_key_and_observe", request.get_json(force=True) or {})

    # ── P3: filtering, cropping, region OCR, budgeted description ───────────

    @app.route("/api/screenshot/cropped")
    def api_screenshot_cropped():
        return _tool_response("get_screenshot_cropped", _merge_query())

    @app.route("/api/ocr")
    def api_ocr():
        return _tool_response("get_ocr", _merge_query())

    # ── P4: tracing, replay, scenarios, oracles ─────────────────────────────

    @app.route("/api/trace/start", methods=["POST"])
    def api_trace_start():
        return _tool_response("trace_start", request.get_json(silent=True) or {})

    @app.route("/api/trace/stop", methods=["POST"])
    def api_trace_stop():
        return _tool_response("trace_stop", {})

    @app.route("/api/trace/status")
    def api_trace_status():
        return _tool_response("trace_status", {})

    @app.route("/api/replay/start", methods=["POST"])
    def api_replay_start():
        return _tool_response("replay_start", request.get_json(force=True) or {})

    @app.route("/api/replay/step", methods=["POST"])
    def api_replay_step():
        return _tool_response("replay_step", request.get_json(force=True) or {})

    @app.route("/api/replay/status", methods=["POST"])
    def api_replay_status():
        return _tool_response("replay_status", request.get_json(force=True) or {})

    @app.route("/api/replay/stop", methods=["POST"])
    def api_replay_stop():
        return _tool_response("replay_stop", request.get_json(force=True) or {})

    @app.route("/api/scenario/load", methods=["POST"])
    def api_scenario_load():
        return _tool_response("load_scenario", request.get_json(force=True) or {})

    @app.route("/api/assert_state", methods=["POST"])
    def api_assert_state():
        return _tool_response("assert_state", request.get_json(force=True) or {})

    # ── P5: budgets / redaction status / propose ────────────────────────────

    @app.route("/api/budget_status")
    def api_budget_status():
        return _tool_response("get_budget_status", {})

    @app.route("/api/redaction_status")
    def api_redaction_status():
        return _tool_response("get_redaction_status", {})

    @app.route("/api/propose_action", methods=["POST"])
    def api_propose():
        return _tool_response("propose_action", request.get_json(force=True) or {})

    # ── P6: extra input verbs ────────────────────────────────────────────────

    @app.route("/api/hover", methods=["POST"])
    def api_hover():
        body = request.get_json(force=True) or {}
        if "x" in body and "y" in body and not body.get("selector") and not body.get("element_id"):
            return _tool_response("hover_at", body)
        return _tool_response("hover_element", body)

    @app.route("/api/element/right_click", methods=["POST"])
    def api_right_click():
        return _tool_response("right_click_element", request.get_json(force=True) or {})

    @app.route("/api/element/double_click", methods=["POST"])
    def api_double_click():
        return _tool_response("double_click_element", request.get_json(force=True) or {})

    @app.route("/api/drag", methods=["POST"])
    def api_drag():
        return _tool_response("drag", request.get_json(force=True) or {})

    @app.route("/api/element/key", methods=["POST"])
    def api_key_into():
        return _tool_response("key_into_element", request.get_json(force=True) or {})

    @app.route("/api/element/clear_text", methods=["POST"])
    def api_clear_text():
        return _tool_response("clear_text", request.get_json(force=True) or {})

    # ── Telemetry: metrics in Prometheus format ─────────────────────────────

    @app.route("/api/metrics")
    def api_metrics():
        from session import get_session
        s = get_session()
        tc = s.tree_cache.counters()
        lines = [
            "# HELP oso_step_count Total tool calls processed",
            "# TYPE oso_step_count counter",
            f"oso_step_count {s.steps.count}",
            "# HELP oso_uptime_seconds Process uptime",
            "# TYPE oso_uptime_seconds gauge",
            f"oso_uptime_seconds {int(s.steps.uptime_s)}",
            # ── Tree-cache effectiveness (P2) ─────────────────────────────
            "# HELP oso_tree_cache_hits_total Tree-cache lookups served "
            "from a fresh cached capture",
            "# TYPE oso_tree_cache_hits_total counter",
            f"oso_tree_cache_hits_total {tc['hits']}",
            "# HELP oso_tree_cache_misses_total Tree-cache lookups that "
            "required a fresh accessibility-tree walk",
            "# TYPE oso_tree_cache_misses_total counter",
            f"oso_tree_cache_misses_total {tc['misses']}",
            "# HELP oso_tree_cache_entries Windows currently cached",
            "# TYPE oso_tree_cache_entries gauge",
            f"oso_tree_cache_entries {tc['entries']}",
            # ── Capture-latency summary (P2) ──────────────────────────────
            "# HELP oso_tree_capture_ms Accessibility-tree capture latency "
            "summary (milliseconds)",
            "# TYPE oso_tree_capture_ms summary",
            f"oso_tree_capture_ms_sum {tc['capture_ms_total']}",
            f"oso_tree_capture_ms_count {tc['capture_count']}",
            "# HELP oso_tree_capture_ms_max Slowest tree capture this "
            "session (milliseconds)",
            "# TYPE oso_tree_capture_ms_max gauge",
            f"oso_tree_capture_ms_max {tc['capture_ms_max']}",
        ]
        if s.budgets is not None:
            st = s.budgets.status()
            lines += [
                "# TYPE oso_actions_used counter",
                f"oso_actions_used {st['actions']['used']}",
                "# TYPE oso_screenshots_used counter",
                f"oso_screenshots_used {st['screenshots']['used']}",
            ]
        if s.active_trace is not None:
            lines.append("oso_active_trace 1")
        else:
            lines.append("oso_active_trace 0")
        body = "\n".join(lines) + "\n"
        return body, 200, {"Content-Type": "text/plain; version=0.0.4"}

    # ── Generic tool console ────────────────────────────────────────────────

    @app.route("/api/tools")
    def api_tools_list():
        return jsonify({"ok": True,
                        "tools": sorted(_tools.REGISTRY.keys())})

    @app.route("/api/tool/<name>", methods=["GET", "POST"])
    def api_tool_run(name: str):
        if request.method == "POST":
            args = request.get_json(silent=True) or {}
        else:
            args = _merge_query()
        return _tool_response(name, args)

    # /api/healthz must stay cheap enough to poll: the OCR diagnostic
    # spawns a `tesseract --version` subprocess, so it is computed once
    # per process and reused (binary availability doesn't change mid-run).
    _healthz_ocr_cache: dict = {}

    @app.route("/api/healthz")
    def api_healthz():
        from session import get_session
        s = get_session()
        out = {
            "ok": True,
            "uptime_s": int(s.steps.uptime_s),
            "step_count": s.steps.count,
            "adapter": type(observer._adapter).__name__,
            "version": (config.get("mcp", {}) or {}).get("version", "0.2.0"),
            # P2 telemetry: cache effectiveness + capture-latency summary.
            "tree_cache": s.tree_cache.counters(),
        }
        # Surface common misconfigurations.
        try:
            from main import config_load_status
            out.update(config_load_status())
        except Exception as e:
            logger.debug(f"healthz: config_load_status unavailable: {e}")
        try:
            if "ocr" not in _healthz_ocr_cache:
                from ocr_util import diagnose as _ocr_diag
                _healthz_ocr_cache["ocr"] = _ocr_diag(config)
            out["ocr"] = _healthz_ocr_cache["ocr"]
        except Exception as e:
            logger.debug(f"healthz: OCR diagnose failed: {e}")
        return jsonify(out)

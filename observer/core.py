"""
ScreenObserver — public interface; selects a platform adapter
and wires the session tree cache.

Split out of observer.py (P3); behavior is unchanged.
"""

import logging
import io
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from observer.activation import ActivationMixin
from observer.adapters.linux import LinuxAdapter
from observer.adapters.macos import MacOSAdapter
from observer.adapters.mock import MockAdapter
from observer.adapters.windows import WindowsAdapter
from observer.adapters.wsl import WSLAdapter
from observer.models import (
    UIElement, WindowInfo, WindowResolution, find_element_by_path,
    prune_tree_depth,
)
from observer.occlusion import OcclusionMixin
from observer.platform_info import EFFECTIVE_PLATFORM

logger = logging.getLogger(__name__)


class ScreenObserver(ActivationMixin, OcclusionMixin):
    """
    Platform-aware screen observer.  All consumers should program against
    this class rather than the platform adapters directly.
    """

    def __init__(self, config: dict):
        self.config = config
        self._adapter = self._select_adapter()
        # Try to upgrade stub adapters to real AX implementations.
        try:
            if isinstance(self._adapter, MacOSAdapter):
                import mac_adapter
                if mac_adapter.install_into(self):
                    logger.info("[ScreenObserver] mac_adapter installed (pyobjc)")
            elif isinstance(self._adapter, LinuxAdapter):
                import linux_adapter
                if linux_adapter.install_into(self):
                    logger.info("[ScreenObserver] linux_adapter installed (pyatspi)")
        except Exception:
            logger.exception("real adapter upgrade failed")

    def _select_adapter(self):
        if self.config.get("mock", False):
            logger.info("[ScreenObserver] Using MockAdapter")
            return MockAdapter()

        target = self.config.get("platform", "auto")
        # EFFECTIVE_PLATFORM is "WSL" when running inside WSL, otherwise same
        # as platform.system().  Explicit config overrides auto-detection.
        sys_plat = EFFECTIVE_PLATFORM if target == "auto" else target

        adapters = {
            "Windows": WindowsAdapter,
            "Darwin":  MacOSAdapter,
            "Linux":   LinuxAdapter,
            "WSL":     WSLAdapter,
        }

        cls = adapters.get(sys_plat)
        if cls is None:
            logger.warning("[ScreenObserver] Unknown platform '%s'; using MockAdapter", sys_plat)
            return MockAdapter()

        try:
            return cls(self.config)
        except Exception as e:
            print(f"[ScreenObserver:_select_adapter] Platform adapter failed: {e}; falling back to Mock")
            traceback.print_exc()
            return MockAdapter()

    @property
    def is_mock(self) -> bool:
        return isinstance(self._adapter, MockAdapter)

    def list_windows(self) -> List[WindowInfo]:
        return self._adapter.list_windows()

    def get_element_tree(self, hwnd=None, window_uid: Optional[str] = None,
                         use_cache: bool = True) -> Optional[UIElement]:
        """Return the accessibility tree for a window.

        When *window_uid* is supplied the per-window tree cache is consulted:
        a fresh cached capture (within ``tree.cache_ttl_s``) is returned
        without walking the adapter; a miss walks and stores.  Pass
        ``use_cache=False`` to force a fresh walk (post-action re-reads);
        the fresh capture still refreshes the cache.  Calls without a
        *window_uid* always walk and are never cached.
        """
        tree, _meta = self.get_element_tree_with_meta(
            hwnd, window_uid=window_uid, use_cache=use_cache)
        return tree

    def get_element_tree_with_meta(
            self, hwnd=None, *, window_uid: Optional[str] = None,
            use_cache: bool = True,
    ) -> Tuple[Optional[UIElement], Dict[str, Any]]:
        """get_element_tree plus capture metadata.

        Returns (tree, meta) where meta is
        ``{"cache": "hit"|"miss"|"bypass", "capture_ms": int,
        "node_count": int}``.
        """
        tree_cfg = self.config.get("tree", {}) or {}
        ttl = float(tree_cfg.get("cache_ttl_s", 2.0))
        cache = self._tree_cache()

        if use_cache and window_uid and cache is not None:
            entry = cache.get(window_uid, ttl_s=ttl)
            if entry is not None:
                return entry.tree, {
                    "cache": "hit",
                    "capture_ms": 0,
                    "node_count": entry.node_count,
                }

        started = time.time()
        tree = self._adapter.get_element_tree(hwnd)
        capture_ms = int((time.time() - started) * 1000)
        node_count = len(tree.flat_list()) if tree is not None else 0

        if tree is not None and window_uid and cache is not None:
            from hashing import tree_hash as _tree_hash
            named = sum(
                1 for e in tree.flat_list()[1:] if (e.name or "").strip()
            )
            cache.put(
                window_uid,
                tree=tree,
                serialized=tree.to_dict(),
                tree_hash=_tree_hash(tree),
                max_depth=int(tree_cfg.get("max_depth", 8)),
                capture_ms=capture_ms,
                node_count=node_count,
                named_node_count=named,
            )

        return tree, {
            "cache": "bypass" if not use_cache else "miss",
            "capture_ms": capture_ms,
            "node_count": node_count,
        }

    @staticmethod
    def _tree_cache():
        """The session-scoped TreeCache (lazy import avoids cycles)."""
        try:
            from session import get_session
            return get_session().tree_cache
        except Exception:
            return None

    def get_element_subtree(self, hwnd=None, element_path: str = "root",
                            max_depth: Optional[int] = None,
                            window_uid: Optional[str] = None,
                            use_cache: bool = True) -> Optional[UIElement]:
        """Return only the subtree rooted at *element_path* ('root.3.2').

        Resolution order:
          1. a fresh cached full capture (no walk at all),
          2. the adapter's native get_element_subtree (walks just the branch),
          3. full walk + extraction.
        The result is depth-limited to *max_depth* levels below the subtree
        root and safe to mutate (cache extraction returns copies)."""
        tree_cfg = self.config.get("tree", {}) or {}
        if max_depth is None:
            max_depth = int(tree_cfg.get("max_depth", 8))

        # 1. Serve from a fresh cached capture.
        cache = self._tree_cache()
        if use_cache and window_uid and cache is not None:
            entry = cache.get(window_uid,
                              ttl_s=float(tree_cfg.get("cache_ttl_s", 2.0)))
            if entry is not None:
                sub = find_element_by_path(entry.tree, element_path)
                if sub is not None:
                    return prune_tree_depth(sub, max_depth)

        # 2. Adapter-native scoped walk.
        native = getattr(self._adapter, "get_element_subtree", None)
        if native is not None:
            try:
                sub = native(hwnd, element_path, max_depth)
                if sub is not None:
                    return sub
            except Exception:
                logger.exception("[ScreenObserver:get_element_subtree] "
                                 "adapter subtree walk failed; falling back")

        # 3. Full walk + extraction.
        tree = self.get_element_tree(hwnd, window_uid=window_uid,
                                     use_cache=use_cache)
        sub = find_element_by_path(tree, element_path)
        return prune_tree_depth(sub, max_depth)

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        return self._adapter.get_screenshot(hwnd)

    def get_full_display_screenshot(self) -> Optional[bytes]:
        """Capture the entire virtual desktop (all monitors combined) as a PNG."""
        try:
            import mss
            from PIL import Image
            with mss.MSS() as sct:
                raw = sct.grab(sct.monitors[0])   # 0 = union of all monitors
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return buf.getvalue()
        except Exception as e:
            logger.warning(f"[ScreenObserver:get_full_display_screenshot] {e}; falling back")
            return self._adapter.get_screenshot()

    def perform_action(self, action: str, element_id: Optional[str] = None,
                       value: Any = None, hwnd=None) -> Dict:
        return self._adapter.perform_action(action, element_id, value, hwnd)

    def window_by_index(self, windows: List[WindowInfo],
                        index: Optional[int]) -> Optional[WindowInfo]:
        """Convenience: return a WindowInfo by list index, or None."""
        if index is None or not windows:
            return None
        if 0 <= index < len(windows):
            return windows[index]
        return None

    def window_by_uid(self, windows: List[WindowInfo],
                      uid: Optional[str]) -> Optional[WindowInfo]:
        """Resolve a window by stable uid; returns None if not found."""
        if not uid or not windows:
            return None
        for w in windows:
            if w.window_uid == uid:
                return w
        return None

    def resolve_window(self, windows: List[WindowInfo],
                       window_uid: Optional[str],
                       window_index: Optional[int],
                       window_title: Optional[str] = None) -> "WindowResolution":
        """Resolve a window by uid (preferred), index, or title substring."""
        if window_uid:
            info = self.window_by_uid(windows, window_uid)
            warning = ("both window_index and window_uid given; window_uid used"
                       if window_index is not None else None)
            return WindowResolution(info=info, warning=warning,
                                    used_uid=True, requested_uid=window_uid)
        if window_index is not None:
            info = self.window_by_index(windows, window_index)
            resolved_uid = info.window_uid if info else None
            return WindowResolution(info=info, warning=None,
                                    used_uid=bool(resolved_uid),
                                    requested_uid=resolved_uid)
        if window_title:
            needle = window_title.lower()
            info = next((w for w in windows if needle in (w.title or "").lower()), None)
            resolved_uid = info.window_uid if info else None
            return WindowResolution(info=info, warning=None,
                                    used_uid=bool(resolved_uid),
                                    requested_uid=resolved_uid)
        return WindowResolution(info=None, warning=None,
                                used_uid=False, requested_uid=None)

    # ── Monitors / DPI (design doc §6.3) ──────────────────────────────────────

    def get_monitors(self) -> List[Dict[str, Any]]:
        """Return per-monitor metadata via mss."""
        try:
            import mss
            with mss.MSS() as sct:
                mons = sct.monitors  # [0] is union; [1..] are individual
                out: List[Dict[str, Any]] = []
                for i, m in enumerate(mons[1:]):
                    out.append({
                        "index": i,
                        "primary": (i == 0),
                        "bounds":  {"x": m["left"], "y": m["top"],
                                    "width": m["width"], "height": m["height"]},
                        "scale_factor": 1.0,
                        "logical_bounds":  {"x": m["left"], "y": m["top"],
                                            "width": m["width"], "height": m["height"]},
                        "physical_bounds": {"x": m["left"], "y": m["top"],
                                            "width": m["width"], "height": m["height"]},
                    })
                return out
        except Exception:
            return []

    # ── Capability discovery (design doc §6.4) ────────────────────────────────

    def get_capabilities(self) -> Dict[str, Any]:
        adapter_name = type(self._adapter).__name__
        is_windows = adapter_name == "WindowsAdapter"
        is_macos   = adapter_name == "MacOSAdapter"
        is_wsl     = adapter_name == "WSLAdapter"
        is_linux   = adapter_name in ("LinuxAdapter", "WSLAdapter")
        is_mock    = adapter_name == "MockAdapter"

        # Probe optional libs.
        def _has(mod: str) -> bool:
            try:
                __import__(mod)
                return True
            except Exception:
                return False

        if is_macos:
            ax_tree = _has("AppKit") or _has("ApplicationServices") or _has("Cocoa")
        elif is_linux:
            ax_tree = _has("pyatspi")
        else:
            ax_tree = is_windows or is_mock

        return {
            "ok": True,
            "platform": EFFECTIVE_PLATFORM,
            "adapter": adapter_name,
            "version": (self.config.get("mcp", {}) or {}).get("version", "0.2.0"),
            "protocol_version": 2,
            "supports": {
                "accessibility_tree":  bool(ax_tree),
                "uia_invoke":          is_windows,
                "occlusion_detection": is_windows or is_mock or _has("Quartz") or _has("Xlib"),
                "drag":                True,
                "ocr":                 _has("pytesseract"),
                "vlm":                 bool((self.config.get("vlm") or {}).get("enabled")
                                            and (self.config.get("vlm") or {}).get("model")),
                "redaction":           True,
                "scenarios":           is_mock,
                "tracing":             True,
                "replay":              True,
                "image_blur":          _has("PIL"),
                "wsl_powershell":      is_wsl,
                # Action capabilities always present via REST + MCP.
                "bring_to_foreground": True,
                "element_targeting":   bool(ax_tree),  # click/focus/invoke/set_value via element_id
                "observe_with_diff":   True,            # /api/observe returns diff token
            },
            "config": {
                "tree_max_depth": (self.config.get("tree", {}) or {}).get("max_depth", 8),
                "tree_default_depth": (self.config.get("tree", {}) or {}).get("default_depth", 5),
                "ascii_grid": {
                    "width":  (self.config.get("ascii_sketch", {}) or {}).get("grid_width",  110),
                    "height": (self.config.get("ascii_sketch", {}) or {}).get("grid_height",  38),
                },
            },
            # Per-window last-capture statistics (node_count,
            # named_node_count, capture_ms, captured_at) so agents can spot
            # accessibility-dark windows without another walk.
            "tree_stats": self._last_capture_stats(),
        }

    def _last_capture_stats(self) -> Dict[str, Dict[str, Any]]:
        cache = self._tree_cache()
        try:
            return cache.stats() if cache is not None else {}
        except Exception:
            return {}

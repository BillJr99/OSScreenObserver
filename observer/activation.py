"""
Window activation: bring_to_foreground, platform API
activation strategies and title-bar click targeting (mixin consumed by
ScreenObserver).

Split out of observer.py (P3); behavior is unchanged.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from observer.models import UIElement, WindowInfo
from observer.platform_info import PLATFORM


class ActivationMixin:
    """Foreground-activation methods of ScreenObserver."""

    # Provided by the concrete ScreenObserver / OcclusionMixin.
    _adapter: Any

    if TYPE_CHECKING:  # pragma: no cover — typing-only method stubs
        def get_element_tree(self, hwnd: Any = None,
                             window_uid: Optional[str] = None,
                             use_cache: bool = True
                             ) -> Optional[UIElement]: ...

        def get_visible_areas(self, target_hwnd: Any,
                              all_windows: List[WindowInfo]
                              ) -> List[Dict]: ...

        def perform_action(self, action: str,
                           element_id: Optional[str] = None,
                           value: Any = None, hwnd: Any = None
                           ) -> Dict: ...
    def bring_to_foreground(self, target_hwnd: Any,
                            all_windows: List[WindowInfo]) -> Dict:
        """
        Bring a window to the foreground.

        Strategy (in order)
        -------------------
        1. Platform API — SetForegroundWindow / NSRunningApplication.activate /
           wmctrl.  Does not click, so it cannot accidentally maximise the window
           and works even when the window is fully occluded.
        2. Click on title bar — fallback when the API call is unavailable or
           blocked (e.g. Windows foreground-lock policy).  Requires at least one
           visible pixel; returns an error only when this is also impossible.
        """
        target = next((w for w in all_windows if w.handle == target_hwnd), None)
        if target is None:
            return {"success": False, "error": "Window not found"}

        # ── 1. Try platform API first ─────────────────────────────────────────
        api_ok, api_note = self._activate_via_api(target_hwnd, target)
        if api_ok:
            return {"success": True, "action": "activate_api",
                    "window": target.title, "note": api_note}

        # ── 2. Fall back to clicking the title bar ────────────────────────────
        regions = self.get_visible_areas(target_hwnd, all_windows)
        if not regions:
            return {
                "success": False,
                "error": (
                    "Window has no visible area (fully occluded or off-screen) "
                    f"and platform API also failed: {api_note}"
                ),
            }

        best = min(regions, key=lambda r: (r["y"], -r["width"]))
        click_x, click_y = self._title_bar_click_point(target_hwnd, target, best)
        result = self.perform_action("click_at",
                                     value={"x": click_x, "y": click_y,
                                            "button": "left", "double": False})
        result["clicked_x"] = click_x
        result["clicked_y"] = click_y
        result.setdefault("action", "click_title_bar")
        result["api_note"] = api_note
        return result

    def _activate_via_api(self, hwnd: Any,
                          info: "WindowInfo") -> tuple:
        """
        Attempt to raise *hwnd* using the platform's native window-focus API.

        Returns (success: bool, note: str).  Never raises — all errors are
        caught and returned as (False, reason).
        """
        if PLATFORM == "Windows":
            return self._activate_windows(hwnd)
        if PLATFORM == "Darwin":
            return self._activate_macos(info)
        if PLATFORM == "Linux":
            # WSL: try X11 tools (wmctrl/xdotool) when DISPLAY is set; they
            # will gracefully fail and return (False, reason) when absent.
            return self._activate_linux(hwnd)
        return False, f"Platform {PLATFORM!r} has no API activate implementation"

    def _activate_windows(self, hwnd: int) -> tuple:
        try:
            import ctypes
            import ctypes.wintypes
            user32   = ctypes.windll.user32      # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32    # type: ignore[attr-defined]

            # Restore if minimised (IsIconic) or hidden.
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE

            def _is_foreground() -> bool:
                return user32.GetForegroundWindow() == hwnd

            if _is_foreground():
                return True, "already foreground"

            # ── Attempt 1: AttachThreadInput trick ────────────────────────────
            fg_hwnd  = user32.GetForegroundWindow()
            fg_tid   = user32.GetWindowThreadProcessId(fg_hwnd, None)
            this_tid = kernel32.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != this_tid:
                attached = bool(user32.AttachThreadInput(this_tid, fg_tid, True))
            try:
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(this_tid, fg_tid, False)

            if _is_foreground():
                return True, "SetForegroundWindow (AttachThreadInput)"

            # ── Attempt 2: synthesise a keypress to acquire foreground lock ───
            # Windows grants foreground rights to processes that just received
            # user input.  A zero-vkey keybd_event satisfies that requirement.
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(0, 0, 0, 0)
            user32.keybd_event(0, 0, KEYEVENTF_KEYUP, 0)
            user32.SetForegroundWindow(hwnd)

            if _is_foreground():
                return True, "SetForegroundWindow (keybd_event unlock)"

            # ── Attempt 3: BringWindowToTop + SetForegroundWindow ─────────────
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

            if _is_foreground():
                return True, "BringWindowToTop + SetForegroundWindow"

            return False, "SetForegroundWindow failed after all attempts"
        except Exception as e:
            return False, f"Windows API error: {e}"

    def _activate_macos(self, info: "WindowInfo") -> tuple:
        try:
            import AppKit
            ws   = AppKit.NSWorkspace.sharedWorkspace()
            apps = ws.runningApplications()
            pid  = info.pid
            for app in apps:
                if int(app.processIdentifier()) == pid:
                    NSApplicationActivateIgnoringOtherApps = 1 << 1
                    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                    return True, f"NSRunningApplication.activate pid={pid}"
            return False, f"No running application found for pid={pid}"
        except Exception as e:
            return False, f"macOS API error: {e}"

    def _activate_linux(self, hwnd: int) -> tuple:
        import subprocess
        # Try wmctrl first (X11; commonly available).
        try:
            r = subprocess.run(
                ["wmctrl", "-ia", hex(hwnd)],
                capture_output=True, timeout=3,
            )
            if r.returncode == 0:
                return True, f"wmctrl -ia {hex(hwnd)} succeeded"
        except FileNotFoundError:
            pass
        except Exception as e:
            return False, f"wmctrl error: {e}"
        # Try xdotool as a second option.
        try:
            r = subprocess.run(
                ["xdotool", "windowactivate", "--sync", str(hwnd)],
                capture_output=True, timeout=3,
            )
            if r.returncode == 0:
                return True, f"xdotool windowactivate {hwnd} succeeded"
            return False, f"xdotool returned {r.returncode}: {r.stderr.decode().strip()}"
        except FileNotFoundError:
            return False, "neither wmctrl nor xdotool found"
        except Exception as e:
            return False, f"xdotool error: {e}"

    # ── Title-bar targeting (used by bring_to_foreground) ────────────────────

    # Reserve these pixel margins for native window-control widgets.
    _LEFT_CONTROL_MARGIN  = 90    # macOS traffic-lights
    _RIGHT_CONTROL_MARGIN = 160   # Windows / Linux minimize/maximize/close
    _MIN_SAFE_WIDTH       = 260   # below this, fall back to centre

    _TITLEBAR_ROLES = {"TitleBar", "AXTitleBar", "title bar", "Title bar",
                       "AXWindow"}

    def _title_bar_click_point(self, target_hwnd: Any,
                                window: Optional["WindowInfo"],
                                region: Dict) -> tuple:
        """Return (x, y) for a safe click inside the title bar of *region*."""
        # 1. Try the accessibility tree.
        try:
            tree = self.get_element_tree(target_hwnd)
        except Exception:
            tree = None
        bar = self._find_title_bar(tree, window.title if window else "") if tree else None
        if bar is not None and bar.bounds and bar.bounds.width > 0:
            tb = bar.bounds
            # Intersect with the visible region so we don't click off-screen.
            x1 = max(region["x"], tb.x)
            y1 = max(region["y"], tb.y)
            x2 = min(region["x"] + region["width"],  tb.right)
            y2 = min(region["y"] + region["height"], tb.bottom)
            if x2 > x1 and y2 > y1:
                # Click 1/4 from the left of the title bar's visible width,
                # but skip the leftmost control margin.
                safe_left  = x1 + min(self._LEFT_CONTROL_MARGIN,  (x2 - x1) // 4)
                safe_right = x2 - min(self._RIGHT_CONTROL_MARGIN, (x2 - x1) // 4)
                if safe_right <= safe_left:
                    cx = (x1 + x2) // 2
                else:
                    quarter = x1 + (x2 - x1) // 4
                    cx = max(safe_left, min(safe_right, quarter))
                cy = y1 + max(1, (y2 - y1) // 2)
                return cx, cy

        # 2. Fall back to the visible region's top strip.
        rx = region["x"]
        rw = region["width"]
        ry = region["y"]
        rh = region["height"]
        if rw < self._MIN_SAFE_WIDTH:
            cx = rx + rw // 2
        else:
            safe_left  = rx + self._LEFT_CONTROL_MARGIN
            safe_right = rx + rw - self._RIGHT_CONTROL_MARGIN
            candidate  = rx + rw // 4
            if safe_right <= safe_left:
                cx = rx + rw // 2
            else:
                cx = max(safe_left, min(safe_right, candidate))
        title_bar_offset = min(12, max(1, (rh - 1) // 2))
        cy = ry + title_bar_offset
        # Clamp inside the region.
        cx = max(rx, min(rx + rw - 1, cx))
        cy = max(ry, min(ry + rh - 1, cy))
        return cx, cy

    def _find_title_bar(self, root: Optional["UIElement"],
                         window_title: str) -> Optional["UIElement"]:
        """Locate a title-bar-like element by role or by matching name.

        Returns None for the root window itself (clicking the whole window's
        center is precisely what we are trying to avoid).
        """
        if root is None:
            return None
        from collections import deque
        queue = deque([(root, 0)])
        while queue:
            elem, depth = queue.popleft()
            # Never return the root — its centre is the window body, not the
            # title bar.
            if depth > 0:
                if (elem.role or "") in self._TITLEBAR_ROLES:
                    return elem
                if window_title and (elem.name or "").strip() == window_title.strip():
                    # Must be near the top edge AND short — a real title bar.
                    if (elem.bounds and root.bounds
                            and (elem.bounds.y - root.bounds.y) < 40
                            and elem.bounds.height <= 48):
                        return elem
            if depth > 3:
                continue
            for c in elem.children:
                queue.append((c, depth + 1))
        return None

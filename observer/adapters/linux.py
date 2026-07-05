"""
Linux adapter (screenshot works; AT-SPI tree is a stub
pending pyatspi work — upgraded at runtime by top-level linux_adapter.py).

Split out of observer.py (P3); behavior is unchanged.
"""

import io
import logging
import traceback
from typing import Any, Dict, List, Optional

from observer.models import Bounds, UIElement, WindowInfo

logger = logging.getLogger(__name__)


class LinuxAdapter:
    def __init__(self, config: dict):
        self.config = config
        logger.info("[LinuxAdapter:__init__] Linux adapter loaded (AT-SPI tree is stub)")

    def get_windows_above_bounds(self, hwnd) -> List[Bounds]:
        return []  # Z-order unavailable without Xlib/wnck

    def list_windows(self) -> List[WindowInfo]:
        # Prefer wmctrl when present; otherwise fall back to Xlib so the
        # adapter still works on systems without the wmctrl binary installed.
        import subprocess
        try:
            r = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=5)
        except FileNotFoundError:
            logger.info("[LinuxAdapter:list_windows] wmctrl not installed; trying Xlib fallback")
            return self._list_windows_xlib()
        except Exception as e:
            logger.warning("[LinuxAdapter:list_windows] wmctrl failed: %s; trying Xlib fallback", e)
            return self._list_windows_xlib()
        try:
            windows = []
            for line in r.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split(None, 8)
                if len(parts) < 8:
                    continue
                hwnd = int(parts[0], 16)
                x, y, w, h = int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
                title = parts[8] if len(parts) > 8 else "(no title)"
                windows.append(WindowInfo(hwnd, title, title, 0,
                                          Bounds(x, y, w, h), False,
                                          window_uid=f"x11:{hwnd:x}"))
            return windows
        except Exception as e:
            print(f"[LinuxAdapter:list_windows] {e}")
            traceback.print_exc()
            return []

    def _list_windows_xlib(self) -> List[WindowInfo]:
        try:
            from Xlib import display, X  # noqa: F401
            from Xlib.error import XError
        except ImportError:
            logger.warning(
                "[LinuxAdapter:list_windows] neither 'wmctrl' nor python-xlib "
                "is available; install one (e.g. `apt install wmctrl` or "
                "`pip install python-xlib`) to enumerate windows"
            )
            return []
        try:
            d = display.Display()
            root = d.screen().root
            NET_CLIENT_LIST = d.intern_atom("_NET_CLIENT_LIST")
            NET_WM_NAME = d.intern_atom("_NET_WM_NAME")
            UTF8_STRING = d.intern_atom("UTF8_STRING")
            prop = root.get_full_property(NET_CLIENT_LIST, X.AnyPropertyType)
            if prop is None:
                return []
            windows: List[WindowInfo] = []
            for wid in prop.value:
                try:
                    w = d.create_resource_object("window", wid)
                    geom = w.get_geometry()
                    coords = w.translate_coords(root, 0, 0)
                    x, y = -coords.x, -coords.y
                    title = ""
                    name_prop = w.get_full_property(NET_WM_NAME, UTF8_STRING)
                    if name_prop and name_prop.value:
                        title = name_prop.value.decode("utf-8", errors="replace")
                    else:
                        wm_name = w.get_wm_name()
                        if wm_name:
                            title = wm_name if isinstance(wm_name, str) else wm_name.decode("utf-8", errors="replace")
                    title = title or "(no title)"
                    windows.append(WindowInfo(int(wid), title, title, 0,
                                              Bounds(x, y, geom.width, geom.height), False,
                                              window_uid=f"x11:{int(wid):x}"))
                except XError:
                    continue
            return windows
        except Exception as e:
            print(f"[LinuxAdapter:_list_windows_xlib] {e}")
            traceback.print_exc()
            return []

    def get_element_tree(self, hwnd=None) -> Optional[UIElement]:
        logger.warning("[LinuxAdapter:get_element_tree] Full tree requires pyatspi; returning stub")
        return UIElement("root", "Linux Application (AT-SPI stub)", "Window",
                         bounds=Bounds(0, 0, 1920, 1080))

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        # mss needs a running X server (DISPLAY must be set).
        try:
            import mss
            from PIL import Image
            with mss.MSS() as sct:
                raw = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return buf.getvalue()
        except Exception as e:
            logger.debug("[LinuxAdapter:get_screenshot] mss failed: %s; trying scrot", e)
        # scrot can write PNG directly to stdout (-z suppresses notifications).
        try:
            import subprocess
            r = subprocess.run(["scrot", "-z", "-"], capture_output=True, timeout=10)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception as e:
            logger.debug("[LinuxAdapter:get_screenshot] scrot failed: %s", e)
        return None

    def perform_action(self, action: str, element_id=None,
                       value: Any = None, hwnd=None) -> Dict:
        try:
            import pyautogui
            if action == "type" and value:
                pyautogui.write(str(value), interval=0.02)
                return {"success": True}
            if action == "key" and value:
                pyautogui.hotkey(*str(value).lower().split("+"))
                return {"success": True}
            return {"success": False, "error": f"Unsupported: {action}"}
        except Exception as e:
            print(f"[LinuxAdapter:perform_action] {e}")
            traceback.print_exc()
            return {"success": False, "error": str(e)}

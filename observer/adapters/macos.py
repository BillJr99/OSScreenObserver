"""
macOS adapter (screenshot works; AX tree is a stub
pending pyobjc work — upgraded at runtime by top-level mac_adapter.py).

Split out of observer.py (P3); behavior is unchanged.
"""

import io
import logging
import traceback
from typing import Any, Dict, List, Optional

from observer.models import Bounds, UIElement, WindowInfo

logger = logging.getLogger(__name__)


class MacOSAdapter:
    def __init__(self, config: dict):
        self.config = config
        logger.info("[MacOSAdapter:__init__] macOS adapter loaded (AX tree is stub)")

    def get_windows_above_bounds(self, hwnd) -> List[Bounds]:
        return []  # Z-order unavailable without Quartz CGWindowList

    def list_windows(self) -> List[WindowInfo]:
        try:
            import subprocess
            script = ('tell application "System Events" to get name of every '
                      'process whose background only is false')
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=5)
            apps = [a.strip() for a in r.stdout.split(",") if a.strip()]
            return [WindowInfo(i, a, a, 0, Bounds(0, 0, 1920, 1080), i == 0,
                               window_uid=f"mac:{i}")
                    for i, a in enumerate(apps)]
        except Exception as e:
            print(f"[MacOSAdapter:list_windows] {e}")
            traceback.print_exc()
            return []

    def get_element_tree(self, hwnd=None) -> Optional[UIElement]:
        logger.warning("[MacOSAdapter:get_element_tree] Full AX tree requires pyobjc; returning stub")
        return UIElement("root", "macOS Application (AX tree stub)", "Window",
                         bounds=Bounds(0, 0, 1920, 1080))

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
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
            print(f"[MacOSAdapter:get_screenshot] {e}")
            traceback.print_exc()
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
            print(f"[MacOSAdapter:perform_action] {e}")
            traceback.print_exc()
            return {"success": False, "error": str(e)}

"""
observer.py — Core screen observation module.

Provides a platform-aware ScreenObserver that exposes a uniform interface
for: enumerating windows, walking the accessibility element tree, capturing
screenshots, and dispatching input actions. Platform adapters (Windows/macOS/
Linux/Mock) share a common base and are selected automatically at runtime.

Data model
----------
  Bounds       — screen-coordinate bounding rectangle
  UIElement    — one node of the accessibility tree
  WindowInfo   — top-level window metadata
"""

import io
import logging
import platform
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLATFORM = platform.system()


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Bounds:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center_x(self) -> int:
        return self.x + self.width // 2

    @property
    def center_y(self) -> int:
        return self.y + self.height // 2

    def to_dict(self) -> Dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    def __bool__(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass
class UIElement:
    element_id: str
    name: str
    role: str
    value: Optional[str] = None
    bounds: Bounds = field(default_factory=lambda: Bounds(0, 0, 0, 0))
    enabled: bool = True
    focused: bool = False
    keyboard_shortcut: Optional[str] = None
    description: Optional[str] = None
    children: List["UIElement"] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "id": self.element_id,
            "name": self.name,
            "role": self.role,
            "value": self.value,
            "bounds": self.bounds.to_dict(),
            "enabled": self.enabled,
            "focused": self.focused,
            "keyboard_shortcut": self.keyboard_shortcut,
            "description": self.description,
            "children": [c.to_dict() for c in self.children],
        }

    def flat_list(self) -> List["UIElement"]:
        """Return all elements in this subtree as a flat list (DFS order)."""
        result = [self]
        for child in self.children:
            result.extend(child.flat_list())
        return result


@dataclass
class WindowInfo:
    handle: Any          # platform-specific: HWND (int) on Windows; int index elsewhere
    title: str
    process_name: str
    pid: int
    bounds: Bounds
    is_focused: bool
    # Stable cross-call identifier; populated by adapters (design doc §6.1).
    window_uid: str = ""
    # Optional multi-monitor metadata (design doc §6.3).  Populated when the
    # adapter knows; left None on adapters that do not.
    monitor_index: Optional[int] = None
    scale_factor: Optional[float] = None
    logical_bounds: Optional[Bounds] = None
    physical_bounds: Optional[Bounds] = None

    def to_dict(self) -> Dict:
        d: Dict[str, Any] = {
            "handle": str(self.handle),
            "title": self.title,
            "process": self.process_name,
            "pid": self.pid,
            "bounds": self.bounds.to_dict(),
            "focused": self.is_focused,
            "window_uid": self.window_uid,
        }
        if self.monitor_index is not None:
            d["monitor_index"] = self.monitor_index
        if self.scale_factor is not None:
            d["scale_factor"] = self.scale_factor
        if self.logical_bounds is not None:
            d["logical_bounds"] = self.logical_bounds.to_dict()
        if self.physical_bounds is not None:
            d["physical_bounds"] = self.physical_bounds.to_dict()
        return d


# ─── Window resolution result ────────────────────────────────────────────────

@dataclass
class WindowResolution:
    info: Optional[WindowInfo]
    warning: Optional[str]
    used_uid: bool
    requested_uid: Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# Mock Adapter  (no OS dependencies; safe to run in any environment)
# ─────────────────────────────────────────────────────────────────────────────

class MockAdapter:
    """Synthetic data adapter for development and testing."""

    def __init__(self) -> None:
        import secrets as _s
        self._nonce = _s.token_hex(4)
        # Optional scenario hook (design doc §15.5).  Set by main.py when
        # --scenario is supplied; methods route through the scenario when
        # active so that input actions can drive state transitions.
        self.scenario: Optional[Any] = None

    def get_windows_above_bounds(self, hwnd) -> List["Bounds"]:
        return []  # Mock assumes the target window is on top

    def list_windows(self) -> List[WindowInfo]:
        if self.scenario is not None:
            return self.scenario.list_windows(self._nonce)
        return [
            WindowInfo(1001, "Untitled — Notepad", "notepad.exe", 1234,
                       Bounds(80, 60, 800, 600), True,
                       window_uid=f"mock:0:{self._nonce}"),
            WindowInfo(1002, "GitHub · Where software is built — Google Chrome",
                       "chrome.exe", 5678, Bounds(0, 0, 1920, 1050), False,
                       window_uid=f"mock:1:{self._nonce}"),
            WindowInfo(1003, "screen_observer.py — Visual Studio Code",
                       "code.exe", 9012, Bounds(960, 0, 960, 1050), False,
                       window_uid=f"mock:2:{self._nonce}"),
        ]

    def get_element_tree(self, hwnd=None) -> Optional[UIElement]:
        if self.scenario is not None:
            return self.scenario.get_element_tree(hwnd)
        root = UIElement("root", "Untitled — Notepad", "Window",
                         bounds=Bounds(80, 60, 800, 600))

        # Menu bar
        menubar = UIElement("root.0", "MenuBar", "MenuBar",
                            bounds=Bounds(80, 60, 800, 22))
        for i, lbl in enumerate(["File", "Edit", "Format", "View", "Help"]):
            menubar.children.append(UIElement(
                f"root.0.{i}", lbl, "MenuItem",
                bounds=Bounds(80 + i * 58, 60, 56, 22)
            ))
        root.children.append(menubar)

        # Main editing area
        editor = UIElement(
            "root.1", "Text Editor", "Document",
            value="Hello, world!\nThis is a test document.\nLine 3 has some content here.\n",
            bounds=Bounds(80, 82, 800, 514), focused=True
        )
        root.children.append(editor)

        # Horizontal scroll bar
        hscroll = UIElement("root.2", "Horizontal ScrollBar", "ScrollBar",
                            bounds=Bounds(80, 596, 784, 18))
        root.children.append(hscroll)

        # Vertical scroll bar
        vscroll = UIElement("root.3", "Vertical ScrollBar", "ScrollBar",
                            bounds=Bounds(864, 82, 18, 532))
        root.children.append(vscroll)

        # Status bar
        sb = UIElement("root.4", "Status Bar", "StatusBar",
                       bounds=Bounds(80, 614, 800, 22))
        for i, (lbl, val) in enumerate([
            ("Position",  "Ln 1, Col 1"),
            ("Zoom",      "100%"),
            ("Encoding",  "UTF-8"),
            ("EOL",       "Windows (CRLF)"),
        ]):
            sb.children.append(UIElement(
                f"root.4.{i}", lbl, "Text", value=val,
                bounds=Bounds(80 + i * 190, 614, 188, 22)
            ))
        root.children.append(sb)

        return root

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (800, 600), "#1a1e2e")
            draw = ImageDraw.Draw(img)

            # Title bar
            draw.rectangle([0, 0, 800, 30], fill="#2d3250")
            draw.text((10, 8), "Untitled — Notepad", fill="#c8d3f5")

            # Menu bar
            draw.rectangle([0, 30, 800, 52], fill="#1e2030")
            for i, m in enumerate(["File", "Edit", "Format", "View", "Help"]):
                draw.text((10 + i * 58, 36), m, fill="#a9b1d6")

            # Editor area
            draw.rectangle([0, 52, 782, 570], fill="#1a1e2e")
            for i, ln in enumerate(["Hello, world!", "This is a test document.",
                                     "Line 3 has some content here.", ""]):
                draw.text((6, 58 + i * 18), ln, fill="#c0caf5")

            # Scrollbars
            draw.rectangle([782, 52, 800, 570], fill="#24283b")
            draw.rectangle([0, 570, 782, 586], fill="#24283b")

            # Status bar
            draw.rectangle([0, 586, 800, 600], fill="#16161e")
            draw.text((6, 589), "Ln 1, Col 1     100%     UTF-8     Windows (CRLF)",
                      fill="#565f89")

            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        except Exception as e:
            print(f"[MockAdapter:get_screenshot] {e}")
            traceback.print_exc()
            return None

    def perform_action(self, action: str, element_id: str = None,
                       value: Any = None, hwnd=None) -> Dict:
        if self.scenario is not None:
            handled = self.scenario.handle_action(action=action,
                                                   element_id=element_id,
                                                   value=value, hwnd=hwnd)
            if handled is not None:
                return handled
        return {
            "success": True,
            "action": action,
            "note": "Mock adapter — no real OS action performed",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Windows Adapter  (requires: pywinauto, pywin32, psutil)
# ─────────────────────────────────────────────────────────────────────────────

class WindowsAdapter:
    """Full Windows UIA adapter using pywinauto + pywin32."""

    def __init__(self, config: dict):
        self.config = config
        try:
            import win32gui    # noqa: F401
            import win32process  # noqa: F401
            import psutil      # noqa: F401
            from pywinauto import Application  # noqa: F401
            self._Application = Application
            logger.info("[WindowsAdapter:__init__] pywinauto/pywin32 ready")
        except ImportError as e:
            print(f"[WindowsAdapter:__init__] Missing dependency: {e}")
            traceback.print_exc()
            raise

    def list_windows(self) -> List[WindowInfo]:
        try:
            import win32gui
            import win32process
            import psutil

            results: List[WindowInfo] = []
            fg = win32gui.GetForegroundWindow()

            def _cb(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return
                    title = win32gui.GetWindowText(hwnd)
                    if not title:
                        return
                    rect = win32gui.GetWindowRect(hwnd)
                    w, h = rect[2] - rect[0], rect[3] - rect[1]
                    if w <= 0 or h <= 0:
                        return
                    try:
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        proc_name = psutil.Process(pid).name()
                    except Exception:
                        pid, proc_name = 0, "unknown"
                    results.append(WindowInfo(
                        handle=hwnd, title=title, process_name=proc_name, pid=pid,
                        bounds=Bounds(rect[0], rect[1], w, h),
                        is_focused=(hwnd == fg),
                        window_uid=f"win:{pid}:{hwnd}",
                    ))
                except Exception as inner:
                    logger.debug(f"[WindowsAdapter:list_windows:_cb] {inner}")

            win32gui.EnumWindows(_cb, None)
            return sorted(results, key=lambda w: (not w.is_focused, w.title.lower()))
        except Exception as e:
            print(f"[WindowsAdapter:list_windows] {e}")
            traceback.print_exc()
            return []

    def get_windows_above_bounds(self, hwnd) -> List[Bounds]:
        """Return bounds of visible windows that are above hwnd in Z-order."""
        try:
            import win32gui
            GW_HWNDNEXT = 2
            above: List[Bounds] = []
            h = win32gui.GetTopWindow(None)
            while h and h != hwnd:
                try:
                    if win32gui.IsWindowVisible(h):
                        rect = win32gui.GetWindowRect(h)
                        w = rect[2] - rect[0]
                        hh = rect[3] - rect[1]
                        if w > 0 and hh > 0:
                            above.append(Bounds(rect[0], rect[1], w, hh))
                except Exception:
                    pass
                try:
                    h = win32gui.GetWindow(h, GW_HWNDNEXT)
                except Exception:
                    break
            return above
        except Exception as e:
            logger.debug(f"[WindowsAdapter:get_windows_above_bounds] {e}")
            return []

    def get_element_tree(self, hwnd=None) -> Optional[UIElement]:
        try:
            import win32gui

            if hwnd is None:
                hwnd = win32gui.GetForegroundWindow()

            app = self._Application(backend="uia").connect(handle=hwnd)
            window = app.window(handle=hwnd)
            wrapper = window.wrapper_object()
            max_depth = self.config.get("tree", {}).get("max_depth", 8)
            return self._walk(wrapper, "root", 0, max_depth)
        except Exception as e:
            print(f"[WindowsAdapter:get_element_tree] {e}")
            traceback.print_exc()
            return None

    def _walk(self, wrapper, elem_id: str, depth: int, max_depth: int) -> UIElement:
        try:
            try:
                rect = wrapper.rectangle()
                bounds = Bounds(rect.left, rect.top,
                                rect.right - rect.left, rect.bottom - rect.top)
            except Exception:
                bounds = Bounds(0, 0, 0, 0)

            try:
                name = wrapper.window_text() or ""
            except Exception:
                name = ""

            try:
                role = wrapper.friendly_class_name() or "Unknown"
            except Exception:
                role = "Unknown"

            try:
                value = wrapper.get_value() if hasattr(wrapper, "get_value") else None
            except Exception:
                value = None

            try:
                enabled = wrapper.is_enabled()
            except Exception:
                enabled = True

            try:
                focused = wrapper.has_keyboard_focus()
            except Exception:
                focused = False

            elem = UIElement(
                element_id=elem_id, name=name, role=role, value=value,
                bounds=bounds, enabled=enabled, focused=focused,
            )

            if depth < max_depth:
                try:
                    for i, child in enumerate(wrapper.children()):
                        elem.children.append(
                            self._walk(child, f"{elem_id}.{i}", depth + 1, max_depth)
                        )
                except Exception as ce:
                    logger.debug(f"[WindowsAdapter:_walk:{elem_id}:children] {ce}")

            return elem
        except Exception as e:
            print(f"[WindowsAdapter:_walk:{elem_id}] {e}")
            traceback.print_exc()
            return UIElement(elem_id, "[error]", "Unknown", bounds=Bounds(0, 0, 0, 0))

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        try:
            import win32gui
            import win32ui
            from PIL import Image
            import ctypes

            if hwnd is None:
                hwnd = win32gui.GetForegroundWindow()

            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]

            if width <= 0 or height <= 0:
                return None

            hwnd_dc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            save_bmp = win32ui.CreateBitmap()
            save_bmp.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(save_bmp)

            # PW_RENDERFULLCONTENT (0x2) renders hardware-accelerated content too
            ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)

            bmpinfo = save_bmp.GetInfo()
            bmpstr = save_bmp.GetBitmapBits(True)

            win32gui.DeleteObject(save_bmp.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)

            if ok:
                img = Image.frombuffer(
                    "RGB",
                    (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                    bmpstr, "raw", "BGRX", 0, 1,
                )
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return buf.getvalue()

            # PrintWindow failed — fall back to screen-region capture
            logger.warning("[WindowsAdapter:get_screenshot] PrintWindow failed; falling back to mss")
            raise RuntimeError("PrintWindow returned 0")

        except Exception as e:
            logger.debug(f"[WindowsAdapter:get_screenshot] PrintWindow path failed ({e}); trying mss")
            try:
                import mss
                from PIL import Image
                import win32gui

                with mss.mss() as sct:
                    if hwnd:
                        rect = win32gui.GetWindowRect(hwnd)
                        region = {"left": rect[0], "top": rect[1],
                                  "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
                    else:
                        region = sct.monitors[1]
                    raw = sct.grab(region)
                    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    return buf.getvalue()
            except Exception as e2:
                print(f"[WindowsAdapter:get_screenshot] {e2}")
                traceback.print_exc()
                return None

    def perform_action(self, action: str, element_id: str = None,
                       value: Any = None, hwnd=None) -> Dict:
        try:
            import pyautogui

            if action == "type" and value:
                pyautogui.write(str(value), interval=0.02)
                return {"success": True, "action": "type", "text": value}

            elif action == "key" and value:
                keys = str(value).lower().split("+")
                pyautogui.hotkey(*keys)
                return {"success": True, "action": "key", "keys": value}

            elif action == "click_at" and isinstance(value, dict):
                pyautogui.click(value["x"], value["y"])
                return {"success": True, "action": "click_at", **value}

            elif action == "scroll" and isinstance(value, dict):
                pyautogui.scroll(value.get("clicks", 3), x=value.get("x"), y=value.get("y"))
                return {"success": True, "action": "scroll"}

            else:
                return {"success": False, "error": f"Unsupported action: {action}"}
        except Exception as e:
            print(f"[WindowsAdapter:perform_action] {e}")
            traceback.print_exc()
            return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# macOS Adapter  (screenshot works; AX tree is a stub pending pyobjc work)
# ─────────────────────────────────────────────────────────────────────────────

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
            with mss.mss() as sct:
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


# ─────────────────────────────────────────────────────────────────────────────
# Linux Adapter  (screenshot works; AT-SPI tree is stub pending pyatspi work)
# ─────────────────────────────────────────────────────────────────────────────

class LinuxAdapter:
    def __init__(self, config: dict):
        self.config = config
        logger.info("[LinuxAdapter:__init__] Linux adapter loaded (AT-SPI tree is stub)")

    def get_windows_above_bounds(self, hwnd) -> List[Bounds]:
        return []  # Z-order unavailable without Xlib/wnck

    def list_windows(self) -> List[WindowInfo]:
        try:
            import subprocess
            r = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=5)
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

    def get_element_tree(self, hwnd=None) -> Optional[UIElement]:
        logger.warning("[LinuxAdapter:get_element_tree] Full tree requires pyatspi; returning stub")
        return UIElement("root", "Linux Application (AT-SPI stub)", "Window",
                         bounds=Bounds(0, 0, 1920, 1080))

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        try:
            import mss
            from PIL import Image
            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return buf.getvalue()
        except Exception as e:
            print(f"[LinuxAdapter:get_screenshot] {e}")
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
            print(f"[LinuxAdapter:perform_action] {e}")
            traceback.print_exc()
            return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ScreenObserver  (public interface; delegates to platform adapter)
# ─────────────────────────────────────────────────────────────────────────────

class ScreenObserver:
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
        sys_plat = PLATFORM if target == "auto" else target

        adapters = {
            "Windows": WindowsAdapter,
            "Darwin":  MacOSAdapter,
            "Linux":   LinuxAdapter,
        }

        cls = adapters.get(sys_plat)
        if cls is None:
            logger.warning(f"[ScreenObserver] Unknown platform '{sys_plat}'; using MockAdapter")
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

    def get_element_tree(self, hwnd=None) -> Optional[UIElement]:
        return self._adapter.get_element_tree(hwnd)

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        return self._adapter.get_screenshot(hwnd)

    def get_full_display_screenshot(self) -> Optional[bytes]:
        """Capture the entire virtual desktop (all monitors combined) as a PNG."""
        try:
            import mss
            from PIL import Image
            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[0])   # 0 = union of all monitors
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return buf.getvalue()
        except Exception as e:
            logger.warning(f"[ScreenObserver:get_full_display_screenshot] {e}; falling back")
            return self._adapter.get_screenshot()

    def perform_action(self, action: str, element_id: str = None,
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
                       window_index: Optional[int]) -> "WindowResolution":
        """Apply the design-doc precedence: uid wins; warn when both given."""
        if window_uid:
            info = self.window_by_uid(windows, window_uid)
            warning = ("both window_index and window_uid given; window_uid used"
                       if window_index is not None else None)
            return WindowResolution(info=info, warning=warning,
                                    used_uid=True, requested_uid=window_uid)
        info = self.window_by_index(windows, window_index)
        return WindowResolution(info=info, warning=None,
                                used_uid=False, requested_uid=None)

    # ── Monitors / DPI (design doc §6.3) ──────────────────────────────────────

    def get_monitors(self) -> List[Dict[str, Any]]:
        """Return per-monitor metadata via mss."""
        try:
            import mss
            with mss.mss() as sct:
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
        is_linux   = adapter_name == "LinuxAdapter"
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
            "platform": PLATFORM,
            "adapter": adapter_name,
            "version": (self.config.get("mcp", {}) or {}).get("version", "0.2.0"),
            "protocol_version": 2,
            "supports": {
                "accessibility_tree":  bool(ax_tree),
                "uia_invoke":          is_windows,
                "occlusion_detection": is_windows or is_mock or _has("Quartz") or _has("Xlib"),
                "drag":                True,
                "ocr":                 _has("pytesseract"),
                "vlm":                 _has("anthropic"),
                "redaction":           True,
                "scenarios":           is_mock,
                "tracing":             True,
                "replay":              True,
                "image_blur":          _has("PIL"),
            },
            "config": {
                "tree_max_depth": (self.config.get("tree", {}) or {}).get("max_depth", 8),
                "ascii_grid": {
                    "width":  (self.config.get("ascii_sketch", {}) or {}).get("grid_width",  110),
                    "height": (self.config.get("ascii_sketch", {}) or {}).get("grid_height",  38),
                },
            },
        }

    # ── Element occlusion (design doc D14) ────────────────────────────────────

    def is_element_occluded(self, element_bounds: Bounds, target_hwnd: Any,
                            all_windows: List[WindowInfo]) -> bool:
        """
        True iff every pixel of *element_bounds* is covered by another window
        above the target in Z-order, or the element lies entirely off-screen.

        On platforms without Z-order info, returns False (assumed visible).
        """
        screen = self.get_screen_bounds()
        clipped = _intersect_bounds(element_bounds, screen)
        if not clipped:
            return True
        regions = [clipped]
        try:
            occluders = self._adapter.get_windows_above_bounds(target_hwnd)
        except Exception:
            occluders = []
        for occ in occluders:
            regions = _subtract_rect(regions, occ)
        return not regions

    # ── Visibility helpers ────────────────────────────────────────────────────

    def get_screen_bounds(self) -> Bounds:
        """Return the bounding rect of the combined virtual screen (all monitors)."""
        try:
            import mss
            with mss.mss() as sct:
                m = sct.monitors[0]   # index 0 = union of all monitors
                return Bounds(m["left"], m["top"], m["width"], m["height"])
        except Exception:
            return Bounds(0, 0, 65535, 65535)

    def get_visible_areas(self, target_hwnd: Any,
                          all_windows: List[WindowInfo]) -> List[Dict]:
        """
        Return a list of {x, y, width, height} dicts for the portions of the
        window identified by *target_hwnd* that are on-screen and not covered
        by windows above it in Z-order.

        On Windows the Z-order is queried precisely via win32gui.
        On macOS/Linux Z-order is unavailable, so the full clipped-to-screen
        bounds are returned (assuming the window is on top).
        """
        target = next((w for w in all_windows if w.handle == target_hwnd), None)
        if target is None:
            return []

        screen   = self.get_screen_bounds()
        clipped  = _intersect_bounds(target.bounds, screen)
        if not clipped:
            return []

        visible: List[Bounds] = [clipped]
        occluders = self._adapter.get_windows_above_bounds(target_hwnd)
        for occ in occluders:
            visible = _subtract_rect(visible, occ)

        return [b.to_dict() for b in visible]

    def bring_to_foreground(self, target_hwnd: Any,
                            all_windows: List[WindowInfo]) -> Dict:
        """
        Bring a window to the foreground by clicking in its title-bar area.

        Strategy
        --------
        1. Compute the visible regions of the window (non-occluded, on-screen).
           On Windows this uses real Z-order; on macOS/Linux the window is
           assumed to be on top so the screen-clipped bounds are returned.
        2. Pick the top-most region (lowest y-value) — that is where the title
           bar lives. If multiple regions share the same top edge, prefer the
           widest one.
        3. Click near the top-centre of that region (~20 px below the top edge,
           clamped to stay strictly inside the region).

        Returns the click result dict, or an error dict when no visible area
        exists (window fully off-screen or, on Windows, fully occluded).
        """
        regions = self.get_visible_areas(target_hwnd, all_windows)
        if not regions:
            # On Windows "no regions" means fully occluded; clicking the raw
            # bounds would hit the covering window instead.  On macOS/Linux the
            # platform adapter returns no occluders, so an empty result means
            # the window is off-screen.  In both cases refuse the click.
            target = next((w for w in all_windows if w.handle == target_hwnd), None)
            if target is None:
                return {"success": False, "error": "Window not found"}
            return {"success": False,
                    "error": "Window has no visible area (fully off-screen or occluded)"}

        # Pick the top-most region (title bar is near the top of the window).
        # Break ties by width so we prefer the widest strip at that y-level.
        best = min(regions, key=lambda r: (r["y"], -r["width"]))

        # Click near the top-centre; offset ~20 px down (title bar height).
        # Keep both coordinates strictly inside the region bounds.
        title_bar_offset = min(20, max(1, (best["height"] - 1) // 2))
        click_x = best["x"] + best["width"] // 2
        click_y = best["y"] + title_bar_offset
        # Clamp to region interior [x, x+width-1] × [y, y+height-1]
        click_x = max(best["x"], min(best["x"] + best["width"]  - 1, click_x))
        click_y = max(best["y"], min(best["y"] + best["height"] - 1, click_y))

        result = self.perform_action("click_at",
                                     value={"x": click_x, "y": click_y,
                                            "button": "left", "double": False})
        result["clicked_x"] = click_x
        result["clicked_y"] = click_y
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Rectangle geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _intersect_bounds(a: Bounds, b: Bounds) -> Optional[Bounds]:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.right, b.right)
    y2 = min(a.bottom, b.bottom)
    if x2 <= x1 or y2 <= y1:
        return None
    return Bounds(x1, y1, x2 - x1, y2 - y1)


def _subtract_rect(rects: List[Bounds], occluder: Bounds) -> List[Bounds]:
    """Subtract occluder from each rect, splitting into up to 4 sub-rects."""
    result: List[Bounds] = []
    for r in rects:
        ix1 = max(r.x, occluder.x)
        iy1 = max(r.y, occluder.y)
        ix2 = min(r.right, occluder.right)
        iy2 = min(r.bottom, occluder.bottom)

        if ix2 <= ix1 or iy2 <= iy1:
            result.append(r)
            continue

        # Top strip
        if iy1 > r.y:
            result.append(Bounds(r.x, r.y, r.width, iy1 - r.y))
        # Bottom strip
        if iy2 < r.bottom:
            result.append(Bounds(r.x, iy2, r.width, r.bottom - iy2))
        # Left strip (height = intersection height)
        if ix1 > r.x:
            result.append(Bounds(r.x, iy1, ix1 - r.x, iy2 - iy1))
        # Right strip (height = intersection height)
        if ix2 < r.right:
            result.append(Bounds(ix2, iy1, r.right - ix2, iy2 - iy1))
    return result

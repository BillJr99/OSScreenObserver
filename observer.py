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
import os
import platform
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLATFORM = platform.system()


def _is_wsl() -> bool:
    """True when running inside Windows Subsystem for Linux (WSL 1 or WSL 2)."""
    if PLATFORM != "Linux":
        return False
    try:
        with open("/proc/version") as _f:
            return "microsoft" in _f.read().lower()
    except Exception:
        return False


IS_WSL = _is_wsl()
EFFECTIVE_PLATFORM = "WSL" if IS_WSL else PLATFORM


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
    # Extended a11y signals — None means "adapter could not determine".
    # Populated where the platform exposes the pattern (UIA SelectionItem /
    # ExpandCollapse / RangeValue, AX AXValue/AXMinValue/AXMaxValue, AT-SPI
    # STATE_SELECTED / STATE_EXPANDED / IValue) and consumed by the ASCII
    # renderer for role-aware glyphs and the structured sidecar.
    selected:   Optional[bool]  = None
    expanded:   Optional[bool]  = None
    value_now:  Optional[float] = None
    value_min:  Optional[float] = None
    value_max:  Optional[float] = None
    identifier: Optional[str]   = None

    def to_dict(self) -> Dict:
        d: Dict[str, Any] = {
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
        # Omit extended fields when unset so existing API consumers do not
        # see a flood of nulls; include them when populated.
        for k in ("selected", "expanded", "value_now", "value_min",
                  "value_max", "identifier"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

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

        # Progress bar — exercises value_now/min/max for the role glyph path.
        root.children.append(UIElement(
            "root.5", "Saving", "ProgressBar",
            bounds=Bounds(560, 600, 200, 10),
            value_now=40.0, value_min=0.0, value_max=100.0,
        ))

        # Word-wrap toggle checkbox — exercises selected=True path.
        root.children.append(UIElement(
            "root.6", "Word Wrap", "CheckBox",
            bounds=Bounds(80, 636, 120, 18), selected=True,
        ))

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
# UIA control-type ID → pywinauto-compatible role name.
# Values from UIAutomationClient.h; must match what selectors/descriptions expect.
_UIA_TYPE_TO_ROLE: Dict[int, str] = {
    50000: "Button",      50001: "Calendar",    50002: "CheckBox",
    50003: "ComboBox",    50004: "Edit",         50005: "Hyperlink",
    50006: "Image",       50007: "ListItem",     50008: "ListBox",
    50009: "Menu",        50010: "MenuBar",      50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton",  50014: "ScrollBar",
    50015: "Slider",      50016: "Spinner",      50017: "StatusBar",
    50018: "TabControl",  50019: "TabItem",      50020: "Text",
    50021: "Toolbar",     50022: "ToolTip",      50023: "Tree",
    50024: "TreeItem",    50025: "Custom",       50026: "GroupBox",
    50027: "Thumb",       50028: "DataGrid",     50029: "DataItem",
    50030: "Document",    50031: "SplitButton",  50032: "Dialog",
    50033: "Pane",        50034: "Header",       50035: "HeaderItem",
    50036: "Table",       50037: "TitleBar",     50038: "Separator",
    50039: "SemanticZoom",50040: "AppBar",
}


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
        except Exception as e:
            print(f"[WindowsAdapter:get_element_tree] win32gui: {e}")
            return None

        # Run both walkers and synthesise: UIA crosses Chromium fragment boundaries
        # (gets web content); pywinauto may surface native-control properties that
        # UIA omits.  The merged result gives the LLM everything either source sees.
        uia_tree = self._uia_walk(hwnd)
        pw_tree  = None
        try:
            app = self._Application(backend="uia").connect(handle=hwnd)
            window = app.window(handle=hwnd)
            wrapper = window.wrapper_object()
            max_depth = self.config.get("tree", {}).get("max_depth", 8)
            pw_tree = self._walk(wrapper, "root", 0, max_depth)
        except Exception as e:
            logger.debug(f"[WindowsAdapter:get_element_tree] pywinauto: {e}")

        if uia_tree is None and pw_tree is None:
            return None
        if uia_tree is None:
            return pw_tree
        if pw_tree is None:
            return uia_tree
        return self._synthesize_trees(uia_tree, pw_tree)

    def _uia_walk(self, hwnd: int) -> Optional[UIElement]:
        """Walk the accessibility tree via raw IUIAutomation COM calls.

        pywinauto's children() falls back to EnumChildWindows for HWND-backed
        elements (e.g. Chrome_RenderWidgetHostHWND), missing all web content.
        Using FindAll(TreeScope_Children) directly on the IUIAutomationElement
        always uses UIA and correctly crosses Chromium fragment boundaries.
        """
        try:
            # pywinauto already initialised comtypes/UIA at import time.
            # Retrieve the raw IUIAutomation COM interface from pywinauto's singleton.
            from pywinauto.uia_defines import IUIA
            _iuia_obj = IUIA()
            # pywinauto ≥0.6 exposes it as .iuia; older versions expose it directly.
            raw_uia = getattr(_iuia_obj, "iuia", _iuia_obj)

            root = raw_uia.ElementFromHandle(hwnd)
            true_cond = raw_uia.CreateTrueCondition()
            max_depth = self.config.get("tree", {}).get("max_depth", 8)

            # UIA property IDs (UIAutomationClient.h)
            _NAME            = 30005
            _CTRL_TYPE       = 30003
            _ENABLED         = 30010
            _FOCUSED         = 30008
            _VALUE           = 30045
            _ACCESS_KEY      = 30023   # keyboard mnemonic, e.g. "Alt+F"
            _ACCEL_KEY       = 30022   # accelerator, e.g. "Ctrl+Z"
            _HELP_TEXT       = 30013   # tooltip / description
            _AUTOMATION_ID   = 30011
            _RANGE_VALUE     = 30047
            _RANGE_MIN       = 30049
            _RANGE_MAX       = 30050
            _IS_SELECTED     = 30079
            _EXPAND_STATE    = 30084   # 0=collapsed 1=expanded 2=partial 3=leaf
            _SCOPE_CHILDREN  = 0x2

            def _prop(elem, pid, default=None):
                try:
                    v = elem.GetCurrentPropertyValue(pid)
                    return v if v is not None else default
                except Exception:
                    return default

            def walk(elem, elem_id: str, depth: int) -> UIElement:
                name = _prop(elem, _NAME, "") or ""
                ctrl = _prop(elem, _CTRL_TYPE, 0) or 0
                role = _UIA_TYPE_TO_ROLE.get(ctrl, "Pane")
                try:
                    r = elem.CurrentBoundingRectangle
                    bounds = Bounds(r.left, r.top, r.right - r.left, r.bottom - r.top)
                except Exception:
                    bounds = Bounds(0, 0, 0, 0)
                enabled = bool(_prop(elem, _ENABLED, True))
                focused = bool(_prop(elem, _FOCUSED, False))
                value   = _prop(elem, _VALUE) or None
                # Keyboard shortcut: prefer access key, fall back to accelerator.
                ks = _prop(elem, _ACCESS_KEY) or _prop(elem, _ACCEL_KEY) or None
                desc = _prop(elem, _HELP_TEXT) or None
                aid = _prop(elem, _AUTOMATION_ID) or None
                # RangeValue pattern: slider / progress / scrollbar
                vn = _prop(elem, _RANGE_VALUE)
                vmin = _prop(elem, _RANGE_MIN)
                vmax = _prop(elem, _RANGE_MAX)
                # SelectionItem.IsSelected: checkbox / radio / tab / menuitem
                sel_raw = _prop(elem, _IS_SELECTED)
                sel = bool(sel_raw) if sel_raw is not None else None
                # ExpandCollapse: combobox / treeitem / menuitem
                exp_raw = _prop(elem, _EXPAND_STATE)
                if exp_raw in (0, 1):
                    exp = bool(exp_raw)
                else:
                    exp = None
                try:
                    vn_f = float(vn) if vn is not None else None
                except Exception:
                    vn_f = None
                try:
                    vmin_f = float(vmin) if vmin is not None else None
                except Exception:
                    vmin_f = None
                try:
                    vmax_f = float(vmax) if vmax is not None else None
                except Exception:
                    vmax_f = None
                node = UIElement(
                    element_id=elem_id, name=name, role=role, value=value,
                    bounds=bounds, enabled=enabled, focused=focused,
                    keyboard_shortcut=ks or None,
                    description=desc or None,
                    selected=sel, expanded=exp,
                    value_now=vn_f, value_min=vmin_f, value_max=vmax_f,
                    identifier=str(aid) if aid else None,
                )
                if depth < max_depth:
                    try:
                        kids = elem.FindAll(_SCOPE_CHILDREN, true_cond)
                        for i in range(kids.Length):
                            node.children.append(
                                walk(kids.GetElement(i), f"{elem_id}.{i}", depth + 1)
                            )
                    except Exception:
                        pass
                return node

            return walk(root, "root", 0)
        except Exception as e:
            logger.debug(f"[WindowsAdapter:_uia_walk] {e}")
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

    def _synthesize_trees(self, primary: UIElement, secondary: UIElement) -> UIElement:
        """Merge two accessibility trees into one richer tree.

        Uses the primary (UIA) tree as the base — it sees web content.
        For each node in secondary (pywinauto) matched by bounds, enrich the
        primary node with any non-empty properties the primary is missing.
        Secondary nodes whose bounds don't appear anywhere in the primary tree
        are injected under the deepest primary ancestor that contains them.
        """
        # Build a flat index of primary nodes keyed by (x, y, w, h).
        bounds_index: Dict[tuple, UIElement] = {}

        def _index(node: UIElement) -> None:
            key = (node.bounds.x, node.bounds.y, node.bounds.width, node.bounds.height)
            if key not in bounds_index:
                bounds_index[key] = node
            for c in node.children:
                _index(c)

        _index(primary)

        # Enrich matched nodes; collect unmatched ones with their bounds.
        unmatched: List[UIElement] = []

        def _enrich(node: UIElement) -> None:
            key = (node.bounds.x, node.bounds.y, node.bounds.width, node.bounds.height)
            target = bounds_index.get(key)
            if target is not None:
                # Copy over properties the primary left empty.
                if not target.keyboard_shortcut and node.keyboard_shortcut:
                    target.keyboard_shortcut = node.keyboard_shortcut
                if not target.description and node.description:
                    target.description = node.description
                if not target.value and node.value:
                    target.value = node.value
            else:
                # Not in primary — keep for injection.
                w, h = node.bounds.width, node.bounds.height
                if w > 0 and h > 0:   # ignore zero-size ghost elements
                    unmatched.append(node)
            for c in node.children:
                _enrich(c)

        _enrich(secondary)

        # Inject unmatched secondary nodes under the deepest primary ancestor
        # whose bounds contain them (largest-area match wins → most specific).
        def _contains(outer: Bounds, inner: Bounds) -> bool:
            return (outer.x <= inner.x and outer.y <= inner.y and
                    outer.x + outer.width  >= inner.x + inner.width and
                    outer.y + outer.height >= inner.y + inner.height)

        for node in unmatched:
            best: Optional[UIElement] = None
            best_area = float("inf")
            for pnode in bounds_index.values():
                if _contains(pnode.bounds, node.bounds):
                    area = pnode.bounds.width * pnode.bounds.height
                    if area < best_area:
                        best_area = area
                        best = pnode
            if best is None:
                best = primary
            # Renumber the injected element_id to avoid collisions.
            node.element_id = f"{best.element_id}.x{len(best.children)}"
            best.children.append(node)

        return primary

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
        # mss needs a running X server (DISPLAY must be set).
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


# ─────────────────────────────────────────────────────────────────────────────
# WSL Adapter  (WSL 1 + WSL 2: X11 when DISPLAY is set, PowerShell fallback)
# ─────────────────────────────────────────────────────────────────────────────

class WSLAdapter(LinuxAdapter):
    """Adapter for Windows Subsystem for Linux.

    Prefers X11-based tools (wmctrl, mss) when DISPLAY is set.  Falls back to
    PowerShell / cmd.exe interop, which is always available in both WSL 1 and
    WSL 2 via the Windows binary execution layer.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self._has_display = bool(os.environ.get("DISPLAY"))
        logger.info(
            "[WSLAdapter:__init__] WSL detected; "
            "DISPLAY=%s", "set" if self._has_display else "not set (PowerShell fallback active)",
        )

    # ── Window listing ────────────────────────────────────────────────────────

    def list_windows(self) -> List[WindowInfo]:
        if self._has_display:
            result = LinuxAdapter.list_windows(self)
            if result:
                return result
        return self._list_windows_ps()

    def _list_windows_ps(self) -> List[WindowInfo]:
        """Enumerate visible Windows windows via PowerShell ConvertTo-Json."""
        try:
            import json
            import subprocess
            ps = (
                "Get-Process "
                "| Where-Object { $_.MainWindowTitle -ne '' } "
                "| Select-Object Id,ProcessName,MainWindowTitle "
                "| ConvertTo-Json -Compress"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return []
            data = json.loads(r.stdout)
            if isinstance(data, dict):
                data = [data]
            results: List[WindowInfo] = []
            for i, item in enumerate(data or []):
                pid   = int(item.get("Id", 0))
                name  = str(item.get("ProcessName", "unknown"))
                title = str(item.get("MainWindowTitle", ""))
                if not title:
                    continue
                results.append(WindowInfo(
                    handle=pid, title=title, process_name=name, pid=pid,
                    bounds=Bounds(0, 0, 1920, 1080), is_focused=(i == 0),
                    window_uid=f"wsl:{pid}",
                ))
            return results
        except Exception as e:
            logger.debug("[WSLAdapter:_list_windows_ps] %s", e)
            return []

    # ── Screenshot ────────────────────────────────────────────────────────────

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        if self._has_display:
            result = LinuxAdapter.get_screenshot(self, hwnd)
            if result:
                return result
        return self._screenshot_ps()

    def _screenshot_ps(self) -> Optional[bytes]:
        """Capture the primary screen via PowerShell, returning PNG bytes."""
        try:
            import base64
            import subprocess
            # Capture screen to a MemoryStream and emit as base64 — avoids
            # WSL↔Windows path translation issues entirely.
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                "$ms=New-Object System.IO.MemoryStream;"
                "$bmp.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png);"
                "$g.Dispose();$bmp.Dispose();"
                "[Convert]::ToBase64String($ms.ToArray())"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0 and r.stdout.strip():
                return base64.b64decode(r.stdout.strip())
        except Exception as e:
            logger.debug("[WSLAdapter:_screenshot_ps] %s", e)
        return None

    # ── get_windows_above_bounds: returns [] (inherited from LinuxAdapter) ────
    # ── get_element_tree: upgraded by linux_adapter.install_into if pyatspi ──
    # ── perform_action: inherited (pyautogui; needs DISPLAY) ─────────────────


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
            user32   = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

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

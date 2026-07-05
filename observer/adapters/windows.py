"""
Windows UIA adapter (pywinauto, pywin32, raw COM walker).

Split out of observer.py (P3); behavior is unchanged.
"""

import io
import logging
import traceback
from typing import Any, Dict, List, Optional

from observer.models import (
    Bounds, UIElement, WindowInfo, find_element_by_path, prune_tree_depth,
)

logger = logging.getLogger(__name__)


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

# UIA property IDs (UIAutomationClient.h) used by the raw-COM walker.
_UIA_BOUNDING_RECT   = 30001
_UIA_NAME            = 30005
_UIA_CTRL_TYPE       = 30003
_UIA_ENABLED         = 30010
_UIA_FOCUSED         = 30008
_UIA_VALUE           = 30045
_UIA_ACCESS_KEY      = 30023   # keyboard mnemonic, e.g. "Alt+F"
_UIA_ACCEL_KEY       = 30022   # accelerator, e.g. "Ctrl+Z"
_UIA_HELP_TEXT       = 30013   # tooltip / description
_UIA_AUTOMATION_ID   = 30011
_UIA_RANGE_VALUE     = 30047
_UIA_RANGE_MIN       = 30049
_UIA_RANGE_MAX       = 30050
_UIA_IS_SELECTED     = 30079
_UIA_EXPAND_STATE    = 30084   # 0=collapsed 1=expanded 2=partial 3=leaf
_UIA_SCOPE_CHILDREN  = 0x2

# Properties bulk-fetched via a UIA CacheRequest so each level of the walk is
# one COM round trip (FindAllBuildCache) instead of ~15 per node.
_UIA_CACHED_PROPS = (
    _UIA_BOUNDING_RECT, _UIA_NAME, _UIA_CTRL_TYPE, _UIA_ENABLED,
    _UIA_FOCUSED, _UIA_VALUE, _UIA_ACCESS_KEY, _UIA_ACCEL_KEY,
    _UIA_HELP_TEXT, _UIA_AUTOMATION_ID, _UIA_RANGE_VALUE, _UIA_RANGE_MIN,
    _UIA_RANGE_MAX, _UIA_IS_SELECTED, _UIA_EXPAND_STATE,
)


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
                except Exception as e:
                    logger.debug(f"[occlusion] window rect probe failed "
                                 f"for hwnd {h}: {e}")
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
        # tree.strategy == "uia_only" skips the second pywinauto walk and the
        # synthesis pass entirely — roughly halving capture time — at the cost
        # of the extra native-control properties the merge would contribute.
        strategy = str(self.config.get("tree", {}).get("strategy",
                                                       "merged")).lower()
        uia_tree = self._uia_walk(hwnd)
        if strategy == "uia_only" and uia_tree is not None:
            return uia_tree
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

    def get_element_subtree(self, hwnd=None, element_path: str = "root",
                            max_depth: Optional[int] = None
                            ) -> Optional[UIElement]:
        """Walk only the subtree rooted at *element_path* via raw UIA.

        Navigates the positional child indices of the element-id path
        ('root.3.2' → child 3 → child 2) so only the requested branch is
        traversed.  Falls back to a full walk plus extraction when the path
        contains non-positional segments (synthesized ids) or navigation
        fails."""
        try:
            import win32gui
            if hwnd is None:
                hwnd = win32gui.GetForegroundWindow()
        except Exception as e:
            print(f"[WindowsAdapter:get_element_subtree] win32gui: {e}")
            return None

        if max_depth is None:
            max_depth = self.config.get("tree", {}).get("max_depth", 8)

        indices = self._parse_positional_path(element_path)
        if indices is not None:
            try:
                raw_uia, true_cond = self._uia_handles()
                elem = raw_uia.ElementFromHandle(hwnd)
                for idx in indices:
                    kids = elem.FindAll(_UIA_SCOPE_CHILDREN, true_cond)
                    if idx < 0 or idx >= kids.Length:
                        elem = None
                        break
                    elem = kids.GetElement(idx)
                if elem is not None:
                    return self._uia_walk_element(
                        elem, element_path, 0, max_depth, true_cond,
                        cache_request=self._uia_cache_request(raw_uia))
            except Exception as e:
                logger.debug(f"[WindowsAdapter:get_element_subtree] "
                             f"navigation failed ({e}); falling back")

        # Fallback: full walk + extraction.
        tree = self.get_element_tree(hwnd)
        sub = find_element_by_path(tree, element_path)
        return prune_tree_depth(sub, max_depth)

    @staticmethod
    def _parse_positional_path(element_path: str) -> Optional[List[int]]:
        """'root.3.2' → [3, 2]; None when the path is not purely positional."""
        segs = (element_path or "").split(".")
        if not segs or segs[0] != "root":
            return None
        try:
            return [int(s) for s in segs[1:]]
        except ValueError:
            return None    # synthesized ids like 'root.2.x1'

    def _uia_handles(self):
        """Return (raw IUIAutomation interface, true condition).

        pywinauto already initialised comtypes/UIA at import time; retrieve
        the raw COM interface from its singleton (pywinauto ≥0.6 exposes it
        as .iuia; older versions expose it directly)."""
        from pywinauto.uia_defines import IUIA
        _iuia_obj = IUIA()
        raw_uia = getattr(_iuia_obj, "iuia", _iuia_obj)
        return raw_uia, raw_uia.CreateTrueCondition()

    def _uia_walk(self, hwnd: int) -> Optional[UIElement]:
        """Walk the accessibility tree via raw IUIAutomation COM calls.

        pywinauto's children() falls back to EnumChildWindows for HWND-backed
        elements (e.g. Chrome_RenderWidgetHostHWND), missing all web content.
        Using FindAll(TreeScope_Children) directly on the IUIAutomationElement
        always uses UIA and correctly crosses Chromium fragment boundaries.

        When the COM API supports it, a CacheRequest bulk-fetches the walked
        properties per level (FindAllBuildCache + GetCachedPropertyValue) —
        one cross-process round trip per node's children instead of ~15 per
        node.  Any CacheRequest failure falls back to the per-property path.
        """
        try:
            raw_uia, true_cond = self._uia_handles()
            root = raw_uia.ElementFromHandle(hwnd)
            max_depth = self.config.get("tree", {}).get("max_depth", 8)
            cache_request = self._uia_cache_request(raw_uia)
            return self._uia_walk_element(root, "root", 0, max_depth,
                                          true_cond,
                                          cache_request=cache_request)
        except Exception as e:
            logger.debug(f"[WindowsAdapter:_uia_walk] {e}")
            return None

    @staticmethod
    def _uia_cache_request(raw_uia):
        """Build a CacheRequest covering the properties the walker reads.
        Returns None (per-property fallback) when construction fails."""
        try:
            cr = raw_uia.CreateCacheRequest()
            for pid in _UIA_CACHED_PROPS:
                cr.AddProperty(pid)
            return cr
        except Exception as e:
            logger.debug(f"[WindowsAdapter:_uia_cache_request] CacheRequest "
                         f"unavailable ({e}); using per-property fetches")
            return None

    @staticmethod
    def _uia_prop(elem, pid, default=None, cached=False):
        if cached:
            try:
                v = elem.GetCachedPropertyValue(pid)
                return v if v is not None else default
            except Exception:
                pass    # cache miss/failure — fall back to a live fetch
        try:
            v = elem.GetCurrentPropertyValue(pid)
            return v if v is not None else default
        except Exception:
            return default

    @staticmethod
    def _uia_bounds(elem, cached=False) -> Bounds:
        if cached:
            try:
                r = elem.CachedBoundingRectangle
                return Bounds(r.left, r.top,
                              r.right - r.left, r.bottom - r.top)
            except Exception:
                try:
                    arr = elem.GetCachedPropertyValue(_UIA_BOUNDING_RECT)
                    if arr is not None and len(arr) == 4:
                        # VT_R8 SAFEARRAY: [left, top, width, height]
                        return Bounds(int(arr[0]), int(arr[1]),
                                      int(arr[2]), int(arr[3]))
                except Exception:
                    pass
        try:
            r = elem.CurrentBoundingRectangle
            return Bounds(r.left, r.top, r.right - r.left, r.bottom - r.top)
        except Exception:
            return Bounds(0, 0, 0, 0)

    def _uia_walk_element(self, elem, elem_id: str, depth: int,
                          max_depth: int, true_cond,
                          cache_request=None, cached: bool = False
                          ) -> UIElement:
        """Build a UIElement for *elem* and recurse into its children.

        *cached* means this element was fetched via FindAllBuildCache and its
        properties can be read with GetCachedPropertyValue (no round trip).
        """
        def _prop(pid, default=None):
            return self._uia_prop(elem, pid, default, cached=cached)

        name = _prop(_UIA_NAME, "") or ""
        ctrl = _prop(_UIA_CTRL_TYPE, 0) or 0
        role = _UIA_TYPE_TO_ROLE.get(ctrl, "Pane")
        bounds = self._uia_bounds(elem, cached=cached)
        enabled = bool(_prop(_UIA_ENABLED, True))
        focused = bool(_prop(_UIA_FOCUSED, False))
        value   = _prop(_UIA_VALUE) or None
        # Keyboard shortcut: prefer access key, fall back to accelerator.
        ks = _prop(_UIA_ACCESS_KEY) or _prop(_UIA_ACCEL_KEY) or None
        desc = _prop(_UIA_HELP_TEXT) or None
        aid = _prop(_UIA_AUTOMATION_ID) or None
        # RangeValue pattern: slider / progress / scrollbar
        vn = _prop(_UIA_RANGE_VALUE)
        vmin = _prop(_UIA_RANGE_MIN)
        vmax = _prop(_UIA_RANGE_MAX)
        # SelectionItem.IsSelected: checkbox / radio / tab / menuitem
        sel_raw = _prop(_UIA_IS_SELECTED)
        sel = bool(sel_raw) if sel_raw is not None else None
        # ExpandCollapse: combobox / treeitem / menuitem
        exp_raw = _prop(_UIA_EXPAND_STATE)
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
            kids = None
            kids_cached = False
            if cache_request is not None:
                try:
                    kids = elem.FindAllBuildCache(_UIA_SCOPE_CHILDREN,
                                                  true_cond, cache_request)
                    kids_cached = True
                except Exception:
                    kids = None     # per-node fallback below
            if kids is None:
                try:
                    kids = elem.FindAll(_UIA_SCOPE_CHILDREN, true_cond)
                except Exception:
                    kids = None
            if kids is not None:
                try:
                    for i in range(kids.Length):
                        node.children.append(self._uia_walk_element(
                            kids.GetElement(i), f"{elem_id}.{i}",
                            depth + 1, max_depth, true_cond,
                            cache_request=cache_request,
                            cached=kids_cached))
                except Exception as e:
                    logger.debug(f"[uia] child walk truncated at "
                                 f"{elem_id}: {e}")
        return node

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
            ok = ctypes.windll.user32.PrintWindow(  # type: ignore[attr-defined]
                hwnd, save_dc.GetSafeHdc(), 2)

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

                with mss.MSS() as sct:
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

    def perform_action(self, action: str, element_id: Optional[str] = None,
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

"""
mac_adapter.py — macOS AX tree adapter via pyobjc.

Imports `AppKit`, `Quartz`, `ApplicationServices`, `Cocoa` lazily so this
module is safe to import on non-macOS platforms (the symbols are simply
unavailable until install_into is called).

Plumbed into MacOSAdapter via install_into(observer): replaces the stub
get_element_tree implementation when pyobjc is available.

UNTESTED on this CI Linux machine — the implementation follows canonical
pyobjc patterns (AXUIElementCreateApplication / AXUIElementCopyAttributeValue
/ kAXChildrenAttribute) and should be exercised on a real macOS machine.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def is_supported() -> bool:
    try:
        import AppKit  # noqa: F401
        import ApplicationServices  # noqa: F401
        import Quartz  # noqa: F401
        return True
    except Exception:
        return False


def install_into(observer: Any) -> bool:
    """Replace MacOSAdapter.get_element_tree with the real implementation."""
    if not is_supported():
        return False
    from observer import MacOSAdapter, Bounds, UIElement, WindowInfo
    adapter = getattr(observer, "_adapter", None)
    if not isinstance(adapter, MacOSAdapter):
        return False

    import ApplicationServices as AS
    import AppKit
    import Quartz

    AXUIElementCreateApplication = AS.AXUIElementCreateApplication
    AXUIElementCopyAttributeValue = AS.AXUIElementCopyAttributeValue
    AX_CHILDREN = "AXChildren"
    AX_ROLE = "AXRole"
    AX_TITLE = "AXTitle"
    AX_VALUE = "AXValue"
    AX_DESCRIPTION = "AXDescription"
    AX_POSITION = "AXPosition"
    AX_SIZE = "AXSize"
    AX_ENABLED = "AXEnabled"
    AX_FOCUSED = "AXFocused"
    AX_MIN_VALUE  = "AXMinValue"
    AX_MAX_VALUE  = "AXMaxValue"
    AX_SELECTED   = "AXSelected"
    AX_EXPANDED   = "AXExpanded"
    AX_IDENTIFIER = "AXIdentifier"

    def _attr(elem, name):
        try:
            err, val = AXUIElementCopyAttributeValue(elem, name, None)
            if err != 0:
                return None
            return val
        except Exception:
            return None

    def _bounds(elem) -> Bounds:
        pos = _attr(elem, AX_POSITION)
        size = _attr(elem, AX_SIZE)
        try:
            x = int(pos.x)
            y = int(pos.y)
            w = int(size.width)
            h = int(size.height)
            return Bounds(x, y, w, h)
        except Exception:
            return Bounds(0, 0, 0, 0)

    def _walk(elem, eid: str, depth: int, max_depth: int) -> "UIElement":
        role = str(_attr(elem, AX_ROLE) or "Unknown")
        name = str(_attr(elem, AX_TITLE) or "")
        value_raw = _attr(elem, AX_VALUE)
        value = str(value_raw) if value_raw is not None else None
        desc = _attr(elem, AX_DESCRIPTION)
        bounds = _bounds(elem)
        # Extended a11y signals — None when AX does not expose the attribute.
        def _opt_float(a):
            v = _attr(elem, a)
            try:
                return float(v) if v is not None else None
            except Exception:
                return None

        def _opt_bool(a):
            v = _attr(elem, a)
            return bool(v) if v is not None else None

        ident = _attr(elem, AX_IDENTIFIER)
        # AXValue doubles as a numeric for sliders / progress; try to parse.
        try:
            value_now = float(value_raw) if value_raw is not None else None
        except Exception:
            value_now = None

        ui = UIElement(
            element_id=eid, name=name, role=role, value=value,
            bounds=bounds,
            enabled=bool(_attr(elem, AX_ENABLED) or True),
            focused=bool(_attr(elem, AX_FOCUSED) or False),
            description=str(desc) if desc else None,
            selected=_opt_bool(AX_SELECTED),
            expanded=_opt_bool(AX_EXPANDED),
            value_now=value_now,
            value_min=_opt_float(AX_MIN_VALUE),
            value_max=_opt_float(AX_MAX_VALUE),
            identifier=str(ident) if ident else None,
        )
        if depth >= max_depth:
            return ui
        children = _attr(elem, AX_CHILDREN) or []
        for i, c in enumerate(children):
            ui.children.append(_walk(c, f"{eid}.{i}", depth + 1, max_depth))
        return ui

    def get_element_tree(hwnd=None) -> Optional[UIElement]:
        try:
            ws = AppKit.NSWorkspace.sharedWorkspace()
            running = ws.runningApplications()
            target = None
            if hwnd is not None:
                # macOS uses kCGWindowNumber or pid as handle.
                for app in running:
                    if int(app.processIdentifier()) == int(hwnd):
                        target = app
                        break
            if target is None:
                target = ws.frontmostApplication()
            if target is None:
                return None
            ax_app = AXUIElementCreateApplication(int(target.processIdentifier()))
            max_depth = adapter.config.get("tree", {}).get("max_depth", 8)
            return _walk(ax_app, "root", 0, max_depth)
        except Exception:
            logger.exception("mac_adapter.get_element_tree failed")
            return None

    def list_windows() -> List[WindowInfo]:
        try:
            options = (Quartz.kCGWindowListOptionOnScreenOnly |
                       Quartz.kCGWindowListExcludeDesktopElements)
            wins = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
            out: List[WindowInfo] = []
            front_pid = int(AppKit.NSWorkspace.sharedWorkspace()
                            .frontmostApplication().processIdentifier())
            for w in wins or []:
                title = w.get("kCGWindowName") or ""
                owner = w.get("kCGWindowOwnerName") or ""
                if not title and not owner:
                    continue
                bounds = w.get("kCGWindowBounds") or {}
                pid = int(w.get("kCGWindowOwnerPID", 0))
                wnum = int(w.get("kCGWindowNumber", 0))
                out.append(WindowInfo(
                    handle=pid, title=str(title or owner),
                    process_name=str(owner), pid=pid,
                    bounds=Bounds(int(bounds.get("X", 0)),
                                   int(bounds.get("Y", 0)),
                                   int(bounds.get("Width",  0)),
                                   int(bounds.get("Height", 0))),
                    is_focused=(pid == front_pid),
                    window_uid=f"mac:{wnum}",
                ))
            return out
        except Exception:
            logger.exception("mac_adapter.list_windows failed")
            return []

    def get_windows_above_bounds(hwnd) -> List[Bounds]:
        try:
            options = (Quartz.kCGWindowListOptionOnScreenAboveWindow |
                       Quartz.kCGWindowListExcludeDesktopElements)
            wins = Quartz.CGWindowListCopyWindowInfo(
                options, int(hwnd) if hwnd is not None else Quartz.kCGNullWindowID)
            out: List[Bounds] = []
            for w in wins or []:
                b = w.get("kCGWindowBounds") or {}
                if int(b.get("Width", 0)) > 0 and int(b.get("Height", 0)) > 0:
                    out.append(Bounds(int(b.get("X", 0)), int(b.get("Y", 0)),
                                       int(b.get("Width",  0)),
                                       int(b.get("Height", 0))))
            return out
        except Exception:
            return []

    adapter.get_element_tree = get_element_tree                 # type: ignore[assignment]
    adapter.list_windows = list_windows                         # type: ignore[assignment]
    adapter.get_windows_above_bounds = get_windows_above_bounds  # type: ignore[assignment]
    return True

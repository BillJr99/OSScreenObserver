"""
linux_adapter.py — Linux AT-SPI accessibility tree adapter via pyatspi.

Imports `pyatspi` lazily so this module is safe to import on non-Linux
platforms (or when AT-SPI isn't installed).  Plumbed into LinuxAdapter via
install_into(observer): replaces the stub get_element_tree with a real
walk over pyatspi.Registry.getDesktop(0).

UNTESTED on this CI Linux machine — pyatspi requires a desktop session
plus an a11y bridge running. The implementation follows canonical patterns
from the GNOME accessibility documentation.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def is_supported() -> bool:
    try:
        import pyatspi  # noqa: F401
        return True
    except Exception:
        return False


def install_into(observer: Any) -> bool:
    if not is_supported():
        return False
    from observer import LinuxAdapter, Bounds, UIElement, WindowInfo
    adapter = getattr(observer, "_adapter", None)
    if not isinstance(adapter, LinuxAdapter):
        return False
    import pyatspi

    def _bounds(node) -> Bounds:
        try:
            comp = node.queryComponent()
            ext = comp.getExtents(pyatspi.DESKTOP_COORDS)
            return Bounds(ext.x, ext.y, ext.width, ext.height)
        except Exception:
            return Bounds(0, 0, 0, 0)

    def _walk(node, eid: str, depth: int, max_depth: int) -> "UIElement":
        try:
            role = node.getRoleName() or "Unknown"
        except Exception:
            role = "Unknown"
        try:
            name = node.name or ""
        except Exception:
            name = ""
        value: Optional[str] = None
        try:
            value = node.queryText().getText(0, -1)
        except Exception:
            try:
                v = node.queryValue()
                value = str(v.currentValue)
            except Exception:
                value = None
        try:
            desc = node.description or None
        except Exception:
            desc = None
        try:
            states = node.getState().getStates()
            enabled = pyatspi.STATE_ENABLED in states
            focused = pyatspi.STATE_FOCUSED in states
            selected = (pyatspi.STATE_SELECTED in states
                        or pyatspi.STATE_CHECKED in states)
            expanded = pyatspi.STATE_EXPANDED in states if (
                pyatspi.STATE_EXPANDABLE in states) else None
            # STATE_SELECTED on non-selectable widgets is meaningless; only
            # set the flag when SELECTABLE / CHECKABLE is present too.
            if not (pyatspi.STATE_SELECTABLE in states
                    or pyatspi.STATE_CHECKABLE in states):
                selected = None
        except Exception:
            enabled = True
            focused = False
            selected = None
            expanded = None

        # AT-SPI IValue exposes numeric current/min/max for sliders & progress.
        value_now = value_min = value_max = None
        try:
            iv = node.queryValue()
            value_now = float(iv.currentValue)
            value_min = float(iv.minimumValue)
            value_max = float(iv.maximumValue)
        except Exception:
            pass

        ui = UIElement(
            element_id=eid, name=name, role=role, value=value,
            bounds=_bounds(node), enabled=enabled, focused=focused,
            description=desc,
            selected=selected, expanded=expanded,
            value_now=value_now, value_min=value_min, value_max=value_max,
        )
        if depth >= max_depth:
            return ui
        try:
            for i in range(node.childCount):
                ui.children.append(_walk(node[i], f"{eid}.{i}",
                                          depth + 1, max_depth))
        except Exception:
            pass
        return ui

    def get_element_tree(hwnd=None) -> Optional[UIElement]:
        try:
            desktop = pyatspi.Registry.getDesktop(0)
        except Exception:
            return None
        max_depth = adapter.config.get("tree", {}).get("max_depth", 8)
        # Find the active window.  Walk applications and pick the first frame
        # with state ACTIVE; otherwise return the desktop subtree itself.
        target = None
        try:
            for app_idx in range(desktop.childCount):
                app = desktop[app_idx]
                for w_idx in range(app.childCount):
                    win = app[w_idx]
                    try:
                        if pyatspi.STATE_ACTIVE in win.getState().getStates():
                            target = win
                            break
                    except Exception:
                        continue
                if target:
                    break
        except Exception:
            target = None
        if target is None:
            target = desktop
        return _walk(target, "root", 0, max_depth)

    def list_windows() -> List[WindowInfo]:
        # Continue using wmctrl for window enumeration; pyatspi gives us the
        # tree but doesn't expose a stable window handle suitable for actions.
        return type(adapter).list_windows(adapter)

    adapter.get_element_tree = get_element_tree    # type: ignore[assignment]
    return True

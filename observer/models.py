"""
Data model: Bounds, UIElement, WindowInfo, WindowResolution
plus subtree helpers (find_element_by_path, prune_tree_depth).

Split out of observer.py (P3); behavior is unchanged.
"""

from dataclasses import dataclass, field, replace as _dc_replace
from typing import Any, Dict, List, Optional


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


# ─── Subtree helpers (P1 perf: scoped drill-in) ──────────────────────────────

def find_element_by_path(root: Optional[UIElement],
                         element_path: str) -> Optional[UIElement]:
    """Locate an element by its positional element-id path (e.g. 'root.3.2').

    Prefers walking the id prefixes level by level (cheap); falls back to a
    full DFS by exact id for trees whose ids are not strictly positional
    (e.g. nodes injected by tree synthesis get ids like 'root.2.x1')."""
    if root is None or not element_path:
        return None
    if root.element_id == element_path:
        return root
    # Fast path: navigate children whose ids extend the current prefix.
    if element_path.startswith(root.element_id + "."):
        node = root
        prefix = root.element_id
        rest = element_path[len(prefix) + 1:]
        found = True
        for seg in rest.split("."):
            prefix = f"{prefix}.{seg}"
            nxt = next((c for c in node.children if c.element_id == prefix),
                       None)
            if nxt is None:
                found = False
                break
            node = nxt
        if found:
            return node
    # Fallback: exhaustive search by exact id.
    stack = [root]
    while stack:
        e = stack.pop()
        if e.element_id == element_path:
            return e
        stack.extend(e.children)
    return None


def prune_tree_depth(elem: Optional[UIElement],
                     max_depth: Optional[int]) -> Optional[UIElement]:
    """Return a copy of *elem* limited to *max_depth* levels below it.

    Nodes are shallow-copied (bounds objects are shared) so the original —
    possibly cache-resident — tree is never mutated."""
    if elem is None or max_depth is None:
        return elem

    def _copy(e: UIElement, depth: int) -> UIElement:
        kids = ([] if depth >= max_depth
                else [_copy(c, depth + 1) for c in e.children])
        return _dc_replace(e, children=kids)

    return _copy(elem, 0)

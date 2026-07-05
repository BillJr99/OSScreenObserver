"""
Occlusion / visibility: visible areas, screen bounds and
rectangle geometry helpers (mixin consumed by ScreenObserver).

Split out of observer.py (P3); behavior is unchanged.
"""

from typing import Any, Dict, List, Optional

from observer.models import Bounds, WindowInfo


class OcclusionMixin:
    """Occlusion / visibility methods of ScreenObserver."""

    # Provided by the concrete ScreenObserver.
    _adapter: Any
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
            with mss.MSS() as sct:
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

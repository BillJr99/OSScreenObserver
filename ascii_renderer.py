"""
ascii_renderer.py — ASCII spatial layout sketch generator.

Converts an accessibility element tree into a character-grid representation
that preserves the spatial geometry of the original UI. Elements are drawn
as labeled boxes using Unicode box-drawing characters (or plain ASCII as a
fallback), with bounding rectangle coordinates normalized from screen pixels
to grid cells via a simple linear scale.

Design rationale
----------------
The key insight is that accessibility APIs already provide bounding rectangles
for every element; the rendering problem is therefore a coordinate projection
problem, not an image processing problem.  We project (screen_x, screen_y)
→ (grid_col, grid_row) with:

    grid_col = floor((screen_x - ref.x) * grid_width  / ref.width )
    grid_row = floor((screen_y - ref.y) * grid_height / ref.height)

Elements are drawn in DFS order (parent before child) so that child boxes
visually overlay parent containers, matching the z-ordering of real UIs.
Elements whose grid footprint is too small to hold a label are assigned a
short identifier and collected in an appended legend.
"""

import logging
import traceback
from typing import Dict, List, Optional, Tuple

from observer import Bounds, UIElement

logger = logging.getLogger(__name__)

# ─── Box character sets ───────────────────────────────────────────────────────

_UNICODE_BOX: Dict[str, str] = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
    "h":  "─", "v":  "│",
}

_ASCII_BOX: Dict[str, str] = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "h":  "-", "v":  "|",
}


def _legend_key(n: int) -> str:
    """Map integer → short legend key: A, B, …, Z, A1, B1, …"""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return letters[n % 26] + (str(n // 26) if n >= 26 else "")


# ─── Main renderer ────────────────────────────────────────────────────────────

class ASCIIRenderer:
    """Renders a UIElement tree as a spatial ASCII sketch."""

    def __init__(self, config: dict):
        sketch_cfg = config.get("ascii_sketch", {})
        self.default_width  = sketch_cfg.get("grid_width",  110)
        self.default_height = sketch_cfg.get("grid_height",  38)
        self.box = _UNICODE_BOX if sketch_cfg.get("unicode_box", True) else _ASCII_BOX

    # ── public entry point ────────────────────────────────────────────────────

    def render(
        self,
        root:         UIElement,
        screen_bounds: Optional[Bounds] = None,
        grid_width:    Optional[int]    = None,
        grid_height:   Optional[int]    = None,
    ) -> str:
        """
        Render *root* and its descendants as an ASCII layout sketch.

        Parameters
        ----------
        root          : Root element of the tree to render.
        screen_bounds : Reference rectangle in screen coordinates. Defaults to
                        root.bounds when not supplied.
        grid_width    : Output grid width in characters (overrides config).
        grid_height   : Output grid height in characters (overrides config).

        Returns
        -------
        Multi-line string containing the ASCII sketch, optionally followed by
        a legend table for elements that were too small to hold an inline label.
        """
        try:
            gw = grid_width  or self.default_width
            gh = grid_height or self.default_height

            # Reference rectangle: defines what portion of screen space we map.
            ref = screen_bounds or root.bounds
            if not ref:
                ref = Bounds(root.bounds.x, root.bounds.y,
                             max(root.bounds.width, 1), max(root.bounds.height, 1))
            rw = max(ref.width,  1)
            rh = max(ref.height, 1)

            # Character grid: list-of-lists for mutability.
            grid: List[List[str]] = [[" "] * gw for _ in range(gh)]

            legend: Dict[str, str] = {}
            counter = [0]

            # ── coordinate helpers ────────────────────────────────────────────

            def to_gx(px: int) -> int:
                return max(0, min(gw - 1, int((px - ref.x) * gw / rw)))

            def to_gy(py: int) -> int:
                return max(0, min(gh - 1, int((py - ref.y) * gh / rh)))

            # ── draw one element ──────────────────────────────────────────────

            def draw(elem: UIElement) -> None:
                b = elem.bounds
                if b.width < 1 or b.height < 1:
                    return

                gx1, gy1 = to_gx(b.x),     to_gy(b.y)
                gx2, gy2 = to_gx(b.right),  to_gy(b.bottom)

                # Minimum 2-wide, 2-tall box so borders don't collapse.
                gx2 = min(max(gx1 + 2, gx2), gw - 1)
                gy2 = min(max(gy1 + 2, gy2), gh - 1)

                if gx1 >= gw or gy1 >= gh or gx2 <= gx1 or gy2 <= gy1:
                    return

                bx = self.box

                # Corners
                grid[gy1][gx1] = bx["tl"]
                grid[gy1][gx2] = bx["tr"]
                grid[gy2][gx1] = bx["bl"]
                grid[gy2][gx2] = bx["br"]

                # Top & bottom edges (don't overwrite corners)
                for x in range(gx1 + 1, gx2):
                    if grid[gy1][x] in (" ", bx["h"]):
                        grid[gy1][x] = bx["h"]
                    if grid[gy2][x] in (" ", bx["h"]):
                        grid[gy2][x] = bx["h"]

                # Left & right edges
                for y in range(gy1 + 1, gy2):
                    if grid[y][gx1] in (" ", bx["v"]):
                        grid[y][gx1] = bx["v"]
                    if grid[y][gx2] in (" ", bx["v"]):
                        grid[y][gx2] = bx["v"]

                # Compose label
                label = _compose_label(elem)

                inner_w = gx2 - gx1 - 2   # columns available inside the box
                inner_y = gy1 + 1          # first row inside the top border

                if inner_w >= 3 and inner_y < gy2:
                    # Write label on the first interior row
                    text = label[:inner_w]
                    for i, ch in enumerate(text):
                        cx = gx1 + 1 + i
                        if cx < gx2 and grid[inner_y][cx] == " ":
                            grid[inner_y][cx] = ch
                else:
                    # Too small: assign a legend key
                    key = _legend_key(counter[0])
                    counter[0] += 1
                    legend[key] = label
                    cy = (gy1 + gy2) // 2
                    cx = (gx1 + gx2) // 2
                    for i, ch in enumerate(key[:2]):
                        if cx + i < gx2 and grid[cy][cx + i] == " ":
                            grid[cy][cx + i] = ch

            # ── DFS traversal: parent before children ─────────────────────────

            def draw_tree(elem: UIElement) -> None:
                draw(elem)
                for child in elem.children:
                    draw_tree(child)

            draw_tree(root)

            # ── Serialise grid → string ───────────────────────────────────────

            lines = ["".join(row).rstrip() for row in grid]
            while lines and not lines[-1].strip():
                lines.pop()
            result = "\n".join(lines)

            if legend:
                result += "\n\n  LEGEND\n  " + "─" * 50
                for key, label in legend.items():
                    result += f"\n  {key:>4}  {label}"

            return result

        except Exception as e:
            print(f"[ASCIIRenderer:render] {e}")
            traceback.print_exc()
            return f"[ASCII render error: {e}]"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _compose_label(elem: UIElement) -> str:
    """Build the text label that appears inside an element's box."""
    parts = [elem.role]

    if elem.name:
        # Truncate very long names
        n = elem.name if len(elem.name) <= 30 else elem.name[:27] + "…"
        parts.append(f'"{n}"')

    if elem.value is not None:
        v = elem.value if len(elem.value) <= 20 else elem.value[:17] + "…"
        parts.append(f"[{v}]")

    badges: List[str] = []
    if elem.focused:
        badges.append("●")
    if not elem.enabled:
        badges.append("✗")
    if elem.keyboard_shortcut:
        badges.append(f"⌨{elem.keyboard_shortcut}")

    label = " ".join(parts)
    if badges:
        label += " " + " ".join(badges)
    return label

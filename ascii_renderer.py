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

OCR overlay (optional)
----------------------
When screenshot bytes are supplied to render(), a second pass runs Tesseract
word-level OCR on the image.  Each recognised word is projected into grid
coordinates and written into any grid cells that are still blank (space char)
at that position.  This fills in text content for elements that have no
accessibility-tree label — canvas widgets, custom renderers, image buttons,
status bars with live counters, etc.

Multi-line labels
-----------------
When a box has enough interior rows the label is split across lines:
  row 1 — role  [+ state badges: ● focused  ✗ disabled  ⌨ shortcut]
  row 2 — "name" (quoted, truncated to fit)
  row 3+ — value excerpt (first characters, word-wrapped to remaining rows)
"""

import logging
import textwrap
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


# ─── OCR helper ───────────────────────────────────────────────────────────────

def _ocr_words(screenshot_bytes: bytes) -> List[Tuple[int, int, int, int, str]]:
    """
    Run Tesseract on *screenshot_bytes* and return a list of
    (left, top, width, height, text) tuples for every word whose
    confidence is ≥ 30.  Returns an empty list when pytesseract is
    unavailable or the image cannot be decoded.
    """
    try:
        import io as _io

        import pytesseract
        from PIL import Image

        img  = Image.open(_io.BytesIO(screenshot_bytes)).convert("RGB")
        data = pytesseract.image_to_data(
            img,
            output_type=pytesseract.Output.DICT,
            # PSM 11: sparse text — detect text at any position
            # OEM 3: best available engine (LSTM + legacy)
            config="--psm 11 --oem 3",
        )
        words: List[Tuple[int, int, int, int, str]] = []
        for i in range(len(data["text"])):
            conf = int(data["conf"][i])
            text = str(data["text"][i]).strip()
            if conf >= 30 and text:
                words.append((
                    int(data["left"][i]),
                    int(data["top"][i]),
                    int(data["width"][i]),
                    int(data["height"][i]),
                    text,
                ))
        return words
    except Exception:
        return []


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
        root:              UIElement,
        screen_bounds:     Optional[Bounds] = None,
        grid_width:        Optional[int]    = None,
        grid_height:       Optional[int]    = None,
        screenshot_bytes:  Optional[bytes]  = None,
    ) -> str:
        """
        Render *root* and its descendants as an ASCII layout sketch.

        Parameters
        ----------
        root              : Root element of the tree to render.
        screen_bounds     : Reference rectangle in screen coordinates.
                            Defaults to root.bounds when not supplied.
        grid_width        : Output grid width in characters (overrides config).
        grid_height       : Output grid height in characters (overrides config).
        screenshot_bytes  : Optional raw PNG bytes for the window screenshot.
                            When provided, a second pass runs Tesseract OCR and
                            overlays recognised text into blank grid cells,
                            significantly improving fidelity for UI elements
                            that the accessibility tree does not describe.

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

                # Interior cells run from gx1+1 to gx2-1 (inclusive), so count = gx2-gx1-1.
                inner_w = gx2 - gx1 - 1   # columns available inside the box
                inner_h = gy2 - gy1 - 1   # rows available inside the box

                if inner_w < 1:
                    # Box is too narrow to fit anything (shouldn't happen given min-size above)
                    return

                if inner_h < 1 or inner_w < 3:
                    # Single interior row or very narrow: use legend
                    key = _legend_key(counter[0])
                    counter[0] += 1
                    full_label = _compose_label(elem)
                    legend[key] = full_label
                    cy = (gy1 + gy2) // 2
                    cx = (gx1 + gx2) // 2
                    for i, ch in enumerate(key[:2]):
                        if cx + i < gx2 and grid[cy][cx + i] == " ":
                            grid[cy][cx + i] = ch
                    return

                # Build multi-line label lines
                label_lines = _compose_label_multiline(elem, inner_w, inner_h)

                for row_idx, line in enumerate(label_lines):
                    gy = gy1 + 1 + row_idx
                    if gy >= gy2:
                        break
                    for col_idx, ch in enumerate(line):
                        cx = gx1 + 1 + col_idx
                        if cx < gx2 and grid[gy][cx] == " ":
                            grid[gy][cx] = ch

            # ── DFS traversal: parent before children ─────────────────────────

            def draw_tree(elem: UIElement) -> None:
                draw(elem)
                for child in elem.children:
                    draw_tree(child)

            draw_tree(root)

            # ── OCR overlay pass ──────────────────────────────────────────────
            # Project recognised words into grid cells. Only blank cells are
            # written so that accessibility labels and box borders are preserved.

            if screenshot_bytes:
                words = _ocr_words(screenshot_bytes)
                for wx, wy, ww, wh, text in words:
                    # Screenshot coords are window-relative (i.e. already offset
                    # by the window's top-left corner, which is ref.x / ref.y).
                    # The to_gx/to_gy helpers subtract ref.x/ref.y, so we add
                    # them back here to stay in screen-absolute space.
                    sx = wx + ref.x
                    sy = wy + ref.y
                    gx = to_gx(sx)
                    gy = to_gy(sy)

                    # Write each character of the word horizontally
                    for i, ch in enumerate(text):
                        cx = gx + i
                        if 0 <= cx < gw and 0 <= gy < gh and grid[gy][cx] == " ":
                            grid[gy][cx] = ch

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

def _state_badges(elem: UIElement) -> str:
    """Return a compact string of state badges for an element."""
    parts: List[str] = []
    if elem.focused:
        parts.append("●")
    if not elem.enabled:
        parts.append("✗")
    if elem.keyboard_shortcut:
        parts.append(f"⌨{elem.keyboard_shortcut}")
    return " ".join(parts)


def _compose_label(elem: UIElement) -> str:
    """Build a single-line text label (used for legend entries and fallback)."""
    parts = [elem.role]

    if elem.name:
        n = elem.name if len(elem.name) <= 30 else elem.name[:27] + "…"
        parts.append(f'"{n}"')

    if elem.value is not None:
        v = elem.value if len(elem.value) <= 20 else elem.value[:17] + "…"
        parts.append(f"[{v}]")

    label = " ".join(parts)
    badges = _state_badges(elem)
    if badges:
        label += " " + badges
    return label


def _compose_label_multiline(elem: UIElement, inner_w: int, inner_h: int) -> List[str]:
    """
    Build a list of strings, one per interior row, that together describe
    *elem* with the highest fidelity that fits within *inner_w* × *inner_h*.

    Layout strategy
    ---------------
    Row 0  — role  [badges]             (always present)
    Row 1  — "name" (quoted, truncated)  (when inner_h ≥ 2 and name present)
    Row 2+ — value text, word-wrapped    (when remaining rows > 0 and value present)
    """
    lines: List[str] = []

    # ── Row 0: role + state badges ────────────────────────────────────────────
    badges = _state_badges(elem)
    role_line = elem.role
    if badges:
        candidate = f"{elem.role} {badges}"
        role_line = candidate if len(candidate) <= inner_w else elem.role[:inner_w]
    else:
        role_line = elem.role[:inner_w]
    lines.append(role_line)

    remaining = inner_h - 1   # rows left after role line

    # ── Row 1: name ───────────────────────────────────────────────────────────
    if remaining > 0 and elem.name:
        quoted = f'"{elem.name}"'
        if len(quoted) <= inner_w:
            lines.append(quoted)
        else:
            # Truncate but keep the closing quote so it reads as a string
            lines.append(f'"{elem.name[:inner_w - 2]}…"'[:inner_w])
        remaining -= 1

    # ── Rows 2+: value (word-wrapped across remaining interior rows) ──────────
    if remaining > 0 and elem.value is not None:
        val = elem.value.strip()
        if val:
            # Flatten multi-line values to a single line for wrapping
            val_flat = " ".join(val.splitlines())
            wrapped  = textwrap.wrap(val_flat, width=inner_w)
            for wline in wrapped[:remaining]:
                lines.append(wline[:inner_w])

    return lines

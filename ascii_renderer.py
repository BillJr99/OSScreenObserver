"""
ascii_renderer.py — High-fidelity text "sketch" generator for UI windows.

Combines two information sources:

  1. The accessibility element tree (UIElement) — provides bounding boxes,
     roles, names, values, focus / enabled / selected / expanded state,
     and numeric range info (slider thumb, progress fill).
  2. A Tesseract (or pluggable) OCR pass over the window screenshot —
     fills in text that the a11y tree does not describe (custom canvases,
     image buttons, live status fields).

Fidelity features (all gated by config; defaults preserve prior behavior):

  • Role-aware glyphs:   `[x] Word Wrap`, `(•) Option B`, `▼ combobox`,
                         `├──●────┤ 40%` for sliders, `▓▓░░ 50%` for
                         progress bars.
  • Tab-order numerals:  `①②③ …` written into the top-left interior of
                         each focusable element so agents can target by
                         focus stop.
  • Landmark headers:    container roles (Toolbar, StatusBar, Dialog, …)
                         get their role/name baked into the top border.
  • Legend keys inline:  legend identifiers are written into the top-right
                         interior of every element, not just the bottom
                         table — agents can refer to small widgets by key
                         without scrolling.
  • Occlusion pruning:   later same-parent siblings (modals, popovers)
                         hide earlier siblings they fully cover, matching
                         what a human sees on the actual screen.
  • OCR line grouping:   Tesseract output is grouped by (block, par, line)
                         and clipped to each line's own bounding box, so
                         a long word can never spill into a neighbor.
  • Confidence-weighted: a parallel confidence grid lets higher-confidence
                         OCR overwrite earlier low-confidence text;
                         box-border characters are always protected.
  • Pre-processing:      ×2 Lanczos upscale + grayscale + autocontrast
                         before OCR substantially improves recognition of
                         small UI fonts.
  • ROI re-OCR:          unlabeled `image` / `custom` / `group` / `pane`
                         elements get cropped, upscaled ×3, and re-OCR'd
                         at PSM 7/6 to recover their text.
  • Optional VLM fallback for elements that survive ROI-OCR with no
                         recovered text (gated; off by default).
  • Structured sidecar:  render_structured() returns the grid alongside
                         a flat list of element records with grid + screen
                         bounds, state, ocr_text, tab_index and legend_key
                         — the form agents actually plan against.

Coordinate model
----------------
Screen-pixel coords map to grid cells with a simple linear scale:

    grid_col = floor((screen_x - ref.x) * grid_width  / ref.width )
    grid_row = floor((screen_y - ref.y) * grid_height / ref.height)

The screenshot passed to render() is window-cropped (the call sites in
web_inspector.py and mcp_server.py crop the full-display PNG before
forwarding), so screenshot-pixel coords are window-relative and the OCR
overlay adds ref.x / ref.y back before projection.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import textwrap
import traceback
import urllib.request
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from observer import Bounds, UIElement

if TYPE_CHECKING:
    # PIL is an optional runtime dependency — every function that touches
    # it imports lazily inside its body so the module remains importable
    # without Pillow installed. The forward-ref annotations below are
    # type-only and resolved by tools (mypy / ruff) via this guard.
    from PIL import Image  # noqa: F401

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

_BORDER_CHARS = frozenset("┌┐└┘─│+-|")

# ─── Tab-order numerals (1-20 circled, then plain) ───────────────────────────

_CIRCLED = [
    "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
]

def _tab_glyph(idx: int) -> str:
    """1-based focus-stop index → display glyph."""
    if 1 <= idx <= len(_CIRCLED):
        return _CIRCLED[idx - 1]
    return f"#{idx}"


# ─── Role normalization & focusability ───────────────────────────────────────

def _norm_role(role: str) -> str:
    """Lowercase, strip whitespace/underscores. Different adapters spell
    roles differently (UIA: 'CheckBox'; AT-SPI: 'check_box'; AX:
    'AXCheckBox'); we normalize once for lookup."""
    s = (role or "").strip().lower().lstrip("ax")
    return s.replace("_", "").replace("-", "").replace(" ", "")

_FOCUSABLE_ROLES = frozenset({
    "button", "checkbox", "radiobutton", "radio", "combobox", "menuitem",
    "tab", "textfield", "edit", "editabletext", "text", "link",
    "hyperlink", "slider", "spinner", "spinbutton",
})

_LANDMARK_ROLES = {
    "toolbar":   "Toolbar",
    "menubar":   "MenuBar",
    "tabpanel":  "TabPanel",
    "sidebar":   "Sidebar",
    "statusbar": "StatusBar",
    "dialog":    "Dialog",
    "alertdialog": "Dialog",
    "navigation": "Navigation",
    "banner":    "Banner",
}

_RANGE_ROLES = frozenset({"slider", "scrollbar", "progressbar"})


# ─── Value parsing helpers ───────────────────────────────────────────────────

def _percent_from_elem(elem: UIElement) -> Optional[float]:
    """Return a 0..1 fraction representing the element's current value.

    Prefers explicit numeric range (value_now/min/max). Falls back to
    parsing common string formats in `value`: "40%", "0.4", "3 of 10".
    Returns None when no fraction can be determined.
    """
    if (elem.value_now is not None
            and elem.value_min is not None
            and elem.value_max is not None
            and elem.value_max > elem.value_min):
        return max(0.0, min(1.0,
            (elem.value_now - elem.value_min)
            / (elem.value_max - elem.value_min)))
    v = (elem.value or "").strip()
    if not v:
        return None
    try:
        if v.endswith("%"):
            return max(0.0, min(1.0, float(v[:-1].strip()) / 100.0))
        if " of " in v:
            n, d = v.split(" of ", 1)
            return max(0.0, min(1.0, float(n.strip()) / float(d.strip())))
        f = float(v)
        if 0.0 <= f <= 1.0:
            return f
        if 0.0 <= f <= 100.0:
            return f / 100.0
    except Exception:
        return None
    return None


# ─── Role-specific glyph rendering ───────────────────────────────────────────

def _role_glyph_row(elem: UIElement, inner_w: int) -> Optional[str]:
    """Compact one-line representation for known control roles. Returns
    None when the element has no role-specific glyph."""
    r = _norm_role(elem.role)
    name = (elem.name or "").strip()

    if r in ("checkbox", "togglebutton", "switch"):
        mark = "x" if elem.selected else " "
        return f"[{mark}] {name}"[:inner_w] if name else f"[{mark}]"

    if r in ("radiobutton", "radio", "radioitem"):
        mark = "•" if elem.selected else " "
        return f"({mark}) {name}"[:inner_w] if name else f"({mark})"

    if r in ("combobox", "dropdownbutton", "popupbutton"):
        arrow = "▼" if elem.expanded else "▶"
        body = f"{arrow} {name}" if name else arrow
        if elem.value:
            body = f"{body} [{elem.value}]"
        return body[:inner_w]

    if r in ("menuitem",):
        # Cascade arrow when expandable.
        arrow = "▸" if elem.expanded else ""
        body = (arrow + " " + name).strip()
        return body[:inner_w] if body else None

    if r in ("slider", "scrollbar"):
        frac = _percent_from_elem(elem)
        if frac is None:
            return None
        # Bar of inner_w characters: ├──●────┤
        bar_w = max(5, inner_w - 6)  # leave room for "100%"
        pos = int(round(frac * (bar_w - 1)))
        chars = ["─"] * bar_w
        chars[0] = "├"
        chars[-1] = "┤"
        chars[pos] = "●"
        return ("".join(chars) + f" {int(round(frac * 100))}%")[:inner_w]

    if r == "progressbar":
        frac = _percent_from_elem(elem)
        if frac is None:
            return None
        bar_w = max(4, inner_w - 5)  # leave room for " 99%"
        filled = int(round(frac * bar_w))
        bar = ("▓" * filled) + ("░" * (bar_w - filled))
        return (bar + f" {int(round(frac * 100))}%")[:inner_w]

    return None


# ─── State badges (focused / disabled / shortcut) ────────────────────────────

def _state_badges(elem: UIElement) -> str:
    parts: List[str] = []
    if elem.focused:
        parts.append("●")
    if not elem.enabled:
        parts.append("✗")
    if elem.keyboard_shortcut:
        parts.append(f"⌨{elem.keyboard_shortcut}")
    return " ".join(parts)


def _legend_key(n: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return letters[n % 26] + (str(n // 26) if n >= 26 else "")


def _compose_label(elem: UIElement) -> str:
    """Single-line label for the legend table and tight fallback."""
    parts = [elem.role]
    if elem.name:
        n = elem.name if len(elem.name) <= 30 else elem.name[:27] + "…"
        parts.append(f'"{n}"')
    if elem.value is not None and not _RANGE_ROLES.intersection({_norm_role(elem.role)}):
        v = elem.value if len(elem.value) <= 20 else elem.value[:17] + "…"
        parts.append(f"[{v}]")
    label = " ".join(parts)
    badges = _state_badges(elem)
    if badges:
        label += " " + badges
    return label


def _compose_label_multiline(elem: UIElement,
                             inner_w: int, inner_h: int,
                             role_glyphs: bool) -> List[str]:
    """List of strings, one per interior row, that together describe *elem*
    in the highest fidelity that fits in inner_w × inner_h."""
    lines: List[str] = []

    # Role-specific glyph row (when enabled and applicable). Replaces the
    # plain role line for known interactive controls.
    glyph_row = _role_glyph_row(elem, inner_w) if role_glyphs else None
    if glyph_row is not None:
        lines.append(glyph_row)
        remaining = inner_h - 1
        # For sliders/progress the glyph row already encodes value+name; no
        # further lines required unless we have spare room for description.
        if remaining > 0 and elem.description and inner_h >= 3:
            for w in textwrap.wrap(elem.description, width=inner_w)[:remaining]:
                lines.append(w[:inner_w])
        return lines

    # Generic path: role [badges] / "name" / value …
    badges = _state_badges(elem)
    role_line = f"{elem.role} {badges}".strip() if badges else elem.role
    if len(role_line) > inner_w:
        role_line = elem.role[:inner_w]
    lines.append(role_line)

    remaining = inner_h - 1
    if remaining > 0 and elem.name:
        quoted = f'"{elem.name}"'
        if len(quoted) <= inner_w:
            lines.append(quoted)
        else:
            lines.append((f'"{elem.name[:inner_w - 2]}…"')[:inner_w])
        remaining -= 1

    if remaining > 0 and elem.value is not None:
        val = elem.value.strip()
        if val:
            val_flat = " ".join(val.splitlines())
            for wline in textwrap.wrap(val_flat, width=inner_w)[:remaining]:
                lines.append(wline[:inner_w])

    return lines


# ─── OCR pipeline ────────────────────────────────────────────────────────────

def _preprocess_image(img: "Image.Image", upscale: int) -> Tuple["Image.Image", float]:
    """Return (preprocessed image, scale factor). Scale lets callers undo
    the coordinate transform when projecting OCR boxes back to original
    image space."""
    from PIL import ImageOps
    if upscale and upscale > 1:
        img = img.resize(
            (img.width * upscale, img.height * upscale),
            resample=__import__("PIL").Image.LANCZOS,
        )
    img = img.convert("L")
    try:
        img = ImageOps.autocontrast(img, cutoff=2)
    except Exception:
        pass
    return img, float(upscale or 1)


def _ocr_lines(screenshot_bytes: bytes,
               config: Optional[dict],
               *,
               psm: int = 11,
               ) -> List[Tuple[int, int, int, int, str, int]]:
    """Tesseract pass that returns LINE-grouped results.

    Each entry is (left, top, width, height, text, confidence) in the
    coordinate space of the *original* screenshot (the function undoes
    upscaling internally).

    Returns [] when pytesseract isn't installed, the image can't be
    decoded, or the configured backend is not tesseract.
    """
    backend = ((config or {}).get("ocr") or {}).get("backend", "tesseract")
    if backend != "tesseract":
        # Future: hook PaddleOCR / EasyOCR here. Today we treat any
        # non-tesseract value as "OCR disabled" which keeps the
        # configuration surface forward-compatible.
        return []

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return []

    try:
        from ocr_util import configure as _ocr_configure
        _ocr_configure(config)
    except Exception:
        pass

    ocr_cfg = (config or {}).get("ocr") or {}
    min_conf = int(ocr_cfg.get("min_confidence", 30))
    upscale = int(ocr_cfg.get("upscale", 2))
    preprocess = bool(ocr_cfg.get("preprocess", True))

    try:
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
    except Exception:
        return []

    scale = 1.0
    if preprocess:
        try:
            img, scale = _preprocess_image(img, upscale)
        except Exception:
            scale = 1.0

    try:
        data = pytesseract.image_to_data(
            img,
            output_type=pytesseract.Output.DICT,
            config=f"--psm {psm} --oem 3",
        )
    except Exception:
        return []

    # Group words by (block_num, par_num, line_num) into lines.
    groups: Dict[Tuple[int, int, int],
                 List[Tuple[int, int, int, int, str, int, int]]] = {}
    for i in range(len(data["text"])):
        text = str(data["text"][i]).strip()
        try:
            conf = int(float(data["conf"][i]))
        except Exception:
            conf = -1
        if conf < min_conf or not text:
            continue
        key = (int(data["block_num"][i]),
               int(data["par_num"][i]),
               int(data["line_num"][i]))
        groups.setdefault(key, []).append((
            int(data["left"][i]),
            int(data["top"][i]),
            int(data["width"][i]),
            int(data["height"][i]),
            text,
            conf,
            int(data["word_num"][i]),
        ))

    lines: List[Tuple[int, int, int, int, str, int]] = []
    for words in groups.values():
        words.sort(key=lambda w: w[6])  # by word_num
        left = min(w[0] for w in words)
        top = min(w[1] for w in words)
        right = max(w[0] + w[2] for w in words)
        bottom = max(w[1] + w[3] for w in words)
        text = " ".join(w[4] for w in words)
        conf = sum(w[5] for w in words) // len(words)
        # Undo preprocessing scale so callers get original-coord boxes.
        if scale and scale > 1:
            left   = int(left   / scale)
            top    = int(top    / scale)
            right  = int(right  / scale)
            bottom = int(bottom / scale)
        lines.append((left, top, right - left, bottom - top, text, conf))
    return lines


def _ocr_roi_text(crop: "Image.Image", psm: int, config: Optional[dict]) -> str:
    """Run Tesseract on a single cropped widget; return the recognised
    text or '' on failure."""
    try:
        import pytesseract
        from PIL import ImageOps
        try:
            crop = crop.resize(
                (crop.width * 3, crop.height * 3),
                resample=__import__("PIL").Image.LANCZOS,
            ).convert("L")
            crop = ImageOps.autocontrast(crop, cutoff=2)
        except Exception:
            pass
        txt = pytesseract.image_to_string(crop, config=f"--psm {psm} --oem 3")
        return " ".join(txt.split()).strip()
    except Exception:
        return ""


# ─── VLM fallback (Phase 5) ──────────────────────────────────────────────────

def _phash(crop: "Image.Image") -> str:
    """Perceptual-ish hash: 8×8 mean-of-grayscale signature. Stable enough
    for caching VLM lookups within a session."""
    try:
        small = crop.convert("L").resize((8, 8))
        px = list(small.getdata())
        avg = sum(px) / max(1, len(px))
        bits = "".join("1" if p >= avg else "0" for p in px)
        return hashlib.sha1(bits.encode()).hexdigest()[:12]
    except Exception:
        return ""


def _vlm_describe_crop(crop: "Image.Image", vlm_cfg: dict) -> str:
    """Single-line natural-language description from an OpenWebUI-compatible
    chat-completions endpoint. Returns '' on any failure.

    Prefers ``vlm.model_fast`` when set (a small/cheap VLM is plenty for
    per-widget labelling); falls back to the primary ``vlm.model``.
    """
    if not vlm_cfg or not vlm_cfg.get("enabled"):
        return ""
    model = vlm_cfg.get("model_fast") or vlm_cfg.get("model")
    if not model:
        return ""
    try:
        import base64 as _b64
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        b64 = _b64.b64encode(buf.getvalue()).decode()
        payload = {
            "model": model,
            "max_tokens": 60,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text",
                     "text": "One short phrase: what control is this and "
                             "what does it say? Return only the phrase."},
                ],
            }],
        }
        import os
        base_url = vlm_cfg.get("base_url") or "http://localhost:3000"
        api_key = vlm_cfg.get("api_key") or os.environ.get("OWUI_API_KEY", "")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            base_url.rstrip("/") + "/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = data["choices"][0]["message"]["content"]
        return " ".join(out.split())[:80]
    except Exception:
        return ""


# ─── Main renderer ───────────────────────────────────────────────────────────

class ASCIIRenderer:
    """Renders a UIElement tree as a spatial ASCII sketch."""

    def __init__(self, config: dict):
        self._config = config
        sketch_cfg = config.get("ascii_sketch", {})
        self.default_width  = sketch_cfg.get("grid_width",  110)
        self.default_height = sketch_cfg.get("grid_height",  38)
        self.box = _UNICODE_BOX if sketch_cfg.get("unicode_box", True) else _ASCII_BOX
        self._role_glyphs     = bool(sketch_cfg.get("role_glyphs",     True))
        self._occlusion_prune = bool(sketch_cfg.get("occlusion_prune", True))
        self._tab_badges      = bool(sketch_cfg.get("tab_index_badges", True))
        self._landmarks       = bool(sketch_cfg.get("landmark_headers", True))
        self._vlm_fallback    = bool(sketch_cfg.get("vlm_fallback",    False))
        self._vlm_cache: Dict[str, str] = {}
        try:
            from ocr_util import configure as _ocr_configure
            _ocr_configure(config)
        except Exception:
            pass

    # ── public string-only API (back-compat) ─────────────────────────────────

    def render(self,
               root:              UIElement,
               screen_bounds:     Optional[Bounds] = None,
               grid_width:        Optional[int]    = None,
               grid_height:       Optional[int]    = None,
               screenshot_bytes:  Optional[bytes]  = None,
               ) -> str:
        return self.render_structured(
            root=root,
            screen_bounds=screen_bounds,
            grid_width=grid_width,
            grid_height=grid_height,
            screenshot_bytes=screenshot_bytes,
        )["sketch"]

    # ── structured API ───────────────────────────────────────────────────────

    def render_structured(self,
                          root:              UIElement,
                          screen_bounds:     Optional[Bounds] = None,
                          grid_width:        Optional[int]    = None,
                          grid_height:       Optional[int]    = None,
                          screenshot_bytes:  Optional[bytes]  = None,
                          ) -> Dict[str, Any]:
        """Render and return both the ASCII grid string and a flat list of
        structured element records, suitable for an LLM planner."""
        try:
            return self._render_impl(
                root, screen_bounds, grid_width, grid_height, screenshot_bytes,
            )
        except Exception as e:
            print(f"[ASCIIRenderer:render_structured] {e}")
            traceback.print_exc()
            return {"sketch": f"[ASCII render error: {e}]",
                    "elements": [], "legend": {}}

    def _render_impl(self,
                     root: UIElement,
                     screen_bounds: Optional[Bounds],
                     grid_width: Optional[int],
                     grid_height: Optional[int],
                     screenshot_bytes: Optional[bytes],
                     ) -> Dict[str, Any]:
        gw = grid_width  or self.default_width
        gh = grid_height or self.default_height
        bx = self.box

        ref = screen_bounds or root.bounds
        if not ref:
            ref = Bounds(root.bounds.x, root.bounds.y,
                         max(root.bounds.width, 1),
                         max(root.bounds.height, 1))
        rw = max(ref.width,  1)
        rh = max(ref.height, 1)

        grid: List[List[str]] = [[" "] * gw for _ in range(gh)]
        # Confidence shadow: 0 for blank/borders, 1+ for OCR-written cells.
        # Borders are written first and kept at 0; OCR overlay refuses to
        # touch any cell already containing a border character.
        conf_grid: List[List[int]] = [[0] * gw for _ in range(gh)]

        def to_gx(px: int) -> int:
            return max(0, min(gw - 1, int((px - ref.x) * gw / rw)))

        def to_gy(py: int) -> int:
            return max(0, min(gh - 1, int((py - ref.y) * gh / rh)))

        # ── ROI screenshot prep ──────────────────────────────────────────────
        roi_img = None
        if screenshot_bytes is not None:
            try:
                from PIL import Image
                roi_img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
            except Exception:
                roi_img = None

        roi_used = [0]
        max_roi = int((self._config.get("ocr") or {}).get("max_roi_crops", 40))
        psm_roi = int((self._config.get("ocr") or {}).get("psm_roi", 7))
        vlm_cfg = self._config.get("vlm") or {}

        # ── pass 1: assign legend keys + tab indices, ROI-enrich names ───────
        # We walk the tree once up front so the structured records and the
        # in-grid labels see the same enriched data.

        records: List[Dict[str, Any]] = []
        focusables: List[UIElement] = []
        elem_meta: Dict[int, Dict[str, Any]] = {}  # id(elem) → meta
        counter = [0]

        def _occluded_by_later_siblings(child: UIElement,
                                        siblings_after: List[UIElement]) -> bool:
            """True if *child*'s bounds are entirely covered by the union of
            siblings drawn after it (same parent, later in DFS = on top)."""
            if not self._occlusion_prune or not siblings_after:
                return False
            cb = child.bounds
            if cb.width <= 0 or cb.height <= 0:
                return False
            # A single covering sibling is the common case (modal/popover).
            for s in siblings_after:
                sb = s.bounds
                if (sb.x <= cb.x and sb.y <= cb.y
                        and sb.right >= cb.right
                        and sb.bottom >= cb.bottom):
                    return True
            return False

        def enrich_label(elem: UIElement) -> str:
            """Return whatever OCR/VLM-recovered text we should treat as the
            element's effective name when the a11y tree provided none."""
            if elem.name or elem.value:
                return ""
            if roi_img is None or roi_used[0] >= max_roi:
                return ""
            r = _norm_role(elem.role)
            if r not in ("image", "custom", "group", "pane", "unknown",
                          "graphic", "imagebutton", ""):
                return ""
            b = elem.bounds
            # Screenshot is window-cropped: convert screen→window coords.
            x0 = max(0, b.x - ref.x)
            y0 = max(0, b.y - ref.y)
            x1 = min(roi_img.width,  b.right  - ref.x)
            y1 = min(roi_img.height, b.bottom - ref.y)
            if x1 - x0 < 4 or y1 - y0 < 4:
                return ""
            roi_used[0] += 1
            try:
                crop = roi_img.crop((x0, y0, x1, y1))
            except Exception:
                return ""
            text = _ocr_roi_text(crop, psm_roi, self._config)
            if text:
                return text
            if self._vlm_fallback:
                ph = _phash(crop)
                if ph in self._vlm_cache:
                    return self._vlm_cache[ph]
                vlabel = _vlm_describe_crop(crop, vlm_cfg)
                if vlabel:
                    self._vlm_cache[ph] = vlabel
                return vlabel
            return ""

        def walk(elem: UIElement, parent_visible: bool,
                 siblings_after: List[UIElement]) -> None:
            occ = _occluded_by_later_siblings(elem, siblings_after)
            visible = parent_visible and not occ
            ocr_label = enrich_label(elem) if visible else ""
            key = _legend_key(counter[0])
            counter[0] += 1
            tab_idx = None
            if visible and _norm_role(elem.role) in _FOCUSABLE_ROLES:
                focusables.append(elem)
                tab_idx = len(focusables)
            elem_meta[id(elem)] = {
                "legend_key": key,
                "tab_index": tab_idx,
                "visible": visible,
                "occluded": occ,
                "ocr_name": ocr_label,
            }
            for i, child in enumerate(elem.children):
                walk(child, visible, elem.children[i + 1:])

        walk(root, True, [])

        # ── pass 2: draw boxes (DFS, parent before child) ────────────────────

        legend: Dict[str, str] = {}

        def draw(elem: UIElement) -> None:
            meta = elem_meta[id(elem)]
            if not meta["visible"]:
                return
            b = elem.bounds
            if b.width < 1 or b.height < 1:
                return

            gx1, gy1 = to_gx(b.x),     to_gy(b.y)
            gx2, gy2 = to_gx(b.right), to_gy(b.bottom)
            gx2 = min(max(gx1 + 2, gx2), gw - 1)
            gy2 = min(max(gy1 + 2, gy2), gh - 1)
            if gx1 >= gw or gy1 >= gh or gx2 <= gx1 or gy2 <= gy1:
                return

            meta["grid_bounds"] = (gx1, gy1, gx2, gy2)

            # Corners + edges
            grid[gy1][gx1] = bx["tl"]
            grid[gy1][gx2] = bx["tr"]
            grid[gy2][gx1] = bx["bl"]
            grid[gy2][gx2] = bx["br"]
            for x in range(gx1 + 1, gx2):
                if grid[gy1][x] in (" ", bx["h"]):
                    grid[gy1][x] = bx["h"]
                if grid[gy2][x] in (" ", bx["h"]):
                    grid[gy2][x] = bx["h"]
            for y in range(gy1 + 1, gy2):
                if grid[y][gx1] in (" ", bx["v"]):
                    grid[y][gx1] = bx["v"]
                if grid[y][gx2] in (" ", bx["v"]):
                    grid[y][gx2] = bx["v"]

            # Landmark header — bake role + name into the top edge.
            if self._landmarks:
                r = _norm_role(elem.role)
                lm = _LANDMARK_ROLES.get(r)
                if lm and gx2 - gx1 >= 6:
                    label = f" {lm}"
                    if elem.name:
                        label += f' "{elem.name}"'
                    label += " "
                    available = gx2 - gx1 - 1   # interior between corners
                    if len(label) > available:
                        label = label[:available]
                    # Center-ish: start at gx1+1
                    for i, ch in enumerate(label):
                        grid[gy1][gx1 + 1 + i] = ch

            inner_w = gx2 - gx1 - 1
            inner_h = gy2 - gy1 - 1
            if inner_w < 1:
                return

            # Legend key in top-right interior (always written when there's
            # room for one cell; lets agents target by short ID).
            key = meta["legend_key"]
            if inner_w >= len(key) + 1 and inner_h >= 1:
                for i, ch in enumerate(key):
                    cx = gx2 - len(key) + i
                    if grid[gy1 + 0][cx] in (" ",) and 0 <= cx < gw:
                        # write under the top border so it remains readable
                        pass
                # Place key inside the top-right corner of the interior:
                kx = gx2 - len(key)
                ky = gy1 + 1 if inner_h >= 1 else gy1
                for i, ch in enumerate(key):
                    if kx + i < gx2 and grid[ky][kx + i] == " ":
                        grid[ky][kx + i] = ch

            # Tab-index numeral in the top-left interior corner.
            if self._tab_badges and meta["tab_index"] is not None:
                glyph = _tab_glyph(meta["tab_index"])
                tx = gx1 + 1
                ty = gy1 + 1 if inner_h >= 1 else gy1
                for i, ch in enumerate(glyph):
                    if tx + i < gx2 and grid[ty][tx + i] == " ":
                        grid[ty][tx + i] = ch

            # Use OCR-recovered name when a11y tree had none.
            effective = elem
            if meta["ocr_name"] and not elem.name:
                # Shallow copy with name patched in — keep tree immutable.
                effective = UIElement(
                    element_id=elem.element_id, name=meta["ocr_name"],
                    role=elem.role, value=elem.value, bounds=elem.bounds,
                    enabled=elem.enabled, focused=elem.focused,
                    keyboard_shortcut=elem.keyboard_shortcut,
                    description=elem.description,
                    selected=elem.selected, expanded=elem.expanded,
                    value_now=elem.value_now, value_min=elem.value_min,
                    value_max=elem.value_max, identifier=elem.identifier,
                )

            if inner_h < 1 or inner_w < 3:
                legend[key] = _compose_label(effective)
                cy = (gy1 + gy2) // 2
                cx = (gx1 + gx2) // 2
                for i, ch in enumerate(key[:2]):
                    if cx + i < gx2 and grid[cy][cx + i] == " ":
                        grid[cy][cx + i] = ch
                return

            # First label-row index inside the box: skip row 1 (used by tab
            # badge + legend key) when either is present, else start at row 1.
            first_label_row = 1
            if self._tab_badges and meta["tab_index"] is not None:
                first_label_row = 2
            elif inner_w >= len(key) + 1:
                first_label_row = 2

            # Budget remaining rows for the label.
            rows_for_label = inner_h - (first_label_row - 1)
            if rows_for_label < 1:
                # Tight box — overwrite into top row.
                first_label_row = 1
                rows_for_label = inner_h

            label_lines = _compose_label_multiline(
                effective, inner_w, rows_for_label, self._role_glyphs,
            )
            for row_idx, line in enumerate(label_lines):
                gy = gy1 + first_label_row + row_idx - 1 + 1
                if gy >= gy2:
                    break
                for col_idx, ch in enumerate(line):
                    cx = gx1 + 1 + col_idx
                    if cx < gx2 and grid[gy][cx] == " ":
                        grid[gy][cx] = ch

            # Stash effective label for the legend table — useful even when
            # the inline label fit, because legend keys are also written into
            # the grid corner.
            legend[key] = _compose_label(effective)

        def draw_tree(elem: UIElement) -> None:
            draw(elem)
            for child in elem.children:
                draw_tree(child)

        draw_tree(root)

        # ── pass 3: OCR overlay (line-grouped, confidence-weighted) ──────────

        if screenshot_bytes:
            psm_window = int((self._config.get("ocr") or {})
                              .get("psm_window", 11))
            lines = _ocr_lines(screenshot_bytes, self._config, psm=psm_window)
            for (wx, wy, ww, wh, text, conf) in lines:
                # Screenshot coords are window-relative; project to screen
                # then to grid.
                sx = wx + ref.x
                sy = wy + ref.y
                gx_start = to_gx(sx)
                gx_end   = to_gx(sx + max(ww, 1))
                gy       = to_gy(sy + wh // 2)
                # Clip line to its own grid footprint so a long word can't
                # bleed into a neighboring widget.
                max_cells = max(1, gx_end - gx_start + 1)
                snippet = text[:max_cells]
                for i, ch in enumerate(snippet):
                    cx = gx_start + i
                    if not (0 <= cx < gw and 0 <= gy < gh):
                        continue
                    cur = grid[gy][cx]
                    # Never overwrite a box border character.
                    if cur in _BORDER_CHARS:
                        continue
                    # Overwrite blanks freely; overwrite earlier OCR only
                    # when this pass has higher confidence.
                    if cur == " " or conf > conf_grid[gy][cx]:
                        grid[gy][cx] = ch
                        conf_grid[gy][cx] = conf

        # ── serialise grid ──────────────────────────────────────────────────

        lines_out = ["".join(row).rstrip() for row in grid]
        while lines_out and not lines_out[-1].strip():
            lines_out.pop()
        sketch = "\n".join(lines_out)

        if legend:
            sketch += "\n\n  LEGEND\n  " + "─" * 50
            for key, label in legend.items():
                sketch += f"\n  {key:>4}  {label}"

        # ── pass 4: build structured records ────────────────────────────────

        def collect(elem: UIElement) -> None:
            meta = elem_meta.get(id(elem)) or {}
            if not meta.get("visible") and not meta.get("occluded"):
                # Element wasn't reached (shouldn't happen) — skip.
                pass
            rec: Dict[str, Any] = {
                "id": elem.element_id,
                "role": elem.role,
                "name": elem.name or meta.get("ocr_name") or "",
                "value": elem.value,
                "bounds_screen": elem.bounds.to_dict(),
                "state": {
                    "focused": elem.focused,
                    "enabled": elem.enabled,
                    "selected": elem.selected,
                    "expanded": elem.expanded,
                },
                "legend_key": meta.get("legend_key"),
                "tab_index":  meta.get("tab_index"),
                "occluded":   bool(meta.get("occluded")),
            }
            if meta.get("ocr_name"):
                rec["ocr_text"] = meta["ocr_name"]
            gb = meta.get("grid_bounds")
            if gb is not None:
                rec["bounds_grid"] = list(gb)
            if elem.value_now is not None:
                rec["value_now"] = elem.value_now
            if elem.value_min is not None:
                rec["value_min"] = elem.value_min
            if elem.value_max is not None:
                rec["value_max"] = elem.value_max
            if elem.identifier:
                rec["identifier"] = elem.identifier
            if elem.keyboard_shortcut:
                rec["keyboard_shortcut"] = elem.keyboard_shortcut
            records.append(rec)
            for child in elem.children:
                collect(child)

        collect(root)

        return {
            "sketch":   sketch,
            "elements": records,
            "legend":   legend,
        }

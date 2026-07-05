"""
redaction.py — Sensitive-region redaction (design doc §18, D7) and
untrusted-screen-content marking (P2 trust boundary).

Default: tree node names/values + OCR text + VLM preamble are scrubbed.
Opt-in: redaction.blur_screenshots paints matched bboxes solid black.

Trust boundary: everything read off the screen (window titles, element
names/values, OCR words, VLM descriptions) is attacker-influenced data —
a web page or document on screen can contain prompt-injection text.
``mark_untrusted`` flags the results of screen-text-carrying tools with
``untrusted: true`` and strips ANSI escape sequences / control characters
so extracted text cannot smuggle terminal escapes to downstream consumers.
MCP clients must treat these fields as data, never as instructions.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_REPLACEMENT = "[REDACTED]"


# ─── Untrusted screen content (P2 trust boundary) ────────────────────────────

# CSI (ESC [ … cmd), OSC (ESC ] … BEL/ST), and single-char escapes.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]"          # CSI sequences
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)?"       # OSC sequences
    r"|[@-Z\\-_])"                            # 2-byte escapes (incl. lone ESC-x)
)
# C0/C1 control characters except \t \n \r (layout-preserving whitespace).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Tool results that carry text extracted from the screen (window titles,
# element names/values, OCR words, descriptions).  Their responses are
# flagged ``untrusted: true`` and sanitized.  Action receipts also embed
# screen-derived selectors, but flagging every action result would drown
# the signal; perception results are where injection text actually lands.
UNTRUSTED_RESULT_TOOLS = frozenset({
    "list_windows",
    "find_element",
    "get_window_structure",
    "observe_window",
    "wait_for",
    "snapshot_get",
    "snapshot_diff",
    "get_ocr",
    "get_screen_description",
})

# Keys whose values are machine artifacts (base64 blobs, opaque tokens) —
# skipped during sanitization for performance and fidelity.
_SANITIZE_SKIP_KEYS = frozenset({
    "data", "screenshot_b64", "tree_token", "base_token",
    "snapshot_id", "confirm_token",
})


def sanitize_screen_text(text: Optional[str]) -> Optional[str]:
    """Strip ANSI escape sequences and non-whitespace control characters
    from screen-extracted text.  Idempotent; returns non-str input as-is."""
    if not isinstance(text, str) or not text:
        return text
    text = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHARS_RE.sub("", text)


def _sanitize_value(value: Any, key: str = "") -> Any:
    if isinstance(value, str):
        return sanitize_screen_text(value)
    if isinstance(value, dict):
        return {k: (v if k in _SANITIZE_SKIP_KEYS else _sanitize_value(v, k))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    return value


def mark_untrusted(tool: str, result: Any) -> Any:
    """Flag screen-text-carrying tool results as untrusted and sanitize
    their string payloads.  No-op for tools that return no screen text."""
    if tool not in UNTRUSTED_RESULT_TOOLS or not isinstance(result, dict):
        return result
    out = _sanitize_value(result)
    out["untrusted"] = True
    return out


class Redactor:
    def __init__(self, config: Dict[str, Any]) -> None:
        cfg = (config.get("redaction") or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.window_title_patterns = list(cfg.get("window_title_patterns", []))
        self.element_name_patterns = list(cfg.get("element_name_patterns", []))
        self.element_role_patterns = list(cfg.get("element_role_patterns", []))
        self.ocr_text_patterns = list(cfg.get("ocr_text_patterns", []))
        self.replacement = cfg.get("replacement", DEFAULT_REPLACEMENT)
        self.blur_screenshots = bool(cfg.get("blur_screenshots", False))
        self.applied_count = 0

    def is_active(self) -> bool:
        return self.enabled and bool(
            self.window_title_patterns or self.element_name_patterns
            or self.element_role_patterns or self.ocr_text_patterns
        )

    # ── Tree ─────────────────────────────────────────────────────────────────

    def redact_tree(self, tree_dict: Dict[str, Any],
                    window_title: str = "") -> Dict[str, Any]:
        if not self.is_active():
            return tree_dict
        title_match = any(re.search(p, window_title or "")
                          for p in self.window_title_patterns)
        return self._walk_node(tree_dict, all_match=title_match)

    def _walk_node(self, node: Dict[str, Any],
                    all_match: bool = False) -> Dict[str, Any]:
        out = dict(node)
        role = out.get("role") or ""
        name = out.get("name") or ""
        match = all_match or any(
            re.search(p, role) for p in self.element_role_patterns
        ) or any(
            re.search(p, name) for p in self.element_name_patterns
        )
        if match:
            if out.get("name"):
                out["name"] = self.replacement
            if out.get("value"):
                out["value"] = self.replacement
            self.applied_count += 1
        out["children"] = [self._walk_node(c, all_match=all_match)
                            for c in (out.get("children") or [])]
        return out

    # ── OCR ──────────────────────────────────────────────────────────────────

    def redact_ocr_text(self, text: str) -> str:
        if not self.is_active() or not text:
            return text
        for pattern in self.ocr_text_patterns:
            try:
                text, n = re.subn(pattern, self.replacement, text)
                if n:
                    self.applied_count += n
            except re.error:
                continue
        return text

    def redact_ocr_words(self, words: List[Dict[str, Any]]
                          ) -> List[Dict[str, Any]]:
        if not self.is_active():
            return words
        out: List[Dict[str, Any]] = []
        for w in words:
            t = self.redact_ocr_text(w.get("text", ""))
            wrec = dict(w, text=t)
            out.append(wrec)
        return out

    # ── VLM preamble ─────────────────────────────────────────────────────────

    def vlm_preamble(self) -> str:
        if not self.is_active():
            return ""
        bits: List[str] = []
        if self.element_role_patterns:
            bits.append("any field whose role matches: "
                        + ", ".join(self.element_role_patterns))
        if self.element_name_patterns:
            bits.append("any field whose name matches: "
                        + ", ".join(self.element_name_patterns))
        if not bits:
            return ""
        return ("Do not transcribe or describe the contents of "
                + "; or ".join(bits) + ". ")

    # ── Screenshot blur (opt-in) ─────────────────────────────────────────────

    def blur_regions(self, png_bytes: bytes,
                      regions: List[Dict[str, int]]) -> bytes:
        if not self.blur_screenshots or not regions:
            return png_bytes
        try:
            from PIL import Image, ImageDraw
        except Exception:
            return png_bytes
        img = Image.open(io.BytesIO(png_bytes))
        draw = ImageDraw.Draw(img)
        for r in regions:
            try:
                x = int(r.get("x", 0))
                y = int(r.get("y", 0))
                w = int(r.get("width", 0))
                h = int(r.get("height", 0))
                if w > 0 and h > 0:
                    draw.rectangle([x, y, x + w, y + h], fill="black")
            except (TypeError, ValueError):
                continue
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active":  self.is_active(),
            "patterns_count": (
                len(self.window_title_patterns) +
                len(self.element_name_patterns) +
                len(self.element_role_patterns) +
                len(self.ocr_text_patterns)
            ),
            "applied_count": self.applied_count,
            "blur_screenshots": self.blur_screenshots,
        }

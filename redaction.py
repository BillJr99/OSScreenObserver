"""
redaction.py — Sensitive-region redaction (design doc §18, D7).

Default: tree node names/values + OCR text + VLM preamble are scrubbed.
Opt-in: redaction.blur_screenshots paints matched bboxes solid black.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DEFAULT_REPLACEMENT = "[REDACTED]"


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

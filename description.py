"""
description.py — Textual description generator.

Three modalities, each with a distinct cost/fidelity profile:

  accessibility  — Pure serialization of the UIA/AX/AT-SPI element tree into
                   structured prose. Zero additional API calls; instant.
                   Limited to what the accessibility framework exposes.

  ocr            — Tesseract-based OCR on a screenshot. Captures text that
                   is rendered but not in the accessibility tree. Moderate
                   latency; requires Pillow + pytesseract.

  vlm            — Claude Vision caption of a screenshot. Richest description;
                   includes layout, iconography, color cues, and contextual
                   interpretation. Requires ANTHROPIC_API_KEY and network.

These can be used individually or combined via combined().
"""

import base64
import io
import logging
import traceback
from typing import Dict, List, Optional

from observer import UIElement, WindowInfo

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _count(elem: UIElement) -> int:
    return 1 + sum(_count(c) for c in elem.children)


def _find_focused(elem: UIElement) -> Optional[UIElement]:
    if elem.focused:
        return elem
    for child in elem.children:
        result = _find_focused(child)
        if result:
            return result
    return None


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────────
# DescriptionGenerator
# ─────────────────────────────────────────────────────────────────────────────

class DescriptionGenerator:
    """Generates textual descriptions of screen state via three modalities."""

    def __init__(self, config: dict):
        self.config    = config
        self.vlm_cfg   = config.get("vlm",  {})
        self.ocr_cfg   = config.get("ocr",  {})

        # Apply tesseract_cmd globally so every callsite (description, tools,
        # oracles, ascii_renderer) sees the configured path.
        try:
            from ocr_util import configure as _ocr_configure
            _ocr_configure(config)
        except Exception:
            pass

    # ── Accessibility tree → prose ────────────────────────────────────────────

    def from_tree(
        self,
        root:   UIElement,
        window: Optional[WindowInfo] = None,
    ) -> str:
        """
        Produce a structured prose description by serializing the element tree.

        The output is a depth-indented listing in which each element is
        represented by its role, name, value (if any), and state badges.
        A summary line at the end reports element count and the currently
        focused element.
        """
        try:
            lines: List[str] = []

            if window:
                lines += [
                    f"Application : {window.process_name}",
                    f"Window      : {window.title}",
                    f"Geometry    : ({window.bounds.x}, {window.bounds.y})  "
                    f"{window.bounds.width} × {window.bounds.height} px",
                    "",
                ]

            lines.append(
                f"Root: {root.role}"
                + (f'  "{_truncate(root.name, 60)}"' if root.name else "")
            )

            def _describe(elem: UIElement, depth: int = 1) -> None:
                prefix = "  " * depth + "└─ "
                parts  = [elem.role]

                if elem.name:
                    parts.append(f'"{_truncate(elem.name, 40)}"')
                if elem.value is not None:
                    v = _truncate(elem.value, 60)
                    parts.append(f"= {v!r}")

                badges: List[str] = []
                if elem.focused:
                    badges.append("FOCUSED")
                if not elem.enabled:
                    badges.append("DISABLED")
                if elem.keyboard_shortcut:
                    badges.append(f"shortcut:{elem.keyboard_shortcut}")
                if elem.description:
                    badges.append(f"desc:{_truncate(elem.description, 30)}")

                b = elem.bounds
                pos = f"  @({b.x},{b.y}) {b.width}×{b.height}"

                badge_str = ("  [" + "  ".join(badges) + "]") if badges else ""
                lines.append(f"{prefix}{' '.join(parts)}{badge_str}{pos}")

                for child in elem.children:
                    _describe(child, depth + 1)

            _describe(root)

            total   = _count(root)
            focused = _find_focused(root)
            focus_str = (
                f"; focused → {focused.role}"
                + (f' "{focused.name}"' if focused.name else "")
                if focused else ""
            )
            lines += ["", f"[{total} elements total{focus_str}]"]

            return "\n".join(lines)

        except Exception as e:
            print(f"[DescriptionGenerator:from_tree] {e}")
            traceback.print_exc()
            return f"[Tree description failed: {e}]"

    # ── OCR ───────────────────────────────────────────────────────────────────

    def from_ocr(self, screenshot_bytes: bytes) -> str:
        """
        Extract visible text via Tesseract OCR.

        Uses pytesseract's data output to reconstruct reading order by
        grouping words into their block → paragraph → line hierarchy.
        Low-confidence detections (below ocr.min_confidence in config)
        are suppressed.
        """
        if not self.ocr_cfg.get("enabled", True):
            return "[OCR disabled in config (ocr.enabled = false)]"

        try:
            import pytesseract
            from PIL import Image
            from ocr_util import configure as _ocr_configure
            _ocr_configure(self.config)

            min_conf = self.ocr_cfg.get("min_confidence", 30)
            img  = Image.open(io.BytesIO(screenshot_bytes))
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

            # Accumulate words keyed by (block, paragraph, line)
            structure: Dict[tuple, List[str]] = {}
            for i, text in enumerate(data["text"]):
                text = text.strip()
                if not text:
                    continue
                try:
                    conf = int(data["conf"][i])
                except (ValueError, TypeError):
                    conf = 0
                if conf < min_conf:
                    continue
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                structure.setdefault(key, []).append(text)

            if not structure:
                return "[No text detected by OCR]"

            # Render blocks separated by blank lines
            lines: List[str] = []
            prev_block = None
            for key in sorted(structure.keys()):
                block = key[0]
                if prev_block is not None and block != prev_block:
                    lines.append("")
                lines.append(" ".join(structure[key]))
                prev_block = block

            return "\n".join(lines)

        except ImportError:
            from ocr_util import INSTALL_HINT as _HINT
            return ("[pytesseract not installed — run `pip install pytesseract`. "
                    f"{_HINT}]")
        except Exception as e:
            print(f"[DescriptionGenerator:from_ocr] {e}")
            traceback.print_exc()
            # Surface the diagnostic so the user can see what tesseract_cmd
            # resolved to (or didn't), plus the install/config hint.
            try:
                from ocr_util import diagnose as _ocr_diag, INSTALL_HINT
                diag = _ocr_diag(self.config)
            except Exception:
                diag = {}
                INSTALL_HINT = ""
            return (f"[OCR failed: {e}; "
                    f"tesseract_cmd={diag.get('configured_path')!r}, "
                    f"exists={diag.get('configured_path_exists')}, "
                    f"version={diag.get('version')}, "
                    f"on_PATH={diag.get('path_discovered')!r}.  "
                    f"{INSTALL_HINT}]")

    # ── VLM (Claude Vision) ───────────────────────────────────────────────────

    def from_vlm(self, screenshot_bytes: bytes) -> str:
        """
        Generate a rich description via Claude's vision capability.

        Sends the screenshot to the configured Claude model with a structured
        analysis prompt designed for AI agent consumption. Requires
        ANTHROPIC_API_KEY to be set in the environment and vlm.enabled = true
        in config.json.
        """
        if not self.vlm_cfg.get("enabled", False):
            return (
                "[VLM disabled — set vlm.enabled = true in config.json "
                "and ensure ANTHROPIC_API_KEY is set]"
            )

        try:
            import anthropic

            client  = anthropic.Anthropic()
            b64_img = base64.b64encode(screenshot_bytes).decode()
            prompt  = self.vlm_cfg.get(
                "prompt",
                "Describe what is on this computer screen in structured detail for an AI agent.",
            )

            response = client.messages.create(
                model      = self.vlm_cfg.get("model", "claude-sonnet-4-20250514"),
                max_tokens = self.vlm_cfg.get("max_tokens", 1500),
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type":       "base64",
                                "media_type": "image/png",
                                "data":       b64_img,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return response.content[0].text

        except ImportError:
            return "[anthropic package not installed — run: pip install anthropic]"
        except Exception as e:
            print(f"[DescriptionGenerator:from_vlm] {e}")
            traceback.print_exc()
            return f"[VLM description failed: {e}]"

    # ── Combined ──────────────────────────────────────────────────────────────

    def combined(
        self,
        root:             UIElement,
        screenshot_bytes: Optional[bytes],
        window:           Optional[WindowInfo] = None,
    ) -> Dict[str, str]:
        """Return all enabled descriptions in a keyed dict."""
        result: Dict[str, str] = {
            "accessibility": self.from_tree(root, window)
        }
        if screenshot_bytes:
            if self.ocr_cfg.get("enabled", True):
                result["ocr"] = self.from_ocr(screenshot_bytes)
            if self.vlm_cfg.get("enabled", False):
                result["vlm"] = self.from_vlm(screenshot_bytes)
        return result

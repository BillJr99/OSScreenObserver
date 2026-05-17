"""
description.py — Textual description generator.

Three modalities, each with a distinct cost/fidelity profile:

  accessibility  — Pure serialization of the UIA/AX/AT-SPI element tree into
                   structured prose. Zero additional API calls; instant.
                   Limited to what the accessibility framework exposes.

  ocr            — Tesseract-based OCR on a screenshot. Captures text that
                   is rendered but not in the accessibility tree. Moderate
                   latency; requires Pillow + pytesseract.

  vlm            — Vision-language-model caption of a screenshot. Richest
                   description; includes layout, iconography, color cues, and
                   contextual interpretation. Reached through an
                   OpenWebUI-compatible OpenAI chat-completions endpoint;
                   requires vlm.base_url + vlm.model in config.json (and an
                   api_key if the endpoint demands one).

These can be used individually or combined via combined().
"""

import base64
import io
import json
import logging
import os
import traceback
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from observer import UIElement, WindowInfo

logger = logging.getLogger(__name__)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject any HTTP redirect — a 302/303 on POST would silently become
    a GET (dropping the screenshot body) and could forward content to an
    unintended host. Surface the misconfiguration instead."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"refusing redirect to {headers.get('Location')!r} "
            f"(check vlm.base_url)",
            headers, fp,
        )

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


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
                + (f'  "{root.name}"' if root.name else "")
            )

            def _describe(elem: UIElement, depth: int = 1) -> None:
                prefix = "  " * depth + "└─ "
                parts  = [elem.role]

                if elem.name:
                    parts.append(f'"{elem.name}"')
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
            from ocr_util import configure as _ocr_configure, diagnose as _ocr_diag, INSTALL_HINT
            _ocr_configure(self.config)

            # Pre-flight: if neither the configured path nor PATH yields a
            # working tesseract binary, return a clean install hint instead
            # of letting pytesseract raise TesseractNotFoundError (which
            # surfaces a long, scary stack trace even though we catch it).
            diag = _ocr_diag(self.config)
            if not diag.get("configured_path_exists") and not diag.get("path_discovered"):
                return (f"[OCR unavailable: tesseract binary not found "
                        f"(configured={diag.get('configured_path')!r}, "
                        f"on_PATH={diag.get('path_discovered')!r}). "
                        f"{INSTALL_HINT}]")

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
            # Known "tesseract missing" condition is already handled above;
            # anything else is unexpected enough to log once at WARNING
            # without dumping a full traceback on every request.
            try:
                from ocr_util import diagnose as _ocr_diag, INSTALL_HINT
                diag = _ocr_diag(self.config)
            except Exception:
                diag = {}
                INSTALL_HINT = ""
            logger.warning("[DescriptionGenerator:from_ocr] %s", e)
            return (f"[OCR failed: {e}; "
                    f"tesseract_cmd={diag.get('configured_path')!r}, "
                    f"exists={diag.get('configured_path_exists')}, "
                    f"version={diag.get('version')}, "
                    f"on_PATH={diag.get('path_discovered')!r}.  "
                    f"{INSTALL_HINT}]")

    # ── VLM (OpenWebUI-compatible chat completions) ──────────────────────────

    def from_vlm(self, screenshot_bytes: bytes) -> str:
        """
        Generate a rich description via a vision-capable LLM exposed through
        an OpenWebUI-compatible OpenAI chat-completions endpoint.

        The screenshot is sent as a base64 `image_url` content part to
        `{vlm.base_url}/api/v1/chat/completions` using `vlm.model`. The
        endpoint can front any vision model OpenWebUI supports (e.g. Claude
        via the Anthropic integration, GPT-4o, etc.) — OSScreenObserver
        itself no longer depends on the Anthropic SDK or ANTHROPIC_API_KEY.

        Required config:
          vlm.enabled  = true
          vlm.base_url = e.g. "http://localhost:3000"
          vlm.model    = a model id available through the endpoint
          vlm.api_key  = optional; falls back to $OWUI_API_KEY
        """
        if not self.vlm_cfg.get("enabled", False):
            return (
                "[VLM disabled — set vlm.enabled = true in config.json "
                "and configure vlm.base_url / vlm.model]"
            )

        model = self.vlm_cfg.get("model")
        if not model:
            return (
                "[VLM model not configured — run `python main.py --mode "
                "inspect` once to pick a model, or set vlm.model in "
                "config.json]"
            )

        base_url   = self.vlm_cfg.get("base_url") or "http://localhost:3000"
        api_key    = (self.vlm_cfg.get("api_key")
                      or os.environ.get("OWUI_API_KEY", ""))
        prompt_txt = self.vlm_cfg.get(
            "prompt",
            "Describe what is on this computer screen in structured detail "
            "for an AI agent.",
        )
        max_tokens = self.vlm_cfg.get("max_tokens", 1500)

        b64_img = base64.b64encode(screenshot_bytes).decode()
        payload = {
            "model":      model,
            "max_tokens": max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
                    {"type": "text", "text": prompt_txt},
                ],
            }],
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Try /api/v1 (OpenWebUI) first, fall back to /v1 (Ollama / OpenAI).
        _PREFIXES = ["/api/v1", "/v1"]
        opener = urllib.request.build_opener(_NoRedirectHandler)
        last_exc: Optional[Exception] = None
        for prefix in _PREFIXES:
            url = base_url.rstrip("/") + prefix + "/chat/completions"
            try:
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode("utf-8"),
                    headers=headers, method="POST",
                )
                # urllib follows redirects by default and would silently
                # convert a 302/303 POST into a GET, dropping the screenshot
                # payload (and potentially forwarding it to an unintended
                # host). Refuse any redirect so misconfigured vlm.base_url
                # fails loudly instead.
                with opener.open(req, timeout=240) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                # A 404 on the URL itself (not the model) suggests the prefix
                # is wrong — try the next one. Any other HTTP error (401, 500
                # …) is definitive; surface it immediately.
                if e.code == 404:
                    last_exc = e
                    continue
                print(f"[DescriptionGenerator:from_vlm] {e}")
                traceback.print_exc()
                return f"[VLM description failed: {e}]"
            except Exception as e:
                last_exc = e
                # A connection-level failure (refused, timeout, DNS) most
                # likely means the whole base_url is wrong; no point retrying
                # with a different prefix.
                break
        print(f"[DescriptionGenerator:from_vlm] {last_exc}")
        traceback.print_exc()
        return f"[VLM description failed: {last_exc}]"

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

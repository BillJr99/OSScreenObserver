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

The VLM channel has two operating modes:

  single     — One screenshot + one prompt + (optionally) the accessibility
               tree, OCR text, and ASCII sketch as in-context "ground truth"
               blocks. Cheap (one call) and back-compatible.

  multipass  — A three-pass pipeline (scene → controls → next-actions) that
               returns a strict JSON envelope with structured fields suitable
               for an agentic LLM consumer. An optional fourth verify pass can
               cross-check the control inventory against the accessibility
               tree using a second model.

These can be used individually or combined via combined().
"""

import base64
import io
import json
import logging
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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
# Default prompts (overridable via config)
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_PROMPT_SINGLE = (
    "You are analysing a computer screen for an agentic AI system.\n"
    "\n"
    "The screenshot is attached. When <ACCESSIBILITY_TREE>, <OCR_TEXT>, "
    "<ASCII_SKETCH>, or <FOCUSED_ELEMENT> blocks are present below, treat "
    "them as GROUND TRUTH for control names, states, and positions — prefer "
    "them over your visual guess. Quote on-screen text verbatim from "
    "<OCR_TEXT> when available.\n"
    "\n"
    "Return ONLY a JSON object with this schema (use null when a field is "
    "not visible — never guess):\n"
    "{\n"
    '  "summary":      "<=2 sentences, what is on screen and what the user '
    'is doing",\n'
    '  "app":          "application name, e.g. \\"VS Code\\"",\n'
    '  "screen_type":  "kebab-case label, e.g. \\"code-editor\\", '
    '\\"settings-dialog\\"",\n'
    '  "primary_task": "<=1 sentence",\n'
    '  "focused":      {"role": "...", "name": "...", "tree_id": "..."} or '
    "null,\n"
    '  "controls":     [{"role": "...", "name": "...", "state": "...", '
    '"selector_hint": "...", "tree_id": "..."}, ...]   (<=8 entries),\n'
    '  "next_actions": [{"description": "...", "target_selector": "...", '
    '"rationale": "...", "risk": "low|medium|high"}, ...]   (<=3 entries),\n'
    '  "modal_open":   true | false | null,\n'
    '  "sensitive_regions": [{"hint": "...", "bbox": [x, y, w, h]}, ...]\n'
    "}\n"
    "\n"
    "Rules: ≤2 sentences per text field; ≤8 controls; ≤3 candidate actions. "
    "Output JSON only — no prose preamble, no code fences."
)

_DEFAULT_PROMPT_SCENE = (
    "Identify the application and screen. The screenshot is attached.\n"
    "Return ONLY this JSON (use null for unknowns):\n"
    '{"app": "...", "screen_type": "kebab-case", '
    '"primary_task": "<=1 sentence", "language": "BCP-47 or null"}'
)

_DEFAULT_PROMPT_CONTROLS = (
    "You are inventorying the interactive controls on a computer screen for "
    "an agentic AI system. The screenshot is attached.\n"
    "\n"
    "The <ACCESSIBILITY_TREE>, <OCR_TEXT>, and <ASCII_SKETCH> blocks below "
    "are GROUND TRUTH. Use the tree's role/name/state values verbatim. Use "
    "OCR text verbatim for label/value strings. Use the sketch's "
    "tab-index badges and legend keys to reference positions stably.\n"
    "\n"
    "Return ONLY this JSON (use null when not visible — never guess):\n"
    "{\n"
    '  "focused":      {"role": "...", "name": "...", "tree_id": "..."} or '
    "null,\n"
    '  "modal_open":   true | false,\n'
    '  "controls": [\n'
    "    {\n"
    '      "role":           "button | menuitem | edit | checkbox | combo | '
    'tab | link | ...",\n'
    '      "name":           "verbatim label",\n'
    '      "state":          "enabled | disabled | checked | unchecked | '
    'selected | expanded | collapsed",\n'
    '      "bbox_hint":      [x, y, w, h] in screen pixels (or null),\n'
    '      "selector_hint":  "XPath-ish or CSS-ish selector",\n'
    '      "tree_id":        "id from <ACCESSIBILITY_TREE> if confident, '
    'else null"\n'
    "    }\n"
    "  ],\n"
    '  "sensitive_regions": [{"hint": "...", "bbox": [x, y, w, h]}, ...]\n'
    "}\n"
    "≤8 controls. Output JSON only."
)

_DEFAULT_PROMPT_ACTIONS = (
    "Given this scene and control inventory, propose up to 3 reasonable "
    "next user actions. Return ONLY this JSON:\n"
    '{"next_actions": [{"description": "<=1 sentence", '
    '"target_selector": "selector from controls", '
    '"rationale": "<=1 sentence", '
    '"risk": "low | medium | high"}]}\n'
    "Rules:\n"
    "- target_selector MUST match a selector_hint or tree_id from the "
    "supplied controls.\n"
    "- Mark write/click/destructive actions as medium or high risk.\n"
    "- Output JSON only."
)

_DEFAULT_PROMPT_VERIFY = (
    "Cross-check this control inventory against the accessibility tree. "
    "For each control in <CONTROLS>, decide whether a matching node exists "
    "in <ACCESSIBILITY_TREE>. Return ONLY this JSON:\n"
    '{"confidence": 0.0_to_1.0, "discrepancies": '
    '[{"control_index": int, "issue": "<=1 sentence"}]}'
)


# ─────────────────────────────────────────────────────────────────────────────
# Tolerant JSON parsing for VLM output
# ─────────────────────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _tolerant_json_loads(raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Best-effort JSON decode of VLM output. Returns (obj, error).

    Strategy:
      1. Strip ```json ... ``` fences and try json.loads.
      2. Fall back to the substring between the first '{' and last '}'.
      3. Return (None, error_message) on second failure.
    """
    if raw is None:
        return None, "empty response"
    text = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, None
    except json.JSONDecodeError as e:
        first_err = str(e)
    else:
        return None, "top-level JSON was not an object"

    # Salvage: clip to first '{' .. last '}'.
    lo = text.find("{")
    hi = text.rfind("}")
    if lo >= 0 and hi > lo:
        try:
            obj = json.loads(text[lo:hi + 1])
            if isinstance(obj, dict):
                return obj, None
        except json.JSONDecodeError as e:
            return None, f"{first_err}; salvage failed: {e}"
    return None, first_err


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

    # ── VLM context-block helpers ────────────────────────────────────────────

    def _build_context_blocks(
        self,
        root:             Optional[UIElement],
        screenshot_bytes: Optional[bytes],
        window:           Optional[WindowInfo],
    ) -> str:
        """Assemble the optional <ACCESSIBILITY_TREE>, <OCR_TEXT>,
        <ASCII_SKETCH>, and <FOCUSED_ELEMENT> envelopes that ground the VLM.

        Each block is gated by its ``ground_with_*`` config flag and silently
        omitted on failure or when empty — the prompt remains valid with the
        screenshot alone.
        """
        parts: List[str] = []

        want_tree   = bool(self.vlm_cfg.get("ground_with_tree",   True))
        want_ocr    = bool(self.vlm_cfg.get("ground_with_ocr",    True))
        want_sketch = bool(self.vlm_cfg.get("ground_with_sketch", True))
        want_focus  = bool(self.vlm_cfg.get("ground_with_focus",  True))

        tree_max_lines   = int(self.vlm_cfg.get("tree_max_lines",   80))
        ocr_max_chars    = int(self.vlm_cfg.get("ocr_max_chars",    4000))
        sketch_max_chars = int(self.vlm_cfg.get("sketch_max_chars", 6000))

        # <ACCESSIBILITY_TREE>
        if want_tree and root is not None:
            try:
                tree_text = self.from_tree(root, window)
                if tree_text and not tree_text.startswith("[Tree description failed"):
                    lines = tree_text.splitlines()
                    if len(lines) > tree_max_lines:
                        lines = lines[:tree_max_lines] + [
                            f"… [tree truncated to {tree_max_lines} lines]"
                        ]
                    parts.append(
                        "<ACCESSIBILITY_TREE>\n"
                        + "\n".join(lines)
                        + "\n</ACCESSIBILITY_TREE>"
                    )
            except Exception as e:
                logger.debug("[_build_context_blocks] tree skipped: %s", e)

        # <OCR_TEXT>
        if (want_ocr and screenshot_bytes is not None
                and self.ocr_cfg.get("enabled", True)):
            try:
                ocr_text = self.from_ocr(screenshot_bytes)
                if ocr_text and not ocr_text.startswith("["):
                    if len(ocr_text) > ocr_max_chars:
                        ocr_text = ocr_text[:ocr_max_chars] + "… [truncated]"
                    parts.append(
                        f"<OCR_TEXT>\n{ocr_text}\n</OCR_TEXT>"
                    )
            except Exception as e:
                logger.debug("[_build_context_blocks] ocr skipped: %s", e)

        # <ASCII_SKETCH>  — lazy import so projects that disable sketch grounding
        # don't pay the import cost (ascii_renderer pulls in PIL transitively).
        if want_sketch and root is not None:
            try:
                from ascii_renderer import ASCIIRenderer
                renderer = ASCIIRenderer(self.config)
                ref = window.bounds if window else root.bounds
                result = renderer.render_structured(
                    root             = root,
                    screen_bounds    = ref,
                    screenshot_bytes = screenshot_bytes,
                )
                sketch_text = result.get("sketch") or ""
                legend = result.get("legend") or {}
                if legend:
                    legend_lines = ["LEGEND:"] + [
                        f"  {k}: {v}" for k, v in legend.items()
                    ]
                    sketch_text = sketch_text + "\n" + "\n".join(legend_lines)
                if sketch_text and not sketch_text.startswith("[ASCII render"):
                    if len(sketch_text) > sketch_max_chars:
                        sketch_text = (sketch_text[:sketch_max_chars]
                                       + "… [sketch truncated]")
                    parts.append(
                        f"<ASCII_SKETCH>\n{sketch_text}\n</ASCII_SKETCH>"
                    )
            except Exception as e:
                logger.debug("[_build_context_blocks] sketch skipped: %s", e)

        # <FOCUSED_ELEMENT>
        if want_focus and root is not None:
            try:
                focused = _find_focused(root)
                if focused is not None:
                    name = f' "{focused.name}"' if focused.name else ""
                    elem_id = getattr(focused, "element_id", None)
                    id_str = f" tree_id={elem_id}" if elem_id else ""
                    parts.append(
                        "<FOCUSED_ELEMENT>\n"
                        f"{focused.role}{name}{id_str}\n"
                        "</FOCUSED_ELEMENT>"
                    )
            except Exception as e:
                logger.debug("[_build_context_blocks] focus skipped: %s", e)

        return "\n\n".join(parts)

    # ── VLM HTTP helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _prepare_image(screenshot_bytes: bytes, max_dim: int) -> bytes:
        """Downscale PNG to *max_dim* on the long edge, preserving aspect.

        Returns the original bytes when Pillow is unavailable, the image is
        already small enough, or any error occurs — never raises.
        """
        if not screenshot_bytes or max_dim <= 0:
            return screenshot_bytes
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(screenshot_bytes))
            w, h = img.size
            long_edge = max(w, h)
            if long_edge <= max_dim:
                return screenshot_bytes
            scale = max_dim / long_edge
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            resized = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        except Exception as e:
            logger.debug("[_prepare_image] passthrough (%s)", e)
            return screenshot_bytes

    def _post_vlm(
        self,
        prompt:          str,
        screenshot_bytes: Optional[bytes],
        *,
        model:           Optional[str] = None,
        max_tokens:      Optional[int] = None,
        temperature:     Optional[float] = None,
        timeout_s:       Optional[float] = None,
    ) -> Optional[str]:
        """Single chat-completions request. Returns assistant text or None.

        Centralises auth, redirect-refusal, prefix-fallback, and image
        preparation so every pass shares one implementation.
        """
        chosen_model = model or self.vlm_cfg.get("model")
        if not chosen_model:
            logger.debug("[_post_vlm] no model configured")
            return None

        base_url = self.vlm_cfg.get("base_url") or "http://localhost:3000"
        api_key  = (self.vlm_cfg.get("api_key")
                    or os.environ.get("OWUI_API_KEY", ""))
        max_tok  = max_tokens if max_tokens is not None else \
                   self.vlm_cfg.get("max_tokens", 1500)
        temp     = (temperature if temperature is not None
                    else self.vlm_cfg.get("temperature", 0.1))
        timeout  = float(timeout_s if timeout_s is not None
                         else self.vlm_cfg.get("timeout_s", 240))

        content: List[Dict[str, Any]] = []
        if screenshot_bytes is not None:
            img_max = int(self.vlm_cfg.get("image_max_dim", 1600))
            prepared = self._prepare_image(screenshot_bytes, img_max)
            b64_img = base64.b64encode(prepared).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64_img}"},
            })
        content.append({"type": "text", "text": prompt})

        payload: Dict[str, Any] = {
            "model":      chosen_model,
            "max_tokens": max_tok,
            "messages":   [{"role": "user", "content": content}],
        }
        if temp is not None:
            payload["temperature"] = float(temp)

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

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
                with opener.open(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code == 404:
                    continue
                break
            except Exception as e:
                last_exc = e
                break
        logger.warning("[_post_vlm] VLM unavailable (%s) — skipping", last_exc)
        return None

    # ── VLM single-shot (grounded) ────────────────────────────────────────────

    def from_vlm(
        self,
        screenshot_bytes: bytes,
        *,
        root:   Optional[UIElement] = None,
        window: Optional[WindowInfo] = None,
    ) -> Optional[str]:
        """Single-shot VLM call, optionally grounded with tree/OCR/sketch.

        Backwards-compatible: when ``root`` is None, this behaves like the
        original implementation (screenshot + prompt only). When ``root`` is
        supplied and the corresponding ``ground_with_*`` flags are true, the
        accessibility tree, OCR text, ASCII sketch, and focused element are
        appended as ``<X>...</X>`` envelopes after the user prompt.

        Returns the model's response string on success, or ``None`` if the
        endpoint is unavailable or not configured.
        """
        if not self.vlm_cfg.get("enabled", False):
            return None
        if not self.vlm_cfg.get("model"):
            logger.debug("[from_vlm] vlm.model not configured — skipping VLM")
            return None

        # Legacy "prompt" key remains honoured as a synonym for "prompt_single".
        prompt_txt = (
            self.vlm_cfg.get("prompt_single")
            or self.vlm_cfg.get("prompt")
            or _DEFAULT_PROMPT_SINGLE
        )

        ctx = self._build_context_blocks(root, screenshot_bytes, window)
        full_prompt = f"{prompt_txt}\n\n{ctx}" if ctx else prompt_txt

        return self._post_vlm(full_prompt, screenshot_bytes)

    # ── VLM multi-pass ────────────────────────────────────────────────────────

    def from_vlm_multipass(
        self,
        screenshot_bytes: bytes,
        *,
        root:   Optional[UIElement] = None,
        window: Optional[WindowInfo] = None,
    ) -> Optional[Dict[str, Any]]:
        """Three-pass VLM pipeline returning a structured JSON envelope.

        Pass 1 (scene)    — small model, screenshot only.
        Pass 2 (controls) — primary model, screenshot + grounding blocks.
        Pass 3 (actions)  — text-only, no image; uses pass 1+2 results.
        Pass V (verify)   — optional second model, no image; cross-checks
                            pass-2 controls against the accessibility tree.

        Each pass is independently fault-tolerant: a failed pass yields null
        fields in the envelope rather than aborting the call. Returns None
        only when VLM is disabled or no model is configured.
        """
        if not self.vlm_cfg.get("enabled", False):
            return None
        primary = self.vlm_cfg.get("model")
        if not primary:
            logger.debug("[from_vlm_multipass] vlm.model not configured")
            return None

        fast    = self.vlm_cfg.get("model_fast")    or primary
        actions = self.vlm_cfg.get("model_actions") or primary
        verify  = self.vlm_cfg.get("model_verify")  # optional

        env: Dict[str, Any] = {
            "summary":            None,
            "app":                None,
            "screen_type":        None,
            "primary_task":       None,
            "focused":            None,
            "controls":           [],
            "next_actions":       [],
            "modal_open":         None,
            "sensitive_regions": [],
            "confidence":         None,
            "discrepancies":      [],
            "_passes":            {},
        }

        # ── Pass 1: scene ────────────────────────────────────────────────────
        t0 = time.time()
        prompt1 = self.vlm_cfg.get("prompt_scene") or _DEFAULT_PROMPT_SCENE
        if window is not None:
            prompt1 += (f"\n\nWindow title: {window.title!r}\n"
                        f"Process: {window.process_name!r}")
        raw1 = self._post_vlm(prompt1, screenshot_bytes, model=fast,
                              max_tokens=400)
        env["_passes"]["scene_ms"] = int((time.time() - t0) * 1000)
        scene_obj: Dict[str, Any] = {}
        if raw1:
            scene_obj, err = _tolerant_json_loads(raw1)
            if scene_obj is None:
                scene_obj = {}
                env["_passes"]["scene_error"] = err
            else:
                for k in ("app", "screen_type", "primary_task"):
                    if scene_obj.get(k) is not None:
                        env[k] = scene_obj[k]

        # ── Pass 2: controls (grounded) ──────────────────────────────────────
        t0 = time.time()
        prompt2 = self.vlm_cfg.get("prompt_controls") or _DEFAULT_PROMPT_CONTROLS
        ctx = self._build_context_blocks(root, screenshot_bytes, window)
        full2 = f"{prompt2}\n\n{ctx}" if ctx else prompt2
        raw2 = self._post_vlm(full2, screenshot_bytes, model=primary)
        env["_passes"]["controls_ms"] = int((time.time() - t0) * 1000)
        controls_obj: Dict[str, Any] = {}
        if raw2:
            controls_obj, err = _tolerant_json_loads(raw2)
            if controls_obj is None:
                controls_obj = {}
                env["_passes"]["controls_error"] = err
            else:
                for k in ("focused", "modal_open", "controls",
                          "sensitive_regions"):
                    if controls_obj.get(k) is not None:
                        env[k] = controls_obj[k]

        # ── Pass 3: next-action candidates (no image) ────────────────────────
        t0 = time.time()
        prompt3 = self.vlm_cfg.get("prompt_actions") or _DEFAULT_PROMPT_ACTIONS
        ctx3 = (
            f"<SCENE>\n{json.dumps(scene_obj, ensure_ascii=False)}\n</SCENE>\n\n"
            f"<CONTROLS>\n{json.dumps(env.get('controls') or [], ensure_ascii=False)}\n</CONTROLS>"
        )
        full3 = f"{prompt3}\n\n{ctx3}"
        raw3 = self._post_vlm(full3, None, model=actions, max_tokens=600)
        env["_passes"]["actions_ms"] = int((time.time() - t0) * 1000)
        if raw3:
            actions_obj, err = _tolerant_json_loads(raw3)
            if actions_obj is None:
                env["_passes"]["actions_error"] = err
            elif isinstance(actions_obj.get("next_actions"), list):
                env["next_actions"] = actions_obj["next_actions"]

        # ── Pass V: verify (optional) ────────────────────────────────────────
        if verify and root is not None:
            t0 = time.time()
            promptv = self.vlm_cfg.get("prompt_verify") or _DEFAULT_PROMPT_VERIFY
            tree_text = ""
            try:
                tree_text = self.from_tree(root, window)
            except Exception:
                pass
            ctxv = (
                f"<CONTROLS>\n"
                f"{json.dumps(env.get('controls') or [], ensure_ascii=False)}\n"
                f"</CONTROLS>\n\n"
                f"<ACCESSIBILITY_TREE>\n{tree_text}\n</ACCESSIBILITY_TREE>"
            )
            rawv = self._post_vlm(f"{promptv}\n\n{ctxv}", None,
                                  model=verify, max_tokens=400)
            env["_passes"]["verify_ms"] = int((time.time() - t0) * 1000)
            if rawv:
                verify_obj, err = _tolerant_json_loads(rawv)
                if verify_obj is None:
                    env["_passes"]["verify_error"] = err
                else:
                    if verify_obj.get("confidence") is not None:
                        env["confidence"] = verify_obj["confidence"]
                    if isinstance(verify_obj.get("discrepancies"), list):
                        env["discrepancies"] = verify_obj["discrepancies"]
        else:
            env["_passes"]["verify_ms"] = 0

        # ── Synthesise a human summary if pass 2 didn't supply one ──────────
        if env.get("summary") is None:
            bits: List[str] = []
            if env.get("app"):
                bits.append(env["app"])
            if env.get("screen_type"):
                bits.append(f"({env['screen_type']})")
            if env.get("primary_task"):
                bits.append(f"— {env['primary_task']}")
            if bits:
                env["summary"] = " ".join(bits)

        return env

    # ── Combined ──────────────────────────────────────────────────────────────

    def combined(
        self,
        root:             UIElement,
        screenshot_bytes: Optional[bytes],
        window:           Optional[WindowInfo] = None,
    ) -> Dict[str, Any]:
        """Return all enabled descriptions in a keyed dict.

        Keys:
          - accessibility : prose serialisation of the element tree (always).
          - ocr           : Tesseract output (when ocr.enabled and screenshot
                            present).
          - vlm           : string form of the VLM output. In single mode this
                            is the raw response. In multipass mode this is the
                            JSON envelope serialised with json.dumps(indent=2).
          - vlm_structured: when multipass mode produced an envelope, the
                            envelope is also exposed as a nested dict here so
                            structured consumers don't have to re-parse.
        """
        result: Dict[str, Any] = {
            "accessibility": self.from_tree(root, window)
        }
        if screenshot_bytes:
            if self.ocr_cfg.get("enabled", True):
                result["ocr"] = self.from_ocr(screenshot_bytes)
            if self.vlm_cfg.get("enabled", False):
                mode = (self.vlm_cfg.get("mode") or "single").lower()
                if mode == "multipass":
                    env = self.from_vlm_multipass(
                        screenshot_bytes, root=root, window=window,
                    )
                    if env is not None:
                        result["vlm"] = json.dumps(env, indent=2,
                                                   ensure_ascii=False)
                        result["vlm_structured"] = env
                else:
                    vlm_out = self.from_vlm(
                        screenshot_bytes, root=root, window=window,
                    )
                    if vlm_out is not None:
                        result["vlm"] = vlm_out
        return result

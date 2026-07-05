"""
Pixel-derived observations: screenshots, OCR, descriptions.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

from errors import Code, error_dict
from observer import UIElement

from tools.context import (
    ToolContext, _find_by_id, _focused_window, _new_step_id, _resolve_window,
)

logger = logging.getLogger(__name__)


def get_screenshot(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_screenshot")
    windows, res = _resolve_window(ctx, args)
    info = res.info
    hwnd = info.handle if info else None
    shot = ctx.observer.get_screenshot(hwnd)
    if shot is None:
        return error_dict(Code.INTERNAL, "screenshot capture failed",
                          step_id=step_id)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window": info.title if info else "(full screen)",
        "format": "png", "encoding": "base64",
        "data": base64.b64encode(shot).decode(),
    }


def get_screenshot_cropped(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """get_screenshot with optional bbox / element_id / max_width / padding."""
    step_id, caused_by = _new_step_id("get_screenshot_cropped")
    windows, res = _resolve_window(ctx, args)
    info = res.info
    hwnd = info.handle if info else None
    shot = ctx.observer.get_screenshot(hwnd)
    if shot is None:
        return error_dict(Code.INTERNAL, "screenshot capture failed",
                          step_id=step_id)

    bbox: Optional[Dict[str, int]] = args.get("bbox")
    element_id: Optional[str] = args.get("element_id")
    padding = int(args.get("padding_px", 0))
    max_width: Optional[int] = args.get("max_width")

    if bbox is None and element_id and info is not None:
        tree = ctx.observer.get_element_tree(info.handle,
                                             window_uid=info.window_uid)
        if tree is not None:
            elem = _find_by_id(tree, element_id)
            if elem is not None:
                # Convert to window-relative coordinates.
                bbox = {
                    "x": max(0, elem.bounds.x - info.bounds.x),
                    "y": max(0, elem.bounds.y - info.bounds.y),
                    "width":  elem.bounds.width,
                    "height": elem.bounds.height,
                }

    if bbox or max_width:
        shot, source_bbox = _apply_crop(shot, bbox, padding, max_width)
    else:
        source_bbox = None

    out: Dict[str, Any] = {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window": info.title if info else "(full screen)",
        "format": "png", "encoding": "base64",
        "data": base64.b64encode(shot).decode(),
    }
    if source_bbox:
        out["source_bbox"] = source_bbox
    return out


def _apply_crop(png_bytes: bytes, bbox: Optional[Dict[str, int]],
                padding: int, max_width: Optional[int]
                ) -> Tuple[bytes, Optional[Dict[str, int]]]:
    try:
        import io as _io
        from PIL import Image
    except Exception:
        return png_bytes, None
    img: "Image.Image" = Image.open(_io.BytesIO(png_bytes))
    source_bbox: Optional[Dict[str, int]] = None
    if bbox is not None:
        x = max(0, int(bbox.get("x", 0)) - padding)
        y = max(0, int(bbox.get("y", 0)) - padding)
        x2 = min(img.width,  int(bbox.get("x", 0)) + int(bbox.get("width",  0)) + padding)
        y2 = min(img.height, int(bbox.get("y", 0)) + int(bbox.get("height", 0)) + padding)
        if x2 > x and y2 > y:
            img = img.crop((x, y, x2, y2))
            source_bbox = {"x": x, "y": y, "width": x2 - x, "height": y2 - y}
    if max_width and img.width > int(max_width):
        ratio = int(max_width) / float(img.width)
        new_size = (int(max_width), max(1, int(img.height * ratio)))
        img = img.resize(new_size)
    buf = __import__("io").BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), source_bbox


def get_ocr(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Region-scoped OCR; returns [{text, confidence, bbox}]."""
    step_id, caused_by = _new_step_id("get_ocr")
    try:
        import io as _io
        from PIL import Image
        import pytesseract
        from ocr_util import configure as _ocr_configure
        _ocr_configure(ctx.config)
    except Exception:
        from ocr_util import INSTALL_HINT
        return error_dict(Code.PLATFORM_UNSUPPORTED,
                          f"pytesseract / Pillow not installed.  {INSTALL_HINT}",
                          step_id=step_id, hint=INSTALL_HINT)
    windows, res = _resolve_window(ctx, args)
    info = res.info
    if info is None:
        return error_dict(Code.BAD_REQUEST,
                          "window_uid or window_index is required",
                          step_id=step_id)
    shot = ctx.observer.get_screenshot(info.handle)
    if shot is None:
        return error_dict(Code.INTERNAL, "screenshot capture failed",
                          step_id=step_id)
    bbox = args.get("bbox")
    element_id = args.get("element_id")
    if element_id and not bbox:
        tree = ctx.observer.get_element_tree(info.handle,
                                             window_uid=info.window_uid)
        if tree is not None:
            elem = _find_by_id(tree, element_id)
            if elem is not None:
                bbox = {
                    "x": max(0, elem.bounds.x - info.bounds.x),
                    "y": max(0, elem.bounds.y - info.bounds.y),
                    "width":  elem.bounds.width,
                    "height": elem.bounds.height,
                }

    img: "Image.Image" = Image.open(_io.BytesIO(shot))
    if bbox:
        x = max(0, int(bbox.get("x", 0)))
        y = max(0, int(bbox.get("y", 0)))
        x2 = min(img.width,  x + int(bbox.get("width",  0)))
        y2 = min(img.height, y + int(bbox.get("height", 0)))
        if x2 > x and y2 > y:
            img = img.crop((x, y, x2, y2))

    min_conf = (ctx.config.get("ocr", {}) or {}).get("min_confidence", 30)
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError:
        from ocr_util import diagnose as _ocr_diag, INSTALL_HINT
        return error_dict(
            Code.PLATFORM_UNSUPPORTED,
            ("tesseract binary not found — check ocr.tesseract_cmd in "
             f"config.json.  {INSTALL_HINT}"),
            step_id=step_id, **_ocr_diag(ctx.config),
        )
    except Exception as e:
        return error_dict(Code.INTERNAL, f"OCR failed: {e}",
                          step_id=step_id)
    out_words: List[Dict[str, Any]] = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        try:
            conf = int(data["conf"][i])
        except (TypeError, ValueError):
            conf = 0
        if conf < min_conf:
            continue
        out_words.append({
            "text": text, "confidence": conf,
            "bbox": {"x": int(data["left"][i]), "y": int(data["top"][i]),
                     "width":  int(data["width"][i]),
                     "height": int(data["height"][i])},
        })
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window": info.title, "window_uid": info.window_uid,
        "words": out_words,
    }


def get_screen_description(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Combined description: accessibility tree + OCR + VLM, returning every available source."""
    step_id, caused_by = _new_step_id("get_screen_description")
    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)
    tree = ctx.observer.get_element_tree(info.handle,
                                         window_uid=info.window_uid)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id)
    max_tokens = args.get("max_tokens")
    focus_id = args.get("focus_element")

    sub: UIElement = tree
    if focus_id:
        found = _find_by_id(tree, focus_id)
        if found is not None:
            sub = found

    parts: Dict[str, str] = {}

    # Accessibility tree — always attempted.
    try:
        parts["accessibility"] = ctx.describer.from_tree(sub, info)
    except Exception as e:
        logger.exception("[get_screen_description] accessibility failed: %s", e)

    # OCR — attempted when enabled in config.
    ocr_enabled = (ctx.config.get("ocr", {}) or {}).get("enabled", True)
    if ocr_enabled:
        try:
            shot = ctx.observer.get_screenshot(info.handle)
            if shot:
                parts["ocr"] = ctx.describer.from_ocr(shot)
            else:
                logger.warning("[get_screen_description] screenshot unavailable for OCR")
        except Exception as e:
            logger.exception("[get_screen_description] OCR failed: %s", e)

    # VLM — attempted when enabled in config. In multipass mode the VLM
    # output is a structured envelope; the JSON-serialised form is folded
    # into the concatenated body (for back-compat with the legacy text
    # description) and the parsed dict is returned separately under
    # ``vlm_structured`` so callers don't have to re-parse it.
    vlm_structured: Any = None
    vlm_enabled = (ctx.config.get("vlm", {}) or {}).get("enabled", False)
    if vlm_enabled:
        try:
            shot = ctx.observer.get_screenshot(info.handle)
            if shot:
                vlm_mode = (
                    (ctx.config.get("vlm", {}) or {}).get("mode") or "single"
                ).lower()
                if vlm_mode == "multipass":
                    env = ctx.describer.from_vlm_multipass(
                        shot, root=sub, window=info,
                    )
                    if env is not None:
                        import json as _json
                        parts["vlm"] = _json.dumps(env, indent=2,
                                                   ensure_ascii=False)
                        vlm_structured = env
                else:
                    vlm_out = ctx.describer.from_vlm(
                        shot, root=sub, window=info,
                    )
                    if vlm_out is not None:
                        parts["vlm"] = vlm_out
            else:
                logger.warning("[get_screen_description] screenshot unavailable for VLM")
        except Exception as e:
            logger.exception("[get_screen_description] VLM failed: %s", e)

    body = ""
    if parts:
        body = "\n\n".join(f"[{k}]\n{v}" for k, v in parts.items())
    else:
        body = "[no description available]"

    truncated = False
    if max_tokens is not None:
        char_cap = int(max_tokens) * 4   # rough chars-per-token
        if len(body) > char_cap:
            body = body[:char_cap] + "… [truncated]"
            truncated = True

    result: Dict[str, Any] = {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window": info.title, "window_uid": info.window_uid,
        "effective_mode": "combined",
        "description": body,
        "truncated": truncated,
    }
    if vlm_structured is not None:
        result["vlm_structured"] = vlm_structured
    return result

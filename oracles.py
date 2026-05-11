"""
oracles.py — Declarative state assertions (design doc §15.6).

assert_state(predicate=[…]) takes an AND list of predicates and returns
{ok, all_passed, results: [{kind, args, passed, observed}]}.  Never raises.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import element_selectors as sel
from errors import Code, error_dict
from hashing import tree_hash
from observer import ScreenObserver, UIElement, WindowInfo

logger = logging.getLogger(__name__)


PREDICATE_KINDS = {
    "element_exists", "element_absent", "value_equals", "value_matches",
    "text_visible", "window_focused", "window_exists", "tree_hash_equals",
    "screenshot_similar",
}


def evaluate(observer: ScreenObserver,
             predicates: List[Dict[str, Any]],
             *,
             config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(predicates, list) or not predicates:
        return error_dict(Code.BAD_REQUEST, "predicates list required")

    windows = observer.list_windows()
    results: List[Dict[str, Any]] = []
    all_passed = True
    for p in predicates:
        if not isinstance(p, dict):
            results.append({"kind": "?", "passed": False,
                            "observed": "predicate must be a mapping"})
            all_passed = False
            continue
        kind = p.get("kind")
        if kind not in PREDICATE_KINDS:
            results.append({"kind": kind or "?", "passed": False,
                            "observed": "unknown predicate kind",
                            "args": p})
            all_passed = False
            continue
        try:
            passed, observed = _run(kind, p, observer, windows, config)
        except Exception as e:
            logger.exception(f"oracle {kind} crashed")
            passed, observed = False, f"error: {type(e).__name__}: {e}"
        results.append({"kind": kind, "passed": bool(passed),
                        "observed": observed,
                        "args": {k: v for k, v in p.items() if k != "kind"}})
        if not passed:
            all_passed = False

    return {
        "ok": True, "success": True,
        "all_passed": all_passed,
        "results": results,
    }


# ─── Implementations ──────────────────────────────────────────────────────────

def _run(kind: str, p: Dict[str, Any], observer: ScreenObserver,
         windows: List[WindowInfo], config: Dict[str, Any]
         ) -> tuple:
    if kind == "window_exists":
        title_rx = p.get("title_regex")
        uid = p.get("window_uid")
        for w in windows:
            if uid and w.window_uid == uid:
                return True, {"window_uid": w.window_uid, "title": w.title}
            if title_rx and re.search(title_rx, w.title):
                return True, {"window_uid": w.window_uid, "title": w.title}
        return False, "no matching window"

    if kind == "window_focused":
        rx = p.get("title_regex", "")
        for w in windows:
            if w.is_focused and re.search(rx, w.title):
                return True, {"window_uid": w.window_uid, "title": w.title}
        return False, "focused window does not match"

    info = _resolve_window(observer, windows, p)
    tree: Optional[UIElement] = (
        observer.get_element_tree(info.handle) if info else None
    )

    if kind in {"element_exists", "element_absent",
                "value_equals", "value_matches"}:
        if tree is None:
            return False, "no tree"
        sel_text = p.get("selector")
        if not sel_text:
            return False, "selector required"
        try:
            res = sel.resolve(tree, sel.parse(sel_text))
        except sel.SelectorParseError as e:
            return False, f"selector parse: {e}"

        if kind == "element_exists":
            return bool(res.matches), (
                {"count": len(res.matches),
                 "first_id": res.matches[0].element_id if res.matches else None}
            )
        if kind == "element_absent":
            return not res.matches, {"count": len(res.matches)}
        if kind == "value_equals":
            if not res.matches:
                return False, "no matches"
            actual = res.matches[0].value
            return actual == p.get("expected"), {"actual": actual}
        if kind == "value_matches":
            if not res.matches:
                return False, "no matches"
            actual = res.matches[0].value or ""
            rx = p.get("regex", "")
            return re.search(rx, actual) is not None, {"actual": actual}

    if kind == "text_visible":
        rx = p.get("regex", "")
        mode = p.get("mode", "auto")
        # Tree pass.
        if tree is not None and mode in ("auto", "tree"):
            for elem in tree.flat_list():
                joined = (elem.name or "") + " " + (elem.value or "")
                if re.search(rx, joined):
                    return True, {"source": "tree",
                                  "element_id": elem.element_id}
        # OCR pass (only when explicitly requested or tree miss in auto).
        if mode in ("ocr", "auto") and info is not None:
            try:
                import io
                from PIL import Image
                import pytesseract
                from ocr_util import configure as _ocr_configure
                _ocr_configure(config)
            except Exception:
                return False, "ocr unavailable"
            shot = observer.get_screenshot(info.handle)
            if shot:
                try:
                    text = pytesseract.image_to_string(Image.open(io.BytesIO(shot)))
                except pytesseract.TesseractNotFoundError:
                    from ocr_util import diagnose as _ocr_diag
                    return False, {"ocr_error": "tesseract not found",
                                   "diagnose": _ocr_diag(config)}
                if re.search(rx, text or ""):
                    return True, {"source": "ocr"}
        return False, "no match"

    if kind == "tree_hash_equals":
        if tree is None:
            return False, "no tree"
        actual = tree_hash(tree)
        return actual == p.get("expected_hash"), {"actual": actual}

    if kind == "screenshot_similar":
        try:
            import io
            from PIL import Image
            import numpy as np
            from skimage.metrics import structural_similarity as ssim
        except Exception:
            return False, "scikit-image not installed"
        ref_path = p.get("reference_path")
        min_ssim = float(p.get("min_ssim", 0.95))
        if not ref_path or not info:
            return False, "reference_path and window required"
        shot = observer.get_screenshot(info.handle)
        if not shot:
            return False, "no screenshot"
        a = np.array(Image.open(io.BytesIO(shot)).convert("L"))
        b = np.array(Image.open(ref_path).convert("L"))
        if a.shape != b.shape:
            # Resize ref to actual.
            from PIL import Image as _Im
            b = np.array(_Im.open(ref_path).convert("L").resize(
                (a.shape[1], a.shape[0])))
        score = float(ssim(a, b))
        return score >= min_ssim, {"ssim": score}

    return False, "unhandled"


def _resolve_window(observer: ScreenObserver, windows: List[WindowInfo],
                    p: Dict[str, Any]) -> Optional[WindowInfo]:
    uid = p.get("window_uid")
    if uid:
        return observer.window_by_uid(windows, uid)
    return next((w for w in windows if w.is_focused), windows[0] if windows else None)

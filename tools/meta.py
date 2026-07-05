"""
Introspection tools: windows, capabilities, status, proposals.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict

from errors import Code, error_dict
from session import get_session

from tools.context import (
    ToolContext, _focused_window, _new_step_id, _resolve_element,
    _resolve_window,
)
from tools.vision import _apply_crop

logger = logging.getLogger(__name__)


def list_windows(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("list_windows")
    try:
        windows = ctx.observer.list_windows()
        return {
            "ok": True, "success": True,
            "step_id": step_id, "caused_by_step_id": caused_by,
            "is_mock": ctx.observer.is_mock,
            "count": len(windows),
            "windows": [{"index": i, **w.to_dict()} for i, w in enumerate(windows)],
        }
    except Exception as e:
        logger.exception("list_windows failed")
        return error_dict(Code.INTERNAL, str(e), step_id=step_id)


def get_capabilities(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_capabilities")
    out = ctx.observer.get_capabilities()
    out.update({"step_id": step_id, "caused_by_step_id": caused_by, "success": True})
    return out


def get_monitors(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_monitors")
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "monitors": ctx.observer.get_monitors(),
    }


def get_budget_status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_budget_status")
    sess = get_session()
    if sess.budgets is None:
        return {"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "configured": False}
    out = sess.budgets.status()
    out.update({"ok": True, "success": True, "configured": True,
                "step_id": step_id, "caused_by_step_id": caused_by})
    return out


def get_redaction_status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_redaction_status")
    sess = get_session()
    out = sess.redactor.status() if sess.redactor else {"enabled": False, "active": False}
    out.update({"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by})
    return out


def propose_action(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Issue a single-use confirm_token for a destructive action."""
    step_id, caused_by = _new_step_id("propose_action")
    action = args.get("action")
    inner_args = args.get("args") or {}
    if not action:
        return error_dict(Code.BAD_REQUEST, "action is required",
                          step_id=step_id)

    windows, res = _resolve_window(ctx, inner_args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)
    tree = ctx.observer.get_element_tree(info.handle,
                                         window_uid=info.window_uid)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id)
    elem, selector_str, err = _resolve_element(tree, inner_args)
    if err or elem is None:
        return {**(err or error_dict(Code.ELEMENT_NOT_FOUND,
                                     "element resolution failed")),
                "step_id": step_id}

    sess = get_session()
    bbox = elem.bounds.to_dict()
    ct = sess.confirms.issue(
        action=action, window_uid=info.window_uid,
        selector=selector_str or "", bbox=bbox, args=inner_args,
    )
    # Optional preview crop.
    preview_b64 = None
    try:
        shot = ctx.observer.get_screenshot(info.handle)
        if shot is not None:
            crop_bytes, _ = _apply_crop(shot, bbox=bbox, padding=8,
                                        max_width=400)
            if crop_bytes:
                preview_b64 = base64.b64encode(crop_bytes).decode()
    except Exception as e:
        logger.debug(f"propose_action: preview crop failed: {e}")

    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "confirm_token": ct.token,
        "expires_at": ct.expires_at,
        "would_target": {
            "window_uid": info.window_uid,
            "selector": selector_str,
            "bounds": bbox,
            "screenshot_b64": preview_b64,
        },
    }

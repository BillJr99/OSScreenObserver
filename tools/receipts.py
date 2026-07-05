"""
Action receipts and confirmation gating.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from errors import Code, error_dict
from hashing import focused_selector, tree_hash
from observer import UIElement, WindowInfo
from session import get_session

from tools.context import ToolContext, _new_dialogs


def _build_receipt(*, step_id: int, action: str, target: Dict[str, Any],
                   before_tree: Optional[UIElement],
                   before_windows: List[WindowInfo],
                   after_tree: Optional[UIElement],
                   after_windows: List[WindowInfo],
                   duration_ms: int, dry_run: bool, ok: bool,
                   extra: Optional[Dict[str, Any]] = None,
                   error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    before_hash = tree_hash(before_tree) if before_tree else ""
    after_hash  = tree_hash(after_tree)  if after_tree  else ""
    receipt: Dict[str, Any] = {
        "ok": ok, "success": ok,
        "step_id": step_id, "caused_by_step_id": step_id,
        "action": action,
        "dry_run": dry_run,
        "target": target,
        "before": {
            "tree_hash": before_hash,
            "focused_selector": focused_selector(before_tree) if before_tree else "",
        },
        "after": {
            "tree_hash": after_hash,
            "focused_selector": focused_selector(after_tree) if after_tree else "",
        },
        "changed": (before_hash != after_hash) and not dry_run,
        "new_dialogs": _new_dialogs(before_windows, after_windows),
        "duration_ms": duration_ms,
    }
    if extra:
        receipt.update(extra)
    if error:
        receipt["error"] = error
    return receipt


def _confirmation_rules_match(ctx: ToolContext,
                              target: Dict[str, Any]) -> bool:
    """True when any configured confirmation_required rule matches the
    target's role/name (derived from the selector tail)."""
    confirm = ctx.config.get("confirmation_required") or []
    if not confirm:
        return False
    name_to_test = ""
    role_to_test = ""
    # Best-effort: derive name/role from the selector tail.
    sel_tail = (target.get("selector") or "").split("/")[-1]
    m = re.match(r"([A-Za-z_*]\w*)", sel_tail)
    if m:
        role_to_test = m.group(1)
    nm = re.search(r'name="([^"]*)"', sel_tail)
    if nm:
        name_to_test = nm.group(1)

    def _matches_rule(rule: Dict[str, Any]) -> bool:
        rname = rule.get("name_regex")
        rrole = rule.get("role")
        if rrole and role_to_test != rrole:
            return False
        if rname and not re.search(rname, name_to_test or ""):
            return False
        return bool(rname or rrole)

    return any(_matches_rule(r) for r in confirm)


def _check_confirmation(ctx: ToolContext, action_name: str,
                        args: Dict[str, Any],
                        target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Returns an error dict to short-circuit when confirmation is required."""
    if not _confirmation_rules_match(ctx, target):
        return None

    token = args.get("confirm_token")
    if not token:
        return error_dict(
            Code.CONFIRMATION_REQUIRED,
            f"action {action_name} requires a confirm_token from propose_action",
            action=action_name, target=target,
        )
    sess = get_session()
    ct = sess.confirms.consume(token)
    if not ct:
        return error_dict(Code.CONFIRMATION_INVALID,
                          "confirm_token expired, unknown, or already used",
                          token=token)
    if ct.action != action_name or ct.window_uid != target["window_uid"] \
            or ct.selector != target["selector"]:
        return error_dict(Code.CONFIRMATION_INVALID,
                          "confirm_token does not match the proposed action")
    tol = (ctx.config.get("confirmation", {}) or {}).get("bbox_tolerance_px", 20)
    bb = target["bounds"]
    if (abs(bb["x"] - ct.bbox.get("x", 0)) > tol or
            abs(bb["y"] - ct.bbox.get("y", 0)) > tol or
            abs(bb["width"]  - ct.bbox.get("width",  0)) > tol or
            abs(bb["height"] - ct.bbox.get("height", 0)) > tol):
        return error_dict(Code.CONFIRMATION_INVALID,
                          "element bounds drifted beyond confirmation tolerance")
    return None

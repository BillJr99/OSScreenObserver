"""
Dispatch table, allowlist gate and cross-cutting hooks.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from errors import Code, error_dict
from hashing import tree_hash
from redaction import mark_untrusted
from session import get_session

from tools.context import ToolContext, _is_input_tool, _new_step_id
from tools import actions, meta, observe, snapshots, trace_replay, vision

logger = logging.getLogger(__name__)


REGISTRY: Dict[str, Callable[[ToolContext, Dict[str, Any]], Dict[str, Any]]] = {
    # Read-only
    "list_windows":         meta.list_windows,
    "get_capabilities":     meta.get_capabilities,
    "get_monitors":         meta.get_monitors,
    "find_element":         observe.find_element,
    "get_window_structure": observe.get_window_structure,
    "get_screenshot":       vision.get_screenshot,
    "get_visible_areas":    observe.get_visible_areas,

    # Element-targeted actions
    "click_element":   actions.click_element,
    "focus_element":   actions.focus_element,
    "set_value":       actions.set_value,
    "invoke_element":  actions.invoke_element,
    "select_option":   actions.select_option,

    # Legacy actions
    "click_at":              actions.click_at,
    "type_text":             actions.type_text,
    "press_key":             actions.press_key,
    "scroll":                actions.scroll,
    "bring_to_foreground":   actions.bring_to_foreground,

    # P2: sync, diff, snapshots, composites
    "observe_window":   observe.observe_window,
    "snapshot":         snapshots.snapshot,
    "snapshot_get":     snapshots.snapshot_get,
    "snapshot_diff":    snapshots.snapshot_diff,
    "snapshot_drop":    snapshots.snapshot_drop,
    "wait_for":         snapshots.wait_for,
    "wait_idle":        snapshots.wait_idle,
    "click_element_and_observe":  actions.click_element_and_observe,
    "type_and_observe":           actions.type_and_observe,
    "press_key_and_observe":      actions.press_key_and_observe,

    # P3
    "get_screenshot_cropped":  vision.get_screenshot_cropped,
    "get_ocr":                 vision.get_ocr,
    "get_screen_description":  vision.get_screen_description,

    # P4: tracing, replay, scenarios, oracles
    "trace_start":     trace_replay.trace_start,
    "trace_stop":      trace_replay.trace_stop,
    "trace_status":    trace_replay.trace_status,
    "replay_start":    trace_replay.replay_start,
    "replay_step":     trace_replay.replay_step,
    "replay_status":   trace_replay.replay_status,
    "replay_stop":     trace_replay.replay_stop,
    "load_scenario":   trace_replay.load_scenario,
    "assert_state":    trace_replay.assert_state,

    # P5: budgets, propose_action, status reporters
    "get_budget_status":    meta.get_budget_status,
    "get_redaction_status": meta.get_redaction_status,
    "propose_action":       meta.propose_action,

    # P6: extra input verbs
    "hover_at":              actions.hover_at,
    "hover_element":         actions.hover_element,
    "right_click_at":        actions.right_click_at,
    "right_click_element":   actions.right_click_element,
    "double_click_at":       actions.double_click_at,
    "double_click_element":  actions.double_click_element,
    "drag":                  actions.drag,
    "key_into_element":      actions.key_into_element,
    "clear_text":            actions.clear_text,
}


_ALLOWLIST_TOOLS = {
    "get_capabilities", "get_monitors", "get_budget_status",
    "get_redaction_status", "trace_status", "replay_status",
    "list_windows",
}


def _check_allowlist(ctx: ToolContext, name: str
                     ) -> Optional[Dict[str, Any]]:
    actions = ctx.config.get("actions") or {}
    allow = set(actions.get("allow") or [])
    deny = set(actions.get("deny") or [])
    default = actions.get("default", "allow")
    if name in _ALLOWLIST_TOOLS:
        return None
    if name in deny:
        return error_dict(Code.PERMISSION_DENIED,
                          f"tool {name!r} is in actions.deny")
    if allow and name not in allow and default == "deny":
        return error_dict(Code.PERMISSION_DENIED,
                          f"tool {name!r} is not in actions.allow")
    if not allow and default == "deny":
        return error_dict(Code.PERMISSION_DENIED,
                          f"actions.default is 'deny' and no allowlist matches {name!r}")
    return None


def dispatch(ctx: ToolContext, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    fn = REGISTRY.get(name)
    if fn is None:
        return error_dict(Code.BAD_REQUEST, f"unknown tool: {name}")

    blocked = _check_allowlist(ctx, name)
    if blocked is not None:
        sid, _ = _new_step_id(name)
        blocked["step_id"] = sid
        return blocked

    started = time.time()
    sess = get_session()
    tree_before = ""
    if sess.active_trace is not None and not sess.active_trace.closed:
        try:
            windows0 = ctx.observer.list_windows()
            focused0 = next((w for w in windows0 if w.is_focused), None)
            if focused0:
                t = ctx.observer.get_element_tree(
                    focused0.handle, window_uid=focused0.window_uid)
                if t:
                    tree_before = tree_hash(t)
        except Exception as e:
            logger.debug(f"trace: pre-call tree hash unavailable: {e}")

    try:
        result = fn(ctx, args or {})
    except Exception as e:
        logger.exception(f"tool {name} crashed")
        result = error_dict(Code.INTERNAL, f"{type(e).__name__}: {e}")

    duration_ms = int((time.time() - started) * 1000)

    # P1 tree cache invalidation — the single choke point.  After any input
    # tool (including bring_to_foreground) the affected window's cached tree
    # is stale; drop it so subsequent reads re-walk.  When the target window
    # cannot be determined (legacy coordinate tools), drop everything.
    if _is_input_tool(name):
        try:
            uid = None
            if isinstance(result, dict):
                uid = ((result.get("target") or {}).get("window_uid")
                       or result.get("window_uid"))
            uid = uid or (args or {}).get("window_uid")
            if uid:
                sess.tree_cache.invalidate(uid)
            else:
                sess.tree_cache.invalidate_all()
        except Exception:
            logger.exception("tree cache invalidation failed")

    # Recurse-safety: don't trace meta tools that would recurse forever.
    if name not in {"trace_start", "trace_stop", "trace_status",
                    "replay_start", "replay_step", "replay_status",
                    "replay_stop"}:
        if sess.active_trace is not None and not sess.active_trace.closed:
            try:
                import tracing as _tracing
                shot_full = ctx.observer.get_full_display_screenshot()
                shot_window = None
                tgt_uid = None
                tgt = result.get("target") or {}
                tgt_uid = tgt.get("window_uid") or args.get("window_uid")
                if tgt_uid:
                    win = ctx.observer.window_by_uid(
                        ctx.observer.list_windows(), tgt_uid)
                    if win:
                        shot_window = ctx.observer.get_screenshot(win.handle)
                tree_after = ""
                try:
                    after_w = ctx.observer.list_windows()
                    f = next((w for w in after_w if w.is_focused), None)
                    if f:
                        t = ctx.observer.get_element_tree(
                            f.handle, window_uid=f.window_uid,
                            use_cache=False)
                        if t:
                            tree_after = tree_hash(t)
                except Exception as e:
                    logger.debug(f"trace: post-call tree hash "
                                 f"unavailable: {e}")
                _tracing.record(
                    sess.active_trace,
                    tool=name, caller=args.get("_caller", "unknown"),
                    args={k: v for k, v in (args or {}).items()
                          if not k.startswith("_")},
                    result=result, duration_ms=duration_ms,
                    tree_hash_before=tree_before, tree_hash_after=tree_after,
                    full_screenshot=shot_full,
                    window_screenshot=shot_window,
                )
            except Exception:
                logger.exception("trace.record failed")

    # Apply redaction to text-bearing read-only results.
    if sess.redactor is not None:
        try:
            result = _apply_redaction(name, result, sess.redactor)
        except Exception:
            logger.exception("redaction failed")

    # Untrusted-content marking (always on): screen-derived text is
    # attacker-influenced data (prompt injection), never instructions.
    # Flag it and strip ANSI/control characters.
    try:
        result = mark_untrusted(name, result)
    except Exception:
        logger.exception("untrusted-content marking failed")

    # Budget accounting.
    if sess.budgets is not None:
        try:
            sess.budgets.note(name, result)
        except Exception:
            logger.exception("budget accounting failed")

    # Audit log.
    if sess.auditor is not None:
        try:
            sess.auditor.record(
                tool=name, caller=args.get("_caller", "unknown"),
                args=args or {}, result=result,
            )
        except Exception:
            logger.exception("audit failed")

    return result


def _apply_redaction(tool: str, result: Dict[str, Any], redactor: Any
                      ) -> Dict[str, Any]:
    if not redactor.is_active() or not isinstance(result, dict):
        return result
    if tool == "get_window_structure" and "tree" in result:
        title = result.get("window") or ""
        result["tree"] = redactor.redact_tree(result["tree"], title)
    elif tool == "observe_window":
        if "tree" in result and isinstance(result["tree"], dict):
            result["tree"] = redactor.redact_tree(result["tree"],
                                                   result.get("window") or "")
    elif tool == "get_screen_description":
        if isinstance(result.get("description"), str):
            result["description"] = redactor.redact_ocr_text(result["description"])
    elif tool == "get_ocr":
        if isinstance(result.get("words"), list):
            result["words"] = redactor.redact_ocr_words(result["words"])
    return result

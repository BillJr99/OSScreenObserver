"""
Element-targeted and coordinate input verbs (plus composites).

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

import element_selectors as sel
from errors import Code, error_dict, annotate_legacy_result
from observer import UIElement, WindowInfo
from session import get_session

from tools.context import (
    ToolContext, _find_by_id, _focused_window, _new_dialogs, _new_step_id,
    _resolve_element, _resolve_window,
)
from tools.observe import observe_window
from tools.receipts import (
    _build_receipt, _check_confirmation, _confirmation_rules_match,
)

logger = logging.getLogger(__name__)


def _do_element_action(ctx: ToolContext, *, action_name: str, args: Dict[str, Any],
                       executor: Callable[[UIElement, WindowInfo, Dict[str, Any]],
                                          Dict[str, Any]]) -> Dict[str, Any]:
    step_id, _ = _new_step_id(action_name)
    dry_run = bool(args.get("dry_run"))

    # Budget gate (no-op until P5 plumbs budgets in).
    sess = get_session()
    if sess.budgets is not None:
        gate = sess.budgets.gate(action_name)
        if gate is not None:
            return {**gate, "step_id": step_id}

    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)
    tree = ctx.observer.get_element_tree(info.handle,
                                         window_uid=info.window_uid)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id, window_uid=info.window_uid)

    elem, selector_str, err = _resolve_element(tree, args)
    if err or elem is None:
        return {**(err or error_dict(Code.ELEMENT_NOT_FOUND,
                                     "element resolution failed")),
                "step_id": step_id}

    if not elem.enabled:
        return error_dict(Code.ELEMENT_DISABLED,
                          f"element is disabled: {selector_str}",
                          step_id=step_id, selector=selector_str)

    occluded = ctx.observer.is_element_occluded(elem.bounds, info.handle, windows)
    if occluded:
        return error_dict(Code.ELEMENT_OCCLUDED,
                          f"element is occluded: {selector_str}",
                          step_id=step_id, selector=selector_str)

    target = {
        "window_uid": info.window_uid,
        "element_id": elem.element_id,
        "selector": selector_str,
        "bounds": elem.bounds.to_dict(),
    }

    # Confirmation gate (no-op until P5 plumbs confirms in).
    confirm_check = _check_confirmation(ctx, action_name, args, target)
    if confirm_check is not None:
        return {**confirm_check, "step_id": step_id}

    started = time.time()
    executor_result: Dict[str, Any]
    if dry_run:
        executor_result = {"success": True, "dry_run": True}
    else:
        try:
            executor_result = executor(elem, info, args)
        except Exception as e:
            logger.exception(f"{action_name} executor failed")
            executor_result = {"success": False, "error": str(e)}

    duration_ms = int((time.time() - started) * 1000)
    after_windows = ctx.observer.list_windows()
    info_after = ctx.observer.window_by_uid(after_windows, info.window_uid)
    # Post-action re-read must bypass the tree cache so the receipt's
    # after-state reflects reality (the fresh capture refreshes the cache).
    after_tree = (ctx.observer.get_element_tree(info_after.handle,
                                                window_uid=info_after.window_uid,
                                                use_cache=False)
                  if info_after else None)

    ok = bool(executor_result.get("success", True))
    err_obj: Optional[Dict[str, Any]] = None
    if not ok:
        err_obj = {
            "code": executor_result.get("error_code", Code.INTERNAL),
            "message": str(executor_result.get("error", "action failed")),
            "recoverable": False,
            "suggested_next_tool": None,
            "context": {},
        }

    extra: Dict[str, Any] = {}
    if "warning" in (res.warning or ""):
        extra["warning"] = res.warning
    if res.warning:
        extra["warning"] = res.warning

    return _build_receipt(
        step_id=step_id, action=action_name, target=target,
        before_tree=tree, before_windows=windows,
        after_tree=after_tree, after_windows=after_windows,
        duration_ms=duration_ms, dry_run=dry_run, ok=ok,
        extra=extra, error=err_obj,
    )


def click_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    button = args.get("button", "left")
    count = int(args.get("count", 1))

    def _exec(elem: UIElement, info: WindowInfo, _args: Dict[str, Any]
              ) -> Dict[str, Any]:
        cx, cy = elem.bounds.center_x, elem.bounds.center_y
        result = ctx.observer.perform_action(
            "click_at",
            element_id=elem.element_id,
            value={"x": cx, "y": cy, "button": button,
                   "double": (count >= 2)},
            hwnd=info.handle,
        )
        return result

    return _do_element_action(ctx, action_name="click_element",
                              args=args, executor=_exec)


def focus_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    def _exec(elem: UIElement, info: WindowInfo, _a: Dict[str, Any]
              ) -> Dict[str, Any]:
        # Focus via center click (universal fallback).  Adapter-specific
        # SetFocus paths live in the platform adapters; today the universal
        # click is sufficient on all three platforms.
        return ctx.observer.perform_action(
            "click_at",
            value={"x": elem.bounds.center_x, "y": elem.bounds.center_y,
                   "button": "left", "double": False},
            hwnd=info.handle,
        )
    return _do_element_action(ctx, action_name="focus_element",
                              args=args, executor=_exec)


def set_value(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    if "value" not in args:
        step_id, _ = _new_step_id("set_value")
        return error_dict(Code.BAD_REQUEST, "value is required",
                          step_id=step_id)
    value = args["value"]
    clear_first = bool(args.get("clear_first", True))

    def _exec(elem: UIElement, info: WindowInfo, _a: Dict[str, Any]
              ) -> Dict[str, Any]:
        ctx.observer.perform_action(
            "click_at",
            value={"x": elem.bounds.center_x, "y": elem.bounds.center_y,
                   "button": "left", "double": False},
            hwnd=info.handle,
        )
        if clear_first:
            ctx.observer.perform_action("key", value="ctrl+a", hwnd=info.handle)
            ctx.observer.perform_action("key", value="delete", hwnd=info.handle)
        return ctx.observer.perform_action("type", value=str(value),
                                           hwnd=info.handle)
    return _do_element_action(ctx, action_name="set_value",
                              args=args, executor=_exec)


def invoke_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    # No platform-specific InvokePattern surface yet; behaves like click.
    return _do_element_action(
        ctx, action_name="invoke_element", args=args,
        executor=lambda elem, info, _a: ctx.observer.perform_action(
            "click_at",
            value={"x": elem.bounds.center_x, "y": elem.bounds.center_y,
                   "button": "left", "double": False},
            hwnd=info.handle,
        ),
    )


def select_option(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Click the element to open the menu, then click the matching child."""
    option_name = args.get("option_name")
    option_index = args.get("option_index")
    if option_name is None and option_index is None:
        step_id, _ = _new_step_id("select_option")
        return error_dict(Code.BAD_REQUEST,
                          "option_name or option_index is required",
                          step_id=step_id)

    def _exec(elem: UIElement, info: WindowInfo, _a: Dict[str, Any]
              ) -> Dict[str, Any]:
        ctx.observer.perform_action(
            "click_at",
            value={"x": elem.bounds.center_x, "y": elem.bounds.center_y,
                   "button": "left", "double": False},
            hwnd=info.handle,
        )
        # Re-walk to see the now-visible option list (bypass the cache —
        # the click above just changed the UI).
        new_tree = ctx.observer.get_element_tree(info.handle,
                                                 window_uid=info.window_uid,
                                                 use_cache=False)
        target: Optional[UIElement] = None
        if new_tree is not None:
            descendants = []
            stack = [new_tree]
            while stack:
                e = stack.pop()
                descendants.append(e)
                stack.extend(e.children)
            if option_name is not None:
                target = next((d for d in descendants if d.name == option_name), None)
            elif option_index is not None:
                idx = int(option_index)
                if 0 <= idx < len(elem.children):
                    target = elem.children[idx]
        if target is None:
            return {"success": False,
                    "error": "option not found after opening selector"}
        return ctx.observer.perform_action(
            "click_at",
            value={"x": target.bounds.center_x, "y": target.bounds.center_y,
                   "button": "left", "double": False},
            hwnd=info.handle,
        )

    return _do_element_action(ctx, action_name="select_option",
                              args=args, executor=_exec)


def click_at(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("click_at")
    before_windows = ctx.observer.list_windows()
    started = time.time()
    result = ctx.observer.perform_action("click_at", value={
        "x": args.get("x", 0), "y": args.get("y", 0),
        "button": args.get("button", "left"),
        "double": args.get("double", False),
    })
    duration_ms = int((time.time() - started) * 1000)
    after_windows = ctx.observer.list_windows()
    out = annotate_legacy_result(result, step_id=step_id, caused_by_step_id=step_id)
    out.setdefault("action", "click_at")
    out["duration_ms"] = duration_ms
    out["new_dialogs"] = _new_dialogs(before_windows, after_windows)
    return out


def type_text(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("type_text")
    result = ctx.observer.perform_action("type", value=args.get("text", ""))
    return annotate_legacy_result(result, step_id=step_id, caused_by_step_id=step_id)


def press_key(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("press_key")
    result = ctx.observer.perform_action("key", value=args.get("keys", ""))
    return annotate_legacy_result(result, step_id=step_id, caused_by_step_id=step_id)


def scroll(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("scroll")
    result = ctx.observer.perform_action("scroll", value=args)
    return annotate_legacy_result(result, step_id=step_id, caused_by_step_id=step_id)


def bring_to_foreground(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("bring_to_foreground")
    windows, res = _resolve_window(ctx, args)
    info = res.info
    if info is None:
        return error_dict(Code.BAD_REQUEST,
                          "window_uid or window_index is required",
                          step_id=step_id)
    result = ctx.observer.bring_to_foreground(info.handle, windows)
    result["window"]     = info.title
    result["window_uid"] = info.window_uid
    return annotate_legacy_result(result, step_id=step_id, caused_by_step_id=step_id)


def _compose_observe(ctx: ToolContext, base_result: Dict[str, Any],
                     wait_after_ms: int, since_token: Optional[str]) -> None:
    if wait_after_ms > 0:
        time.sleep(wait_after_ms / 1000.0)
    args: Dict[str, Any] = {}
    target = base_result.get("target") or {}
    if target.get("window_uid"):
        args["window_uid"] = target["window_uid"]
    if since_token:
        args["since"] = since_token
    obs = observe_window(ctx, args)
    base_result["observation"] = obs


def click_element_and_observe(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    wait_after_ms = int(args.pop("wait_after_ms", 200))
    since = args.pop("since", None)
    receipt = click_element(ctx, args)
    if receipt.get("ok"):
        _compose_observe(ctx, receipt, wait_after_ms, since)
    return receipt


def type_and_observe(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    wait_after_ms = int(args.pop("wait_after_ms", 200))
    since = args.pop("since", None)
    receipt = type_text(ctx, args)
    if receipt.get("ok"):
        _compose_observe(ctx, receipt, wait_after_ms, since)
    return receipt


def press_key_and_observe(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    wait_after_ms = int(args.pop("wait_after_ms", 200))
    since = args.pop("since", None)
    receipt = press_key(ctx, args)
    if receipt.get("ok"):
        _compose_observe(ctx, receipt, wait_after_ms, since)
    return receipt


def hover_at(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("hover_at")
    x = int(args.get("x", 0))
    y = int(args.get("y", 0))
    hover_ms = int(args.get("hover_ms", 250))
    try:
        import pyautogui
        pyautogui.moveTo(x, y)
        time.sleep(hover_ms / 1000.0)
        ok = True
    except Exception:
        ok = False
    return {"ok": ok, "success": ok, "action": "hover_at",
            "step_id": step_id, "caused_by_step_id": step_id,
            "x": x, "y": y, "hover_ms": hover_ms}


def hover_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    hover_ms = int(args.get("hover_ms", 250))
    def _exec(elem: UIElement, info: WindowInfo, _a: Dict[str, Any]
              ) -> Dict[str, Any]:
        try:
            import pyautogui
            pyautogui.moveTo(elem.bounds.center_x, elem.bounds.center_y)
            time.sleep(hover_ms / 1000.0)
            return {"success": True, "action": "hover", "hover_ms": hover_ms}
        except Exception as e:
            return {"success": False, "error": str(e)}
    return _do_element_action(ctx, action_name="hover_element",
                              args=args, executor=_exec)


def right_click_at(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return click_at(ctx, dict(args, button="right"))


def right_click_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return click_element(ctx, dict(args, button="right"))


def double_click_at(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return click_at(ctx, dict(args, double=True))


def double_click_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return click_element(ctx, dict(args, count=2))


def drag(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("drag")
    src = args.get("from") or {}
    dst = args.get("to") or {}
    modifiers = list(args.get("modifiers") or [])
    # Resolve element references on either end.
    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    tree = (ctx.observer.get_element_tree(info.handle,
                                          window_uid=info.window_uid)
            if info else None)

    def _to_xy(spec: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        if "x" in spec and "y" in spec:
            return int(spec["x"]), int(spec["y"])
        eid = spec.get("element_id")
        sel_text = spec.get("selector")
        if tree is None:
            return None
        if sel_text:
            try:
                resu = sel.resolve(tree, sel.parse(sel_text))
                if resu.matches:
                    e = resu.matches[0]
                    return e.bounds.center_x, e.bounds.center_y
            except sel.SelectorParseError:
                return None
        if eid:
            e = _find_by_id(tree, eid)
            if e is not None:
                return e.bounds.center_x, e.bounds.center_y
        return None

    p1 = _to_xy(src)
    p2 = _to_xy(dst)
    if p1 is None or p2 is None:
        return error_dict(Code.BAD_REQUEST,
                          "drag requires from/to as {x,y} or {selector|element_id}",
                          step_id=step_id)

    # §21 confirmation gate: drag endpoints addressed by selector/element_id
    # resolve to concrete elements, so a confirmation_required rule matching
    # either endpoint (e.g. a "Trash" drop target) must demand a token, just
    # like the other element-targeted verbs.  The first matching endpoint is
    # validated (propose_action(action="drag", args={"selector": …}) issues
    # the token for that endpoint's selector).
    if tree is not None and info is not None:
        for spec in (src, dst):
            if not (spec.get("selector") or spec.get("element_id")):
                continue
            elem, selector_str, res_err = _resolve_element(tree, spec)
            if res_err or elem is None:
                continue
            tgt = {"window_uid": info.window_uid,
                   "element_id": elem.element_id,
                   "selector": selector_str,
                   "bounds": elem.bounds.to_dict()}
            if _confirmation_rules_match(ctx, tgt):
                confirm_check = _check_confirmation(ctx, "drag", args, tgt)
                if confirm_check is not None:
                    return {**confirm_check, "step_id": step_id}
                break   # token validated for the matching endpoint

    duration = float(args.get("duration_s", 0.5))
    path = [p1, ((p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2), p2]
    try:
        import pyautogui
        for k in modifiers:
            pyautogui.keyDown(k)
        try:
            pyautogui.moveTo(*p1)
            pyautogui.dragTo(*p2, duration=duration, button="left")
        finally:
            for k in modifiers:
                pyautogui.keyUp(k)
        ok = True
        err = None
    except Exception as e:
        ok = False
        err = str(e)
    out = {"ok": ok, "success": ok, "action": "drag",
           "step_id": step_id, "caused_by_step_id": step_id,
           "from": list(p1), "to": list(p2),
           "modifiers": modifiers, "path": [list(p) for p in path]}
    if err:
        out["error"] = {"code": Code.INTERNAL, "message": err,
                        "recoverable": False, "suggested_next_tool": None,
                        "context": {}}
    return out


def key_into_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    keys = args.get("keys", "")
    def _exec(elem: UIElement, info: WindowInfo, _a: Dict[str, Any]
              ) -> Dict[str, Any]:
        ctx.observer.perform_action(
            "click_at",
            element_id=elem.element_id,
            value={"x": elem.bounds.center_x, "y": elem.bounds.center_y,
                   "button": "left", "double": False},
            hwnd=info.handle,
        )
        return ctx.observer.perform_action("key", value=keys, hwnd=info.handle)
    return _do_element_action(ctx, action_name="key_into_element",
                              args=args, executor=_exec)


def clear_text(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    def _exec(elem: UIElement, info: WindowInfo, _a: Dict[str, Any]
              ) -> Dict[str, Any]:
        ctx.observer.perform_action(
            "click_at",
            element_id=elem.element_id,
            value={"x": elem.bounds.center_x, "y": elem.bounds.center_y,
                   "button": "left", "double": False},
            hwnd=info.handle,
        )
        ctx.observer.perform_action("key", value="ctrl+a", hwnd=info.handle)
        return ctx.observer.perform_action("key", value="delete",
                                            hwnd=info.handle)
    return _do_element_action(ctx, action_name="clear_text",
                              args=args, executor=_exec)

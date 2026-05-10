"""
tools.py — Central tool implementations.

Both mcp_server.py and web_inspector.py dispatch into this module; the
MCP and REST surfaces are thin wrappers.  Every tool returns a dict in
one of two shapes:

    {ok: true,  step_id: …, …tool-specific fields…}
    {ok: false, success: false, step_id: …, error: {code, message, …}}

For backwards compatibility (design doc D5) success-shaped legacy fields
are preserved alongside the new `ok` / `error` object on existing tools.
"""

from __future__ import annotations

import base64
import logging
import re
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import element_selectors as sel
from errors import Code, Error, error_dict, annotate_legacy_result
from hashing import focused_selector, tree_hash, windows_hash
from observer import (
    Bounds, ScreenObserver, UIElement, WindowInfo, WindowResolution,
    _intersect_bounds, _subtract_rect,
)
from session import Session, get_session

logger = logging.getLogger(__name__)


# ─── Context ──────────────────────────────────────────────────────────────────

@dataclass
class ToolContext:
    observer:   ScreenObserver
    renderer:   Any
    describer:  Any
    config:     Dict[str, Any]

    @property
    def session(self) -> Session:
        return get_session()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_input_tool(name: str) -> bool:
    return name in {
        "click_at", "type_text", "press_key", "scroll", "bring_to_foreground",
        "click_element", "focus_element", "set_value", "invoke_element",
        "select_option", "hover_at", "hover_element",
        "right_click_at", "right_click_element",
        "double_click_at", "double_click_element",
        "drag", "key_into_element", "clear_text",
        "click_element_and_observe", "type_and_observe", "press_key_and_observe",
    }


def _new_step_id(name: str) -> Tuple[int, Optional[int]]:
    return get_session().steps.next_id(is_input=_is_input_tool(name))


def _resolve_window(ctx: ToolContext, args: Dict[str, Any]
                    ) -> Tuple[List[WindowInfo], WindowResolution]:
    windows = ctx.observer.list_windows()
    res = ctx.observer.resolve_window(
        windows,
        window_uid=args.get("window_uid"),
        window_index=args.get("window_index"),
    )
    return windows, res


def _focused_window(windows: List[WindowInfo]) -> Optional[WindowInfo]:
    for w in windows:
        if w.is_focused:
            return w
    return windows[0] if windows else None


def _resolve_element(tree: UIElement, args: Dict[str, Any]
                     ) -> Tuple[Optional[UIElement], Optional[str], Optional[Dict]]:
    """
    Returns (element, selector_string, error_dict).  Either *element* is set
    or *error_dict* is.  The selector string is resolved or derived.
    """
    selector = args.get("selector")
    element_id = args.get("element_id")

    if selector:
        try:
            parsed = sel.parse(selector)
        except sel.SelectorParseError as e:
            return None, None, error_dict(Code.BAD_REQUEST,
                                          f"selector parse error: {e}",
                                          selector=selector)
        result = sel.resolve(tree, parsed)
        if not result.matches:
            return None, None, error_dict(
                Code.ELEMENT_NOT_FOUND,
                f"no element matches selector {selector!r}",
                selector=selector,
            )
        return result.matches[0], parsed.canonical(), None

    if element_id:
        elem = _find_by_id(tree, element_id)
        if elem is None:
            return None, None, error_dict(
                Code.ELEMENT_NOT_FOUND,
                f"no element with id {element_id!r}",
                element_id=element_id,
            )
        derived = sel.selector_for(tree, element_id) or ""
        return elem, derived, None

    return None, None, error_dict(Code.BAD_REQUEST,
                                  "either element_id or selector is required")


def _find_by_id(elem: UIElement, target: str) -> Optional[UIElement]:
    if elem.element_id == target:
        return elem
    for c in elem.children:
        r = _find_by_id(c, target)
        if r is not None:
            return r
    return None


def _new_dialogs(before: List[WindowInfo], after: List[WindowInfo]) -> List[Dict]:
    before_uids = {w.window_uid for w in before}
    return [
        {"window_uid": w.window_uid, "title": w.title}
        for w in after if w.window_uid and w.window_uid not in before_uids
    ]


# ─── Read-only tools ──────────────────────────────────────────────────────────

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


def find_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("find_element")
    selector_text = args.get("selector")
    if not selector_text:
        return error_dict(Code.BAD_REQUEST, "selector is required",
                          step_id=step_id)
    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)
    tree = ctx.observer.get_element_tree(info.handle)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id, window_uid=info.window_uid)
    try:
        parsed = sel.parse(selector_text)
    except sel.SelectorParseError as e:
        return error_dict(Code.BAD_REQUEST, f"selector parse error: {e}",
                          step_id=step_id)
    result = sel.resolve(tree, parsed)
    if not result.matches:
        return error_dict(Code.ELEMENT_NOT_FOUND,
                          f"no element matches {selector_text!r}",
                          step_id=step_id, selector=selector_text,
                          window_uid=info.window_uid)
    first = result.matches[0]
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window_uid": info.window_uid,
        "element_id": first.element_id,
        "selector": parsed.canonical(),
        "bounds": first.bounds.to_dict(),
        "ambiguous_matches": len(result.matches),
        "all_matches": [
            {"element_id": m.element_id, "bounds": m.bounds.to_dict(),
             "name": m.name, "role": m.role}
            for m in result.matches
        ],
    }


# ─── Receipts and action wrappers ────────────────────────────────────────────

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
    tree = ctx.observer.get_element_tree(info.handle)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id, window_uid=info.window_uid)

    elem, selector_str, err = _resolve_element(tree, args)
    if err:
        return {**err, "step_id": step_id}

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
    after_tree = (ctx.observer.get_element_tree(info_after.handle)
                  if info_after else None)

    ok = bool(executor_result.get("success", True))
    err_obj = None
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


def _check_confirmation(ctx: ToolContext, action_name: str,
                        args: Dict[str, Any],
                        target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Returns an error dict to short-circuit when confirmation is required."""
    confirm = ctx.config.get("confirmation_required") or []
    if not confirm:
        return None
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

    if not any(_matches_rule(r) for r in confirm):
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


# ─── Element-targeted actions ─────────────────────────────────────────────────

def click_element(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    button = args.get("button", "left")
    count = int(args.get("count", 1))

    def _exec(elem: UIElement, info: WindowInfo, _args: Dict[str, Any]
              ) -> Dict[str, Any]:
        cx, cy = elem.bounds.center_x, elem.bounds.center_y
        result = ctx.observer.perform_action(
            "click_at",
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
        # Re-walk to see the now-visible option list.
        new_tree = ctx.observer.get_element_tree(info.handle)
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


# ─── Legacy actions (now also returning ActionReceipts) ──────────────────────

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


def get_window_structure(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_window_structure")
    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)
    tree = ctx.observer.get_element_tree(info.handle)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id, window_uid=info.window_uid)
    serialized = tree.to_dict()
    th = tree_hash(tree)
    token = get_session().tree_tokens.put(info.window_uid, serialized, th)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window": info.title,
        "window_uid": info.window_uid,
        "element_count": len(tree.flat_list()),
        "tree": serialized,
        "tree_hash": th,
        "tree_token": token,
    }


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


def bring_to_foreground(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, _ = _new_step_id("bring_to_foreground")
    windows, res = _resolve_window(ctx, args)
    info = res.info
    if info is None:
        return error_dict(Code.BAD_REQUEST,
                          "window_uid or window_index is required",
                          step_id=step_id)
    result = ctx.observer.bring_to_foreground(info.handle, windows)
    result["window"] = info.title
    return annotate_legacy_result(result, step_id=step_id, caused_by_step_id=step_id)


def get_visible_areas(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_visible_areas")
    windows, res = _resolve_window(ctx, args)
    info = res.info
    if info is None:
        return error_dict(Code.BAD_REQUEST,
                          "window_uid or window_index is required",
                          step_id=step_id)
    areas = ctx.observer.get_visible_areas(info.handle, windows)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window": info.title,
        "window_uid": info.window_uid,
        "visible_regions": areas,
    }


# ─── Dispatcher ───────────────────────────────────────────────────────────────

REGISTRY: Dict[str, Callable[[ToolContext, Dict[str, Any]], Dict[str, Any]]] = {
    # Read-only
    "list_windows":         list_windows,
    "get_capabilities":     get_capabilities,
    "get_monitors":         get_monitors,
    "find_element":         find_element,
    "get_window_structure": get_window_structure,
    "get_screenshot":       get_screenshot,
    "get_visible_areas":    get_visible_areas,

    # Element-targeted actions
    "click_element":   click_element,
    "focus_element":   focus_element,
    "set_value":       set_value,
    "invoke_element":  invoke_element,
    "select_option":   select_option,

    # Legacy actions
    "click_at":              click_at,
    "type_text":             type_text,
    "press_key":             press_key,
    "scroll":                scroll,
    "bring_to_foreground":   bring_to_foreground,
}


def dispatch(ctx: ToolContext, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    fn = REGISTRY.get(name)
    if fn is None:
        return error_dict(Code.BAD_REQUEST, f"unknown tool: {name}")
    try:
        return fn(ctx, args or {})
    except Exception as e:
        logger.exception(f"tool {name} crashed")
        return error_dict(Code.INTERNAL, f"{type(e).__name__}: {e}")

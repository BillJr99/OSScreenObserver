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
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import element_selectors as sel
from errors import Code, error_dict, annotate_legacy_result
from hashing import focused_selector, tree_hash
from observer import (
    ScreenObserver, UIElement, WindowInfo, WindowResolution,
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
        window_title=args.get("window_title"),
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

    # Try selector first, then element_id.  When both are provided, element_id
    # acts as a fallback so a bad/unmatched selector doesn't block the call.
    selector_err = None
    if selector:
        try:
            parsed = sel.parse(selector)
            result = sel.resolve(tree, parsed)
            if result.matches:
                return result.matches[0], parsed.canonical(), None
            selector_err = error_dict(
                Code.ELEMENT_NOT_FOUND,
                f"no element matches selector {selector!r}",
                selector=selector,
            )
        except sel.SelectorParseError as e:
            selector_err = error_dict(Code.BAD_REQUEST,
                                      f"selector parse error: {e}",
                                      selector=selector)

    if element_id:
        elem = _find_by_id(tree, element_id)
        if elem is not None:
            derived = sel.selector_for(tree, element_id) or ""
            return elem, derived, None
        # element_id didn't match any internal ID — try parsing it as a selector
        # (LLMs often pass the display-format string e.g. 'TabItem "name"' as an id).
        try:
            parsed = sel.parse(element_id)
            result = sel.resolve(tree, parsed)
            if result.matches:
                return result.matches[0], parsed.canonical(), None
        except (sel.SelectorParseError, Exception):
            pass
        # Both failed — prefer selector error if we have one (more informative).
        if selector_err:
            return None, None, selector_err
        return None, None, error_dict(
            Code.ELEMENT_NOT_FOUND,
            f"no element with id {element_id!r}",
            element_id=element_id,
        )

    if selector_err:
        return None, None, selector_err

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

    # P3 filtering / paging --------------------------------------------------
    roles = args.get("roles")
    exclude_roles = args.get("exclude_roles")
    visible_only = bool(args.get("visible_only"))
    name_regex = args.get("name_regex")
    max_text_len = args.get("max_text_len")
    prune_empty = bool(args.get("prune_empty"))
    max_nodes = args.get("max_nodes")
    page_cursor = args.get("page_cursor")

    visible_regions: Optional[List[Dict[str, int]]] = None
    if visible_only:
        try:
            visible_regions = ctx.observer.get_visible_areas(info.handle, windows)
        except Exception:
            visible_regions = []

    filtered = _filter_tree(
        serialized,
        roles=set(roles) if roles else None,
        exclude_roles=set(exclude_roles) if exclude_roles else None,
        visible_regions=visible_regions,
        name_regex=name_regex,
        max_text_len=max_text_len,
        prune_empty=prune_empty,
    )

    truncated = False
    next_cursor: Optional[str] = None
    node_count = _count_nodes(filtered) if filtered else 0
    if max_nodes is not None or page_cursor is not None:
        filtered, truncated, next_cursor, node_count = _page_tree(
            filtered, max_nodes=max_nodes, page_cursor=page_cursor,
        )

    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window": info.title,
        "window_uid": info.window_uid,
        "element_count": len(tree.flat_list()),
        "node_count": node_count,
        "tree": filtered,
        "tree_hash": th,
        "tree_token": token,
        "truncated": truncated,
        "next_cursor": next_cursor,
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
    result["window"]     = info.title
    result["window_uid"] = info.window_uid
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


# ─── P2: observe-with-diff, snapshots, wait_for, composites ──────────────────

def _serialize_full_observation(ctx: ToolContext, info: WindowInfo,
                                 ) -> Tuple[Optional[UIElement], Dict[str, Any]]:
    tree = ctx.observer.get_element_tree(info.handle)
    if tree is None:
        return None, {"error": "no tree"}
    serialized = tree.to_dict()
    th = tree_hash(tree)
    token = get_session().tree_tokens.put(info.window_uid, serialized, th)
    return tree, {
        "format": "full",
        "window_uid": info.window_uid,
        "window": info.title,
        "tree": serialized,
        "tree_hash": th,
        "tree_token": token,
        "base_token": None,
    }


def observe_window(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Return the current tree, optionally as a diff against a tree_token."""
    from diff import diff_custom, diff_json_patch
    step_id, caused_by = _new_step_id("observe_window")
    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)
    since = args.get("since")
    fmt = args.get("format", "custom")

    if not since:
        _, full = _serialize_full_observation(ctx, info)
        full.update({"ok": True, "success": True,
                     "step_id": step_id, "caused_by_step_id": caused_by,
                     "format": "full"})
        return full

    entry = get_session().tree_tokens.get(since)
    tree = ctx.observer.get_element_tree(info.handle)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id, window_uid=info.window_uid)
    serialized = tree.to_dict()
    th = tree_hash(tree)
    new_token = get_session().tree_tokens.put(info.window_uid, serialized, th)

    if entry is None or entry.window_uid != info.window_uid:
        # Token expired/wrong-window: return full tree.
        return {
            "ok": True, "success": True,
            "step_id": step_id, "caused_by_step_id": caused_by,
            "window_uid": info.window_uid, "window": info.title,
            "tree": serialized, "tree_hash": th,
            "tree_token": new_token, "base_token": None,
            "format": "full",
        }

    if fmt == "json-patch":
        changes = diff_json_patch(entry.serialized, serialized)
        out_format = "json-patch"
    else:
        changes = diff_custom(entry.serialized, serialized)
        out_format = "custom"

    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window_uid": info.window_uid, "window": info.title,
        "tree_token": new_token, "base_token": since,
        "format": out_format,
        "changes": changes,
        "unchanged": len(changes) == 0,
        "tree_hash": th,
    }


def snapshot(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("snapshot")
    windows = ctx.observer.list_windows()
    trees: Dict[str, Dict[str, Any]] = {}
    hashes: Dict[str, str] = {}
    for w in windows:
        try:
            t = ctx.observer.get_element_tree(w.handle)
            if t is not None and w.window_uid:
                trees[w.window_uid] = t.to_dict()
                hashes[w.window_uid] = tree_hash(t)
        except Exception:
            continue
    snap = get_session().snapshots.put(
        windows=[w.to_dict() for w in windows],
        trees=trees, tree_hashes=hashes,
    )
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "snapshot_id": snap.snapshot_id,
        "ts": snap.ts,
        "summary": {"windows": len(snap.windows), "trees": len(trees)},
    }


def snapshot_get(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("snapshot_get")
    sid = args.get("snapshot_id")
    if not sid:
        return error_dict(Code.BAD_REQUEST, "snapshot_id is required",
                          step_id=step_id)
    snap = get_session().snapshots.get(sid)
    if snap is None:
        return error_dict(Code.SNAPSHOT_EXPIRED,
                          "snapshot expired or not found",
                          step_id=step_id, snapshot_id=sid)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "snapshot_id": snap.snapshot_id, "ts": snap.ts,
        "windows": snap.windows,
        "trees": snap.trees,
        "tree_hashes": snap.tree_hashes,
    }


def snapshot_diff(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from diff import diff_custom, diff_json_patch
    step_id, caused_by = _new_step_id("snapshot_diff")
    a_id = args.get("a")
    b_id = args.get("b")
    if not a_id or not b_id:
        return error_dict(Code.BAD_REQUEST, "a and b are required",
                          step_id=step_id)
    sess = get_session()
    a = sess.snapshots.get(a_id)
    b = sess.snapshots.get(b_id)
    if a is None or b is None:
        return error_dict(Code.SNAPSHOT_EXPIRED,
                          "one or both snapshots are missing",
                          step_id=step_id)
    fmt = args.get("format", "custom")

    a_uids = {w["window_uid"] for w in a.windows}
    b_uids = {w["window_uid"] for w in b.windows}
    windows_added = sorted(b_uids - a_uids)
    windows_removed = sorted(a_uids - b_uids)
    common = sorted(a_uids & b_uids)

    per_window: Dict[str, Any] = {}
    for uid in common:
        if uid in a.trees and uid in b.trees:
            if fmt == "json-patch":
                per_window[uid] = {"format": "json-patch",
                                   "changes": diff_json_patch(a.trees[uid], b.trees[uid])}
            else:
                per_window[uid] = {"format": "custom",
                                   "changes": diff_custom(a.trees[uid], b.trees[uid])}
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "windows_added": windows_added,
        "windows_removed": windows_removed,
        "per_window_changes": per_window,
    }


def snapshot_drop(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("snapshot_drop")
    sid = args.get("snapshot_id")
    dropped = get_session().snapshots.drop(sid) if sid else False
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "dropped": dropped,
    }


# ─── wait_for / wait_idle ─────────────────────────────────────────────────────

def _check_condition(ctx: ToolContext, cond: Dict[str, Any],
                     window_uid_hint: Optional[str]) -> Tuple[bool, Dict[str, Any]]:
    kind = cond.get("type")
    sess = get_session()
    windows = ctx.observer.list_windows()
    info = ctx.observer.window_by_uid(windows, window_uid_hint) or _focused_window(windows)

    if kind == "window_appears":
        rx = cond.get("title_regex", "")
        for w in windows:
            if re.search(rx, w.title):
                return True, {"window_uid": w.window_uid, "title": w.title}
        return False, {}
    if kind == "window_disappears":
        target = cond.get("window_uid")
        for w in windows:
            if w.window_uid == target:
                return False, {}
        return True, {"window_uid": target}
    if kind == "focused_changes":
        focus = next((w for w in windows if w.is_focused), None)
        return (focus is not None), ({"focused_uid": focus.window_uid} if focus else {})
    if kind == "tree_changes":
        token = cond.get("since")
        entry = sess.tree_tokens.get(token) if token else None
        if entry is None or info is None:
            return False, {}
        tree = ctx.observer.get_element_tree(info.handle)
        return tree is not None and tree_hash(tree) != entry.tree_hash, {}

    if info is None:
        return False, {}
    tree = ctx.observer.get_element_tree(info.handle)
    if tree is None:
        return False, {}

    if kind == "element_appears":
        sel_text = cond.get("selector")
        if not sel_text:
            return False, {}
        try:
            res = sel.resolve(tree, sel.parse(sel_text))
        except sel.SelectorParseError:
            return False, {}
        if res.matches:
            m = res.matches[0]
            return True, {"element_id": m.element_id, "bounds": m.bounds.to_dict()}
        return False, {}
    if kind == "element_disappears":
        sel_text = cond.get("selector")
        eid = cond.get("element_id")
        if sel_text:
            try:
                res = sel.resolve(tree, sel.parse(sel_text))
                return not res.matches, {}
            except sel.SelectorParseError:
                return False, {}
        if eid:
            return _find_by_id(tree, eid) is None, {}
        return False, {}
    if kind == "text_visible":
        rx = cond.get("regex", "")
        # Walk tree names/values.
        for elem in tree.flat_list():
            joined = (elem.name or "") + " " + (elem.value or "")
            if re.search(rx, joined):
                return True, {"element_id": elem.element_id}
        return False, {}
    return False, {}


def wait_for(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("wait_for")
    timeout_ms = int(args.get("timeout_ms", 5000))
    cap = int((ctx.config.get("wait_for", {}) or {}).get("max_timeout_ms", 60000))
    timeout_ms = min(timeout_ms, cap)
    poll_ms = max(50, int(args.get("poll_ms", 200)))
    conditions = args.get("any_of", [])
    if not conditions:
        return error_dict(Code.BAD_REQUEST, "any_of is required",
                          step_id=step_id)
    window_uid = args.get("window_uid")

    started = time.time()
    polls = 0
    while True:
        polls += 1
        for i, cond in enumerate(conditions):
            try:
                ok, detail = _check_condition(ctx, cond, window_uid)
            except Exception:
                ok, detail = False, {}
            if ok:
                return {
                    "ok": True, "success": True,
                    "step_id": step_id, "caused_by_step_id": caused_by,
                    "matched_index": i, "matched_detail": detail,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "polls": polls,
                }
        elapsed = (time.time() - started) * 1000
        if elapsed >= timeout_ms:
            err = error_dict(
                Code.TIMEOUT, f"wait_for timed out after {int(elapsed)}ms",
                step_id=step_id,
            )
            err.update({
                "elapsed_ms": int(elapsed), "polls": polls,
                "matched_index": None,
            })
            return err
        time.sleep(poll_ms / 1000.0)


def wait_idle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("wait_idle")
    timeout_ms = int(args.get("timeout_ms", 5000))
    quiet_ms = int(args.get("quiet_ms", 750))
    poll_ms = max(50, int(args.get("poll_ms", 100)))
    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)

    started = time.time()
    last_hash = None
    last_change_at = time.time()
    while (time.time() - started) * 1000 < timeout_ms:
        tree = ctx.observer.get_element_tree(info.handle)
        if tree is None:
            time.sleep(poll_ms / 1000.0)
            continue
        h = tree_hash(tree)
        if h != last_hash:
            last_hash = h
            last_change_at = time.time()
        elif (time.time() - last_change_at) * 1000 >= quiet_ms:
            return {
                "ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "elapsed_ms": int((time.time() - started) * 1000),
                "tree_hash": h,
            }
        time.sleep(poll_ms / 1000.0)

    err = error_dict(Code.TIMEOUT, "wait_idle timed out", step_id=step_id)
    err["elapsed_ms"] = int((time.time() - started) * 1000)
    return err


# ─── Composite action+observe ─────────────────────────────────────────────────

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


# ─── P3: tree filtering / paging helpers ─────────────────────────────────────

def _filter_tree(node: Dict[str, Any], *, roles: Optional[set],
                 exclude_roles: Optional[set],
                 visible_regions: Optional[List[Dict[str, int]]],
                 name_regex: Optional[str],
                 max_text_len: Optional[int],
                 prune_empty: bool) -> Optional[Dict[str, Any]]:
    if node is None:
        return None
    role = node.get("role")
    name = node.get("name") or ""
    bounds = node.get("bounds") or {}

    # Role filter
    role_keep = True
    if roles is not None and role not in roles:
        role_keep = False
    if exclude_roles is not None and role in exclude_roles:
        role_keep = False

    # Name regex
    name_keep = True
    if name_regex:
        try:
            name_keep = bool(re.search(name_regex, name))
        except re.error:
            name_keep = True

    # Visibility
    visible_keep = True
    if visible_regions is not None:
        visible_keep = _intersects_any(bounds, visible_regions)

    self_keep = role_keep and name_keep and visible_keep

    # Recurse children regardless (so we can keep ancestors if descendants match)
    new_children: List[Dict[str, Any]] = []
    for c in node.get("children", []) or []:
        fc = _filter_tree(
            c, roles=roles, exclude_roles=exclude_roles,
            visible_regions=visible_regions, name_regex=name_regex,
            max_text_len=max_text_len, prune_empty=prune_empty,
        )
        if fc is not None:
            new_children.append(fc)

    if prune_empty and not self_keep and not new_children:
        return None

    # Truncate text fields if requested.
    truncated_node = dict(node)
    if max_text_len is not None:
        n = int(max_text_len)
        if isinstance(truncated_node.get("name"), str) and len(truncated_node["name"]) > n:
            truncated_node["name"] = truncated_node["name"][:n] + "…"
        v = truncated_node.get("value")
        if isinstance(v, str) and len(v) > n:
            truncated_node["value"] = v[:n] + "…"
    truncated_node["children"] = new_children
    return truncated_node


def _intersects_any(b: Dict[str, int], regions: List[Dict[str, int]]) -> bool:
    if not b:
        return False
    bx, by = b.get("x", 0), b.get("y", 0)
    bw, bh = b.get("width", 0), b.get("height", 0)
    if bw <= 0 or bh <= 0:
        return False
    bx2, by2 = bx + bw, by + bh
    for r in regions:
        rx, ry = r.get("x", 0), r.get("y", 0)
        rx2, ry2 = rx + r.get("width", 0), ry + r.get("height", 0)
        if bx < rx2 and bx2 > rx and by < ry2 and by2 > ry:
            return True
    return False


def _count_nodes(node: Optional[Dict[str, Any]]) -> int:
    if node is None:
        return 0
    return 1 + sum(_count_nodes(c) for c in (node.get("children") or []))


def _page_tree(node: Optional[Dict[str, Any]], *,
               max_nodes: Optional[int],
               page_cursor: Optional[str]
               ) -> Tuple[Optional[Dict[str, Any]], bool, Optional[str], int]:
    """
    Paginated DFS walk.  Returns (subtree-shaped result containing only the
    page slice, truncated flag, next_cursor, node_count_in_page).

    Cursors are post-order element_ids; resuming starts from the next sibling
    in the original walk.  This is a best-effort pager — if the tree changed,
    callers will get SnapshotExpired-shaped semantics by virtue of an unknown
    cursor returning truncated:false and node_count:0.
    """
    if node is None:
        return None, False, None, 0
    flat: List[Dict[str, Any]] = []
    _flatten(node, flat)

    if page_cursor is not None:
        for i, n in enumerate(flat):
            if n.get("id") == page_cursor:
                flat = flat[i + 1:]
                break
        else:
            return None, False, None, 0

    if max_nodes is None or max_nodes >= len(flat):
        # Return full (possibly trimmed) tree starting from cursor.
        if page_cursor is None:
            return node, False, None, len(flat)
        return _flat_to_tree(flat), False, None, len(flat)

    page = flat[:max_nodes]
    truncated = True
    next_cursor = page[-1].get("id") if page else None
    return _flat_to_tree(page), truncated, next_cursor, len(page)


def _flatten(node: Dict[str, Any], out: List[Dict[str, Any]]) -> None:
    out.append(node)
    for c in node.get("children") or []:
        _flatten(c, out)


def _flat_to_tree(flat: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Wrap a list of nodes as children of a synthetic Window root."""
    if not flat:
        return None
    return {
        "id": "page-root",
        "name": "[paged]",
        "role": "Group",
        "value": None,
        "bounds": {"x": 0, "y": 0, "width": 0, "height": 0},
        "enabled": True, "focused": False,
        "keyboard_shortcut": None, "description": None,
        "children": [dict(n, children=[]) for n in flat],
    }


# ─── P3: cropped screenshots, region OCR, budgeted description ────────────────

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
        tree = ctx.observer.get_element_tree(info.handle)
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
    img = Image.open(_io.BytesIO(png_bytes))
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
        tree = ctx.observer.get_element_tree(info.handle)
        if tree is not None:
            elem = _find_by_id(tree, element_id)
            if elem is not None:
                bbox = {
                    "x": max(0, elem.bounds.x - info.bounds.x),
                    "y": max(0, elem.bounds.y - info.bounds.y),
                    "width":  elem.bounds.width,
                    "height": elem.bounds.height,
                }

    img = Image.open(_io.BytesIO(shot))
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
    tree = ctx.observer.get_element_tree(info.handle)
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

    # P2: sync, diff, snapshots, composites
    "observe_window":   observe_window,
    "snapshot":         snapshot,
    "snapshot_get":     snapshot_get,
    "snapshot_diff":    snapshot_diff,
    "snapshot_drop":    snapshot_drop,
    "wait_for":         wait_for,
    "wait_idle":        wait_idle,
    "click_element_and_observe":  click_element_and_observe,
    "type_and_observe":           type_and_observe,
    "press_key_and_observe":      press_key_and_observe,

    # P3
    "get_screenshot_cropped":  get_screenshot_cropped,
    "get_ocr":                 get_ocr,
    "get_screen_description":  get_screen_description,
}

# Forward-declared P4 entries appended after definitions (below).


# ─── P4: tracing, replay, scenarios, oracles ─────────────────────────────────

def trace_start(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import tracing as _tracing
    step_id, caused_by = _new_step_id("trace_start")
    sess = get_session()
    if sess.active_trace is not None and not sess.active_trace.closed:
        return error_dict(Code.BAD_REQUEST, "trace already active",
                          step_id=step_id,
                          trace_id=sess.active_trace.trace_id)
    handle = _tracing.start(label=args.get("label", ""), config=ctx.config)
    sess.active_trace = handle
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "trace_id": handle.trace_id,
        "started_at": handle.started_at,
        "dir": handle.dir,
    }


def trace_stop(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import tracing as _tracing
    step_id, caused_by = _new_step_id("trace_stop")
    sess = get_session()
    if sess.active_trace is None:
        return error_dict(Code.BAD_REQUEST, "no active trace",
                          step_id=step_id)
    info = _tracing.stop(sess.active_trace)
    sess.active_trace = None
    info.update({"ok": True, "success": True,
                 "step_id": step_id, "caused_by_step_id": caused_by})
    return info


def trace_status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("trace_status")
    sess = get_session()
    if sess.active_trace is None:
        return {"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "active_trace_id": None, "step_count": 0, "dir": None}
    h = sess.active_trace
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "active_trace_id": h.trace_id,
        "step_count": h.counter.value,
        "dir": h.dir,
    }


# ─── Replay state ────────────────────────────────────────────────────────────

_REPLAYS: Dict[str, Any] = {}


def replay_start(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import replay as _replay
    step_id, caused_by = _new_step_id("replay_start")
    path = args.get("path")
    if not path:
        return error_dict(Code.BAD_REQUEST, "path is required",
                          step_id=step_id)
    mode = args.get("mode", "execute")
    on_div = args.get("on_divergence", "warn")
    try:
        rep = _replay.load(path, mode=mode, on_divergence=on_div)
    except Exception as e:
        return error_dict(Code.BAD_REQUEST, f"could not load trace: {e}",
                          step_id=step_id, path=path)
    handle_id = "rep:" + str(len(_REPLAYS) + 1)
    _REPLAYS[handle_id] = rep
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "replay_id": handle_id,
        "total": len(rep.rows),
        "mode": rep.mode,
        "label": rep.label,
    }


def replay_step(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import replay as _replay
    step_id, caused_by = _new_step_id("replay_step")
    rid = args.get("replay_id")
    rep = _REPLAYS.get(rid)
    if rep is None:
        return error_dict(Code.BAD_REQUEST, "unknown replay_id",
                          step_id=step_id, replay_id=rid)

    def _disp(name: str, a: Dict[str, Any]) -> Dict[str, Any]:
        return dispatch(ctx, name, a)

    out = _replay.step(rep, dispatch=_disp)
    out.update({"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "replay_id": rid})
    return out


def replay_status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("replay_status")
    rid = args.get("replay_id")
    rep = _REPLAYS.get(rid)
    if rep is None:
        return error_dict(Code.BAD_REQUEST, "unknown replay_id",
                          step_id=step_id, replay_id=rid)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "replay_id": rid,
        "position": rep.position,
        "total": len(rep.rows),
        "finished": rep.finished,
        "divergences": rep.divergences,
        "mode": rep.mode,
    }


def replay_stop(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("replay_stop")
    rid = args.get("replay_id")
    if rid in _REPLAYS:
        _REPLAYS.pop(rid)
        return {"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "stopped": True}
    return error_dict(Code.BAD_REQUEST, "unknown replay_id",
                      step_id=step_id, replay_id=rid)


# ─── Scenarios ───────────────────────────────────────────────────────────────

def load_scenario(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import scenarios as _scn
    step_id, caused_by = _new_step_id("load_scenario")
    path = args.get("path")
    if not path:
        return error_dict(Code.BAD_REQUEST, "path is required",
                          step_id=step_id)
    try:
        sc = _scn.load(path)
        _scn.attach_to_observer(sc, ctx.observer)
    except _scn.ScenarioError as e:
        return error_dict(Code.SCENARIO_INVALID, str(e),
                          step_id=step_id, path=path)
    except Exception as e:
        return error_dict(Code.SCENARIO_INVALID, f"{type(e).__name__}: {e}",
                          step_id=step_id, path=path)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "scenario": sc.name,
        "state": sc.current_state,
        "states": list(sc.states.keys()),
    }


# ─── Oracles ─────────────────────────────────────────────────────────────────

def assert_state(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import oracles as _oracles
    step_id, caused_by = _new_step_id("assert_state")
    pred = args.get("predicate") or args.get("predicates") or []
    out = _oracles.evaluate(ctx.observer, pred, config=ctx.config)
    if out.get("ok"):
        out["step_id"] = step_id
        out["caused_by_step_id"] = caused_by
    return out


REGISTRY.update({
    "trace_start":     trace_start,
    "trace_stop":      trace_stop,
    "trace_status":    trace_status,
    "replay_start":    replay_start,
    "replay_step":     replay_step,
    "replay_status":   replay_status,
    "replay_stop":     replay_stop,
    "load_scenario":   load_scenario,
    "assert_state":    assert_state,
})


# ─── P5: budgets, propose_action, status reporters ───────────────────────────

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
    tree = ctx.observer.get_element_tree(info.handle)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id)
    elem, selector_str, err = _resolve_element(tree, inner_args)
    if err:
        return {**err, "step_id": step_id}

    sess = get_session()
    bbox = elem.bounds.to_dict()
    ct = sess.confirms.issue(
        action=action, window_uid=info.window_uid,
        selector=selector_str, bbox=bbox, args=inner_args,
    )
    # Optional preview crop.
    try:
        shot = ctx.observer.get_screenshot(info.handle)
        crop_bytes, _ = _apply_crop(shot, bbox=bbox, padding=8, max_width=400)
        preview_b64 = base64.b64encode(crop_bytes).decode() if crop_bytes else None
    except Exception:
        preview_b64 = None

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


REGISTRY.update({
    "get_budget_status":    get_budget_status,
    "get_redaction_status": get_redaction_status,
    "propose_action":       propose_action,
})


# ─── P6: extra input verbs ────────────────────────────────────────────────────

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
    tree = ctx.observer.get_element_tree(info.handle) if info else None

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


REGISTRY.update({
    "hover_at":              hover_at,
    "hover_element":         hover_element,
    "right_click_at":        right_click_at,
    "right_click_element":   right_click_element,
    "double_click_at":       double_click_at,
    "double_click_element":  double_click_element,
    "drag":                  drag,
    "key_into_element":      key_into_element,
    "clear_text":            clear_text,
})


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
                t = ctx.observer.get_element_tree(focused0.handle)
                if t:
                    tree_before = tree_hash(t)
        except Exception:
            pass

    try:
        result = fn(ctx, args or {})
    except Exception as e:
        logger.exception(f"tool {name} crashed")
        result = error_dict(Code.INTERNAL, f"{type(e).__name__}: {e}")

    duration_ms = int((time.time() - started) * 1000)

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
                        t = ctx.observer.get_element_tree(f.handle)
                        if t:
                            tree_after = tree_hash(t)
                except Exception:
                    pass
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

    # Budget accounting.
    if sess.budgets is not None:
        try:
            sess.budgets.note(name, result)
        except Exception:
            pass

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

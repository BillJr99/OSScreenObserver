"""
Tool execution context and window/element resolution helpers.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import element_selectors as sel
from errors import Code, error_dict
from observer import ScreenObserver, UIElement, WindowInfo, WindowResolution
from session import Session, get_session

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    observer:   ScreenObserver
    renderer:   Any
    describer:  Any
    config:     Dict[str, Any]

    @property
    def session(self) -> Session:
        return get_session()


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
        except Exception as e:
            logger.debug(f"element_id {element_id!r} not parseable as a "
                         f"selector either: {e}")
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

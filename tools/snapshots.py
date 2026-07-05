"""
Snapshots and wait_for / wait_idle condition polling.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Tuple

import element_selectors as sel
from errors import Code, error_dict
from hashing import tree_hash
from session import get_session

from tools.context import (
    ToolContext, _find_by_id, _focused_window, _new_step_id, _resolve_window,
)


def snapshot(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("snapshot")
    windows = ctx.observer.list_windows()
    trees: Dict[str, Dict[str, Any]] = {}
    hashes: Dict[str, str] = {}
    for w in windows:
        try:
            t = ctx.observer.get_element_tree(w.handle,
                                              window_uid=w.window_uid)
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
        tree = ctx.observer.get_element_tree(info.handle,
                                             window_uid=info.window_uid,
                                             use_cache=False)
        return tree is not None and tree_hash(tree) != entry.tree_hash, {}

    if info is None:
        return False, {}
    # Polling must observe fresh state — bypass the tree cache.
    tree = ctx.observer.get_element_tree(info.handle,
                                         window_uid=info.window_uid,
                                         use_cache=False)
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
        tree = ctx.observer.get_element_tree(info.handle,
                                             window_uid=info.window_uid,
                                             use_cache=False)
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

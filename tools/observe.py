"""
Tree observation: structure, diffs, filtering/paging helpers.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

import element_selectors as sel
from errors import Code, error_dict
from hashing import tree_hash
from observer import UIElement, WindowInfo
from session import get_session

from tools.context import (
    ToolContext, _focused_window, _new_step_id, _resolve_window,
)


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
    tree = ctx.observer.get_element_tree(info.handle,
                                         window_uid=info.window_uid)
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


def _effective_depth(ctx: ToolContext, args: Dict[str, Any]) -> int:
    """Depth to return: caller's depth= (clamped to tree.max_depth) or
    tree.default_depth when the caller passes none."""
    tree_cfg = ctx.config.get("tree", {}) or {}
    hard_cap = int(tree_cfg.get("max_depth", 8))
    requested = args.get("depth")
    if requested is None:
        depth = int(tree_cfg.get("default_depth", 5))
    else:
        try:
            depth = int(requested)
        except (TypeError, ValueError):
            depth = int(tree_cfg.get("default_depth", 5))
    return max(0, min(depth, hard_cap))


def _truncate_depth(node: Optional[Dict[str, Any]], max_depth: int,
                    _depth: int = 0) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Copy *node* limited to *max_depth* levels below it.

    Nodes whose children were dropped are marked ``truncated: true`` with a
    ``child_count`` so the caller knows to drill in (via scope=/depth=).
    Returns (tree, any_node_truncated).  The input dict is not mutated."""
    if node is None:
        return None, False
    out = dict(node)
    children = node.get("children") or []
    if _depth >= max_depth and children:
        out["children"] = []
        out["truncated"] = True
        out["child_count"] = len(children)
        return out, True
    truncated_any = False
    new_children: List[Dict[str, Any]] = []
    for c in children:
        nc, t = _truncate_depth(c, max_depth, _depth + 1)
        if nc is not None:
            new_children.append(nc)
        truncated_any = truncated_any or t
    out["children"] = new_children
    return out, truncated_any


def get_window_structure(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("get_window_structure")
    windows, res = _resolve_window(ctx, args)
    info = res.info or _focused_window(windows)
    if info is None:
        return error_dict(Code.WINDOW_GONE, "no windows available",
                          step_id=step_id)

    depth = _effective_depth(ctx, args)
    scope = args.get("scope")
    token = None

    if scope:
        # Drill into one branch only (element-id path, e.g. 'root.3.2').
        ttl = float((ctx.config.get("tree", {}) or {}).get("cache_ttl_s", 2.0))
        had_fresh_entry = (get_session().tree_cache.get(
            info.window_uid, ttl_s=ttl) is not None)
        started = time.time()
        tree = ctx.observer.get_element_subtree(
            info.handle, scope, max_depth=depth,
            window_uid=info.window_uid)
        capture_ms = int((time.time() - started) * 1000)
        if tree is None:
            return error_dict(Code.ELEMENT_NOT_FOUND,
                              f"no element with id {scope!r} to scope to",
                              step_id=step_id, scope=scope,
                              window_uid=info.window_uid)
        perf = {"capture_ms": capture_ms,
                "node_count": len(tree.flat_list()),
                "cache": "hit" if had_fresh_entry else "miss",
                "depth_used": depth}
        # scoped captures are not valid diff baselines → no tree_token
    else:
        tree, meta = ctx.observer.get_element_tree_with_meta(
            info.handle, window_uid=info.window_uid)
        if tree is None:
            return error_dict(Code.INTERNAL, "could not retrieve element tree",
                              step_id=step_id, window_uid=info.window_uid)
        perf = {"capture_ms": meta["capture_ms"],
                "node_count": meta["node_count"],
                "cache": meta["cache"],
                "depth_used": depth}
    full_serialized = tree.to_dict()
    th = tree_hash(tree)
    if not scope:
        token = get_session().tree_tokens.put(info.window_uid,
                                              full_serialized, th)
    serialized, depth_truncated = _truncate_depth(full_serialized, depth)

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
        serialized or {},
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

    out = {
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
        "depth_used": depth,
        "depth_truncated": depth_truncated,
        "perf": perf,
    }
    if scope:
        out["scope"] = scope
    else:
        # Degradation signal: accessibility-dark windows (games, custom
        # renderers) produce sparse trees — steer the agent to pixel-based
        # fallbacks instead of letting it act on a near-empty tree.
        named = sum(1 for e in tree.flat_list()[1:] if (e.name or "").strip())
        threshold = int((ctx.config.get("tree", {}) or {})
                        .get("sparse_threshold", 5))
        if named < threshold:
            out["degraded"] = {
                "reason": "sparse_accessibility_tree",
                "named_node_count": named,
                "threshold": threshold,
                "suggested_fallbacks": ["get_ocr", "get_screen_description"],
            }
    return out


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


def _perf_dict(meta: Dict[str, Any], depth: Optional[int]) -> Dict[str, Any]:
    return {"capture_ms": meta["capture_ms"],
            "node_count": meta["node_count"],
            "cache": meta["cache"],
            "depth_used": depth}


def _serialize_full_observation(ctx: ToolContext, info: WindowInfo,
                                depth: Optional[int] = None,
                                 ) -> Tuple[Optional[UIElement], Dict[str, Any]]:
    tree, meta = ctx.observer.get_element_tree_with_meta(
        info.handle, window_uid=info.window_uid)
    if tree is None:
        return None, {"error": "no tree"}
    serialized = tree.to_dict()
    th = tree_hash(tree)
    # Diff baselines keep the full capture; only the returned tree is
    # depth-bounded (with truncated-node markers).
    token = get_session().tree_tokens.put(info.window_uid, serialized, th)
    out_tree: 'Optional[Dict[str, Any]]' = serialized
    depth_truncated = False
    if depth is not None:
        out_tree, depth_truncated = _truncate_depth(serialized, depth)
    return tree, {
        "format": "full",
        "window_uid": info.window_uid,
        "window": info.title,
        "tree": out_tree,
        "tree_hash": th,
        "tree_token": token,
        "base_token": None,
        "depth_used": depth,
        "depth_truncated": depth_truncated,
        "perf": _perf_dict(meta, depth),
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
    depth = _effective_depth(ctx, args)
    changed_only = bool(args.get("changed_only"))

    if not since and changed_only:
        return _observe_changed_only(ctx, info, depth=depth,
                                     step_id=step_id, caused_by=caused_by)

    if not since:
        _, full = _serialize_full_observation(ctx, info, depth=depth)
        full.update({"ok": True, "success": True,
                     "step_id": step_id, "caused_by_step_id": caused_by,
                     "format": "full"})
        return full

    entry = get_session().tree_tokens.get(since)
    tree, meta = ctx.observer.get_element_tree_with_meta(
        info.handle, window_uid=info.window_uid)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id, window_uid=info.window_uid)
    serialized = tree.to_dict()
    th = tree_hash(tree)
    new_token = get_session().tree_tokens.put(info.window_uid, serialized, th)

    if entry is None or entry.window_uid != info.window_uid:
        # Token expired/wrong-window: return full tree (depth-bounded).
        out_tree, depth_truncated = _truncate_depth(serialized, depth)
        return {
            "ok": True, "success": True,
            "step_id": step_id, "caused_by_step_id": caused_by,
            "window_uid": info.window_uid, "window": info.title,
            "tree": out_tree, "tree_hash": th,
            "tree_token": new_token, "base_token": None,
            "format": "full",
            "depth_used": depth,
            "depth_truncated": depth_truncated,
            "perf": _perf_dict(meta, depth),
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
        "perf": _perf_dict(meta, depth),
    }


def _observe_changed_only(ctx: ToolContext, info: WindowInfo, *, depth: int,
                          step_id: int, caused_by: Optional[int]
                          ) -> Dict[str, Any]:
    """observe_window changed_only=true: compare a fresh capture against the
    last cached capture of the window.  Unchanged → a tiny
    {unchanged: true, tree_hash} response; changed → a custom diff instead
    of the full tree; no baseline → full tree."""
    from diff import diff_custom
    sess = get_session()
    # Baseline: the most recent capture, regardless of cache TTL.
    baseline = sess.tree_cache.peek(info.window_uid)

    # Fresh capture (bypass the cache — the whole point is to detect drift).
    tree, meta = ctx.observer.get_element_tree_with_meta(
        info.handle, window_uid=info.window_uid, use_cache=False)
    if tree is None:
        return error_dict(Code.INTERNAL, "could not retrieve element tree",
                          step_id=step_id, window_uid=info.window_uid)
    serialized = tree.to_dict()
    th = tree_hash(tree)
    new_token = sess.tree_tokens.put(info.window_uid, serialized, th)
    perf = _perf_dict(meta, depth)

    base: Dict[str, Any] = {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "window_uid": info.window_uid, "window": info.title,
        "tree_hash": th, "tree_token": new_token,
        "changed_only": True,
        "perf": perf,
    }

    if baseline is None:
        # Nothing to compare against — return the (depth-bounded) full tree.
        out_tree, depth_truncated = _truncate_depth(serialized, depth)
        base.update({"format": "full", "tree": out_tree, "base_token": None,
                     "depth_used": depth, "depth_truncated": depth_truncated})
        return base

    if baseline.tree_hash == th:
        base["unchanged"] = True
        return base

    changes = diff_custom(baseline.serialized, serialized)
    base.update({"format": "custom", "changes": changes, "unchanged": False})
    return base


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

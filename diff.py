"""
diff.py — Tree diff (custom default + RFC 6902 JSON Patch).

Per design doc §13:
  - Custom format emits {op: add|remove|replace|move, path, ...} ops with
    slash-delimited child-index paths.
  - JSON Patch format emits RFC 6902 ops over the serialized tree dict
    (key '/children/N/...').

Both operate on the dict form returned by UIElement.to_dict().  A node's
identity for the purpose of detecting 'move' is (role, name).  When
identities are ambiguous we emit add+remove pairs instead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# ─── Custom format ────────────────────────────────────────────────────────────

def diff_custom(before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Compare two serialized trees.  Returns a list of changes:
        {op:"add",     path, node}
        {op:"remove",  path}
        {op:"replace", path, fields}        # only fields that changed
        {op:"move",    from, to}
    """
    changes: List[Dict[str, Any]] = []
    _diff_node("", before, after, changes)
    return changes


def _diff_node(path: str, a: Dict, b: Dict, out: List[Dict[str, Any]]) -> None:
    field_changes: Dict[str, Any] = {}
    for key in ("name", "role", "value", "enabled", "keyboard_shortcut",
                "description"):
        if a.get(key) != b.get(key):
            field_changes[key] = b.get(key)
    if a.get("bounds") != b.get("bounds"):
        field_changes["bounds"] = b.get("bounds")
    if field_changes:
        # path="" addresses the root; descendants use slash-delimited indices.
        out.append({"op": "replace", "path": path,
                    "fields": field_changes})

    a_children = a.get("children", []) or []
    b_children = b.get("children", []) or []

    # Build (role, name) -> first position maps so we can detect moves.
    # When a sibling identity appears more than once on EITHER side we
    # can no longer unambiguously match across the two lists; in that
    # case the duplicates are excluded from move detection and fall
    # through to the add/remove pairs below.
    a_counts: Dict[Tuple[str, str], int] = {}
    b_counts: Dict[Tuple[str, str], int] = {}
    for n in a_children:
        a_counts[_identity(n)] = a_counts.get(_identity(n), 0) + 1
    for n in b_children:
        b_counts[_identity(n)] = b_counts.get(_identity(n), 0) + 1
    _ambiguous = {ident for ident, c in a_counts.items() if c > 1} | \
                 {ident for ident, c in b_counts.items() if c > 1}

    a_id_to_pos: Dict[Tuple[str, str], int] = {}
    for idx, n in enumerate(a_children):
        ident = _identity(n)
        if ident in _ambiguous:
            continue
        a_id_to_pos.setdefault(ident, idx)
    b_id_to_pos: Dict[Tuple[str, str], int] = {}
    for idx, n in enumerate(b_children):
        ident = _identity(n)
        if ident in _ambiguous:
            continue
        b_id_to_pos.setdefault(ident, idx)

    matched_b: set = set()
    matched_a: set = set()

    # Detect moves and recurse into matched pairs.
    for a_idx, an in enumerate(a_children):
        ident = _identity(an)
        if ident in b_id_to_pos:
            b_idx = b_id_to_pos[ident]
            if b_idx in matched_b:
                continue
            matched_a.add(a_idx)
            matched_b.add(b_idx)
            child_path = f"{path}/{a_idx}" if path else str(a_idx)
            new_path  = f"{path}/{b_idx}" if path else str(b_idx)
            if a_idx != b_idx:
                out.append({"op": "move", "from": child_path, "to": new_path})
            _diff_node(new_path, an, b_children[b_idx], out)

    # Removed
    for a_idx, an in enumerate(a_children):
        if a_idx in matched_a:
            continue
        out.append({"op": "remove",
                    "path": f"{path}/{a_idx}" if path else str(a_idx)})

    # Added
    for b_idx, bn in enumerate(b_children):
        if b_idx in matched_b:
            continue
        out.append({"op": "add",
                    "path": f"{path}/{b_idx}" if path else str(b_idx),
                    "node": bn})


def _identity(node: Dict) -> Tuple[str, str]:
    return (node.get("role") or "", node.get("name") or "")


# ─── RFC 6902 JSON Patch ─────────────────────────────────────────────────────

def diff_json_patch(before: Dict[str, Any], after: Dict[str, Any]
                    ) -> List[Dict[str, Any]]:
    """Convert a custom diff into RFC 6902 ops over the serialized tree."""
    custom = diff_custom(before, after)
    patch: List[Dict[str, Any]] = []
    for c in custom:
        if c["op"] == "replace":
            for field, value in c["fields"].items():
                patch.append({
                    "op": "replace",
                    "path": _to_pointer(c["path"], field),
                    "value": value,
                })
        elif c["op"] == "add":
            patch.append({"op": "add",
                          "path": _to_pointer(c["path"]),
                          "value": c["node"]})
        elif c["op"] == "remove":
            patch.append({"op": "remove", "path": _to_pointer(c["path"])})
        elif c["op"] == "move":
            patch.append({"op": "move",
                          "from": _to_pointer(c["from"]),
                          "path": _to_pointer(c["to"])})
    return patch


def _to_pointer(child_path: str, field: Optional[str] = None) -> str:
    """Convert '0/2/3' to '/children/0/children/2/children/3' (root-relative)."""
    parts = [p for p in (child_path or "").split("/") if p != ""]
    pointer = "".join(f"/children/{p}" for p in parts)
    if field is not None:
        pointer += f"/{field}"
    return pointer or ""


# ─── Apply ────────────────────────────────────────────────────────────────────

def apply_custom(before: Dict[str, Any],
                 changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply a custom diff to *before* and return the resulting tree."""
    import copy
    after = copy.deepcopy(before)
    # Apply replaces, removes (in reverse path order), then adds (forward),
    # then moves (last).  Caller-supplied order is respected for moves; for
    # adds and removes we sort by path depth to avoid index drift.
    replaces = [c for c in changes if c["op"] == "replace"]
    moves    = [c for c in changes if c["op"] == "move"]
    removes  = [c for c in changes if c["op"] == "remove"]
    adds     = [c for c in changes if c["op"] == "add"]

    for c in replaces:
        n = _resolve(after, c["path"])
        n.update(c["fields"])

    # remove deepest-first to keep sibling indices stable
    for c in sorted(removes, key=lambda x: -x["path"].count("/")):
        parent_path, idx = _split_last(c["path"])
        parent = _resolve(after, parent_path) if parent_path else after
        children = parent.get("children", [])
        if 0 <= idx < len(children):
            children.pop(idx)

    for c in sorted(adds, key=lambda x: x["path"].count("/")):
        parent_path, idx = _split_last(c["path"])
        parent = _resolve(after, parent_path) if parent_path else after
        children = parent.setdefault("children", [])
        children.insert(idx, c["node"])

    for c in moves:
        # Treat as remove + insert
        from_parent_path, from_idx = _split_last(c["from"])
        from_parent = _resolve(after, from_parent_path) if from_parent_path else after
        node = from_parent["children"].pop(from_idx)
        to_parent_path, to_idx = _split_last(c["to"])
        to_parent = _resolve(after, to_parent_path) if to_parent_path else after
        to_parent.setdefault("children", []).insert(to_idx, node)

    return after


def _resolve(tree: Dict, path: str) -> Dict:
    parts = [p for p in path.split("/") if p != ""]
    cur = tree
    for p in parts:
        cur = cur["children"][int(p)]
    return cur


def _split_last(path: str) -> Tuple[str, int]:
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) <= 1:
        return ("", int(parts[-1]) if parts else 0)
    return ("/".join(parts[:-1]), int(parts[-1]))

"""
hashing.py — Stable tree and snapshot hashing.

Per design doc D13: tree hash includes role, name, value, bounds, enabled.
It excludes 'focused' (changes during normal interaction without meaningful
state drift) and any timestamps.  Element IDs are also excluded because they
renumber across walks; structural shape is captured via a pre-order traversal.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List


def tree_hash(elem: Any) -> str:
    """Return 'sha1:<hex>' over a stable serialization of *elem*."""
    h = hashlib.sha1()
    _feed(elem, h)
    return "sha1:" + h.hexdigest()


def _feed(elem: Any, h: "hashlib._Hash") -> None:
    h.update((elem.role or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((elem.name or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((elem.value or "").encode("utf-8"))
    h.update(b"\x00")
    b = elem.bounds
    h.update(f"{b.x},{b.y},{b.width},{b.height}".encode("ascii"))
    h.update(b"\x00")
    h.update(b"E" if elem.enabled else b"D")
    h.update(b"\x00")
    h.update(f"{len(elem.children)}".encode("ascii"))
    h.update(b"\x00")
    for c in elem.children:
        _feed(c, h)


def windows_hash(windows: Iterable[Any]) -> str:
    """Hash the (uid, title, bounds, focused) tuple-set of a window list."""
    h = hashlib.sha1()
    items: List[Dict] = []
    for w in windows:
        items.append({
            "uid":     getattr(w, "window_uid", None) or str(getattr(w, "handle", "")),
            "title":   w.title or "",
            "process": w.process_name or "",
            "bounds":  w.bounds.to_dict(),
            "focused": bool(w.is_focused),
        })
    items.sort(key=lambda x: x["uid"])
    h.update(json.dumps(items, sort_keys=True).encode("utf-8"))
    return "sha1:" + h.hexdigest()


def focused_selector(root: Any) -> str:
    """Return a short selector path to the focused element, or '' if none."""
    found = _find_focused(root)
    if not found:
        return ""
    from element_selectors import selector_for
    sel = selector_for(root, found.element_id)
    return sel or ""


def _find_focused(elem: Any) -> Any:
    if elem.focused:
        return elem
    for c in elem.children:
        r = _find_focused(c)
        if r:
            return r
    return None

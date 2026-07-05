"""
tree_cache.py — Per-window accessibility-tree cache (perception performance P1).

Walking the accessibility tree is by far the most expensive observation
primitive (multi-second on complex Windows apps).  Historically every tool
call re-walked the full tree; this module caches the most recent capture per
``window_uid`` so read-only tools within a short TTL reuse it.

Coherence model
---------------
  - Entries expire after ``tree.cache_ttl_s`` seconds (default 2.0).
  - ``tools.dispatch()`` invalidates a window's entry after any input tool
    (clicks, typing, bring_to_foreground, …) executes, so post-action reads
    never observe pre-action state.
  - Post-action re-reads (ActionReceipt ``after`` captures) bypass the cache
    explicitly via ``use_cache=False``.

The cache also remembers lightweight per-window last-capture statistics
(node counts) which survive invalidation; ``get_capabilities`` surfaces them
so agents can detect accessibility-dark windows.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional

# Defaults (overridable via config / constructor).
DEFAULT_TTL_S = 2.0
DEFAULT_MAX_WINDOWS = 8
_MAX_STATS = 64          # per-window last-capture stats retained


@dataclass
class TreeCacheEntry:
    window_uid: str
    tree: Any                      # UIElement root of the capture
    serialized: Dict[str, Any]     # tree.to_dict() of the same capture
    tree_hash: str
    captured_at: float
    max_depth: int                 # depth cap in effect when captured
    capture_ms: int = 0
    node_count: int = 0
    named_node_count: int = 0

    def age_s(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.captured_at


class TreeCache:
    """Thread-safe TTL + LRU cache of per-window accessibility trees."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S,
                 max_windows: int = DEFAULT_MAX_WINDOWS) -> None:
        self.ttl_s = float(ttl_s)
        self.max_windows = int(max_windows)
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, TreeCacheEntry]" = OrderedDict()
        # Last-capture stats per window; kept across invalidation so
        # capability reporting works even when the cache is cold.
        self._stats: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # ── Read ─────────────────────────────────────────────────────────────────

    def get(self, window_uid: str,
            ttl_s: Optional[float] = None) -> Optional[TreeCacheEntry]:
        """Return a fresh entry for *window_uid*, or None (expired entries
        are dropped).  *ttl_s* overrides the instance TTL for this lookup."""
        limit = self.ttl_s if ttl_s is None else float(ttl_s)
        with self._lock:
            entry = self._entries.get(window_uid)
            if entry is None:
                return None
            if entry.age_s() > limit:
                self._entries.pop(window_uid, None)
                return None
            self._entries.move_to_end(window_uid)
            return entry

    def peek(self, window_uid: str) -> Optional[TreeCacheEntry]:
        """Return the last capture regardless of TTL (no LRU touch).
        Used as the baseline for changed_only comparisons."""
        with self._lock:
            return self._entries.get(window_uid)

    # ── Write ────────────────────────────────────────────────────────────────

    def put(self, window_uid: str, *, tree: Any, serialized: Dict[str, Any],
            tree_hash: str, max_depth: int, capture_ms: int = 0,
            node_count: int = 0, named_node_count: int = 0) -> TreeCacheEntry:
        entry = TreeCacheEntry(
            window_uid=window_uid, tree=tree, serialized=serialized,
            tree_hash=tree_hash, captured_at=time.time(),
            max_depth=int(max_depth), capture_ms=int(capture_ms),
            node_count=int(node_count),
            named_node_count=int(named_node_count),
        )
        with self._lock:
            self._entries[window_uid] = entry
            self._entries.move_to_end(window_uid)
            while len(self._entries) > self.max_windows:
                self._entries.popitem(last=False)
            self._stats[window_uid] = {
                "captured_at": entry.captured_at,
                "capture_ms": entry.capture_ms,
                "node_count": entry.node_count,
                "named_node_count": entry.named_node_count,
            }
            self._stats.move_to_end(window_uid)
            while len(self._stats) > _MAX_STATS:
                self._stats.popitem(last=False)
        return entry

    # ── Invalidation ─────────────────────────────────────────────────────────

    def invalidate(self, window_uid: str) -> bool:
        """Drop the entry for one window.  Returns True when one existed."""
        with self._lock:
            return self._entries.pop(window_uid, None) is not None

    def invalidate_all(self) -> None:
        with self._lock:
            self._entries.clear()

    # ── Introspection ────────────────────────────────────────────────────────

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, window_uid: str) -> bool:
        with self._lock:
            return window_uid in self._entries

    def stats(self) -> Dict[str, Dict[str, Any]]:
        """Per-window last-capture statistics (survive invalidation)."""
        with self._lock:
            return {uid: dict(s) for uid, s in self._stats.items()}


# Convenience default factory used by session.Session.
def default_tree_cache() -> TreeCache:
    return TreeCache()


__all__ = ["TreeCache", "TreeCacheEntry", "default_tree_cache",
           "DEFAULT_TTL_S", "DEFAULT_MAX_WINDOWS"]

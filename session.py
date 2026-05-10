"""
session.py — Single global session state (design doc D2).

Holds per-process state shared between the REST and MCP interfaces:
  - step_id counter and last input step (for caused_by_step_id)
  - tree token ring buffer per window_uid (for since= diffs)
  - snapshots (TTL-bounded, LRU)
  - confirmation tokens (TTL-bounded)
  - active trace handle (set by tracing.py)

All access is thread-safe.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ─── Tunables (overridable from config) ──────────────────────────────────────

TREE_TOKEN_TTL_SEC = 300         # 5 minutes
TREE_TOKEN_RING    = 16          # per window_uid
SNAPSHOT_TTL_SEC   = 300
SNAPSHOT_MAX       = 32
CONFIRM_TTL_SEC    = 60


# ─── Tree token storage ──────────────────────────────────────────────────────

@dataclass
class _TreeEntry:
    token: str
    window_uid: str
    serialized: Dict[str, Any]   # the dict version of the UIElement tree
    tree_hash: str
    expires_at: float


class _TreeTokenStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_token: "OrderedDict[str, _TreeEntry]" = OrderedDict()
        self._by_window: Dict[str, List[str]] = {}     # uid -> [token, …] (oldest first)

    def put(self, window_uid: str, serialized_tree: Dict[str, Any],
            tree_hash: str) -> str:
        with self._lock:
            self._evict_expired()
            token = "tt:" + secrets.token_hex(8)
            entry = _TreeEntry(
                token=token, window_uid=window_uid,
                serialized=serialized_tree, tree_hash=tree_hash,
                expires_at=time.time() + TREE_TOKEN_TTL_SEC,
            )
            self._by_token[token] = entry
            ring = self._by_window.setdefault(window_uid, [])
            ring.append(token)
            while len(ring) > TREE_TOKEN_RING:
                old = ring.pop(0)
                self._by_token.pop(old, None)
            return token

    def get(self, token: str) -> Optional[_TreeEntry]:
        with self._lock:
            entry = self._by_token.get(token)
            if entry is None:
                return None
            if entry.expires_at < time.time():
                self._by_token.pop(token, None)
                return None
            return entry

    def _evict_expired(self) -> None:
        now = time.time()
        stale = [t for t, e in self._by_token.items() if e.expires_at < now]
        for t in stale:
            self._by_token.pop(t, None)
        for uid, ring in list(self._by_window.items()):
            ring[:] = [t for t in ring if t in self._by_token]
            if not ring:
                self._by_window.pop(uid, None)


# ─── Snapshot storage ────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    snapshot_id: str
    ts: float
    windows: List[Dict[str, Any]]
    trees: Dict[str, Dict[str, Any]]   # window_uid -> serialized tree
    tree_hashes: Dict[str, str]
    expires_at: float


class _SnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: "OrderedDict[str, Snapshot]" = OrderedDict()

    def put(self, windows: List[Dict[str, Any]],
            trees: Dict[str, Dict[str, Any]],
            tree_hashes: Dict[str, str]) -> Snapshot:
        with self._lock:
            self._evict()
            sid = "snap:" + secrets.token_hex(8)
            snap = Snapshot(
                snapshot_id=sid, ts=time.time(),
                windows=windows, trees=trees, tree_hashes=tree_hashes,
                expires_at=time.time() + SNAPSHOT_TTL_SEC,
            )
            self._items[sid] = snap
            while len(self._items) > SNAPSHOT_MAX:
                self._items.popitem(last=False)
            return snap

    def get(self, sid: str) -> Optional[Snapshot]:
        with self._lock:
            s = self._items.get(sid)
            if not s:
                return None
            if s.expires_at < time.time():
                self._items.pop(sid, None)
                return None
            self._items.move_to_end(sid)
            return s

    def drop(self, sid: str) -> bool:
        with self._lock:
            return self._items.pop(sid, None) is not None

    def _evict(self) -> None:
        now = time.time()
        for sid, s in list(self._items.items()):
            if s.expires_at < now:
                self._items.pop(sid, None)


# ─── Confirmation tokens ─────────────────────────────────────────────────────

@dataclass
class ConfirmToken:
    token: str
    action: str
    window_uid: str
    selector: str
    bbox: Dict[str, int]
    args: Dict[str, Any]
    expires_at: float
    used: bool = False


class _ConfirmStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: Dict[str, ConfirmToken] = {}

    def issue(self, *, action: str, window_uid: str, selector: str,
              bbox: Dict[str, int], args: Dict[str, Any]) -> ConfirmToken:
        with self._lock:
            self._evict()
            tok = "ct:" + secrets.token_hex(8)
            ct = ConfirmToken(
                token=tok, action=action, window_uid=window_uid,
                selector=selector, bbox=bbox, args=dict(args),
                expires_at=time.time() + CONFIRM_TTL_SEC,
            )
            self._items[tok] = ct
            return ct

    def consume(self, token: str) -> Optional[ConfirmToken]:
        with self._lock:
            ct = self._items.get(token)
            if not ct or ct.used or ct.expires_at < time.time():
                if ct:
                    self._items.pop(token, None)
                return None
            ct.used = True
            return ct

    def _evict(self) -> None:
        now = time.time()
        for tok, ct in list(self._items.items()):
            if ct.expires_at < now or ct.used:
                self._items.pop(tok, None)


# ─── Step counter ────────────────────────────────────────────────────────────

class _StepCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 1
        self._last_input_step: Optional[int] = None
        self._started_at = time.time()

    def next_id(self, *, is_input: bool) -> Tuple[int, Optional[int]]:
        """Returns (step_id, caused_by_step_id)."""
        with self._lock:
            sid = self._next
            self._next += 1
            if is_input:
                caused_by = sid
                self._last_input_step = sid
            else:
                caused_by = self._last_input_step
            return sid, caused_by

    @property
    def count(self) -> int:
        with self._lock:
            return self._next - 1

    @property
    def uptime_s(self) -> float:
        return time.time() - self._started_at


# ─── Top-level session object ────────────────────────────────────────────────

@dataclass
class Session:
    tree_tokens: _TreeTokenStore = field(default_factory=_TreeTokenStore)
    snapshots:   _SnapshotStore  = field(default_factory=_SnapshotStore)
    confirms:    _ConfirmStore   = field(default_factory=_ConfirmStore)
    steps:       _StepCounter    = field(default_factory=_StepCounter)

    # Set by tracing.py when a trace is active.
    active_trace: Optional[Any] = None

    # Set by budgets.py (lazy import to avoid circular).
    budgets: Optional[Any] = None

    # Set by main.py from config (P5: redaction.Redactor, audit.AuditLogger).
    redactor: Optional[Any] = None
    auditor:  Optional[Any] = None


_GLOBAL: Optional[Session] = None
_GLOBAL_LOCK = threading.Lock()


def get_session() -> Session:
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = Session()
        return _GLOBAL


def reset_session_for_tests() -> Session:
    """Test helper: drop the singleton and create a fresh session."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        _GLOBAL = Session()
        return _GLOBAL

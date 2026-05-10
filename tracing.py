"""
tracing.py — Trace recording (design doc §15.2, D10, D16, D17).

Layout:
    traces/
      <trace_id>/
        trace.jsonl
        screenshots/
          step-NNNNN-full.png
          step-NNNNN-window.png

Hooked into tools.dispatch: every tool call is recorded with its args
(after audit-style redaction), result_summary, and tree hash before/after.
At the configured cadence the screenshot pair is written too.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DIR = "./traces"
DEFAULT_CADENCE = 5
DEFAULT_MAX_ARGS_BYTES = 4096
DEFAULT_REDACT_KEYS = ["api_key", "password", "Authorization", "token",
                       "confirm_token"]


@dataclass
class _Counter:
    value: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self) -> int:
        with self.lock:
            self.value += 1
            return self.value


@dataclass
class TraceHandle:
    trace_id: str
    started_at: float
    dir: str
    label: str = ""
    counter: _Counter = field(default_factory=_Counter)
    cadence: int = DEFAULT_CADENCE
    max_args_bytes: int = DEFAULT_MAX_ARGS_BYTES
    redact_keys: List[str] = field(default_factory=lambda: list(DEFAULT_REDACT_KEYS))
    file_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def jsonl_path(self) -> str:
        return os.path.join(self.dir, "trace.jsonl")


def start(*, label: str = "", config: Optional[Dict[str, Any]] = None
          ) -> TraceHandle:
    cfg = (config or {}).get("tracing", {}) or {}
    base_dir = cfg.get("dir", DEFAULT_DIR)
    cadence = int(cfg.get("screenshot_every_n_actions", DEFAULT_CADENCE))
    max_args = int(cfg.get("max_args_bytes", DEFAULT_MAX_ARGS_BYTES))
    redact = list(cfg.get("redact_keys", DEFAULT_REDACT_KEYS))

    trace_id = "trace-" + secrets.token_hex(4)
    trace_dir = os.path.join(base_dir, trace_id)
    os.makedirs(os.path.join(trace_dir, "screenshots"), exist_ok=True)
    handle = TraceHandle(
        trace_id=trace_id, started_at=time.time(), dir=trace_dir,
        label=label, cadence=cadence, max_args_bytes=max_args,
        redact_keys=redact,
    )
    # Write a header record so consumers can identify the trace.
    _append(handle, {
        "type": "header", "trace_id": trace_id, "label": label,
        "started_at": _iso(handle.started_at),
        "tool_version": "0.2.0",
    })
    return handle


def stop(handle: TraceHandle) -> Dict[str, Any]:
    if handle.closed:
        return {"trace_id": handle.trace_id, "path": handle.jsonl_path(),
                "step_count": handle.counter.value, "duration_ms": 0,
                "already_closed": True}
    duration_ms = int((time.time() - handle.started_at) * 1000)
    _append(handle, {"type": "footer",
                     "stopped_at": _iso(time.time()),
                     "step_count": handle.counter.value,
                     "duration_ms": duration_ms})
    handle.closed = True
    return {
        "trace_id": handle.trace_id,
        "path": handle.jsonl_path(),
        "step_count": handle.counter.value,
        "duration_ms": duration_ms,
    }


def record(handle: TraceHandle, *,
           tool: str, caller: str, args: Dict[str, Any],
           result: Dict[str, Any], duration_ms: int,
           tree_hash_before: str, tree_hash_after: str,
           full_screenshot: Optional[bytes] = None,
           window_screenshot: Optional[bytes] = None) -> None:
    """Write one row.  Caller decides whether to supply screenshots."""
    if handle.closed:
        return
    seq = handle.counter.inc()

    full_ref = window_ref = None
    if handle.cadence > 0 and (seq == 1 or seq % handle.cadence == 0):
        if full_screenshot:
            full_ref = _save_png(handle, seq, "full", full_screenshot)
        if window_screenshot:
            window_ref = _save_png(handle, seq, "window", window_screenshot)

    row = {
        "step_id": result.get("step_id", seq),
        "trace_seq": seq,
        "ts": _iso(time.time()),
        "caller": caller,
        "tool": tool,
        "args": _redact_args(args, handle.redact_keys, handle.max_args_bytes),
        "result_summary": _summarize_result(result),
        "tree_hash_before": tree_hash_before,
        "tree_hash_after": tree_hash_after,
        "duration_ms": duration_ms,
    }
    if full_ref:
        row["screenshot_full_ref"] = full_ref
    if window_ref:
        row["screenshot_window_ref"] = window_ref
    _append(handle, row)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _append(handle: TraceHandle, row: Dict[str, Any]) -> None:
    line = json.dumps(row, separators=(",", ":"))
    with handle.file_lock:
        with open(handle.jsonl_path(), "a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")


def _save_png(handle: TraceHandle, seq: int, kind: str, png: bytes) -> str:
    rel = os.path.join("screenshots", f"step-{seq:05d}-{kind}.png")
    abs_path = os.path.join(handle.dir, rel)
    try:
        with open(abs_path, "wb") as f:
            f.write(png)
    except Exception:
        logger.exception("trace screenshot save failed")
        return ""
    return rel


def _redact_args(args: Dict[str, Any], redact_keys: List[str],
                 max_bytes: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in redact_keys:
            out[k] = "<redacted>"
            continue
        out[k] = v
    raw = json.dumps(out, default=str)
    if len(raw.encode("utf-8")) > max_bytes:
        out = {"__truncated": True, "preview": raw[:max_bytes // 2]}
    return out


_SUMMARY_KEYS = (
    "ok", "step_id", "action", "changed", "matched_index", "effective_mode",
    "tree_hash", "all_passed", "node_count", "duration_ms",
    "ambiguous_matches", "count", "format", "width", "height",
)


def _summarize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = {k: result[k] for k in _SUMMARY_KEYS if k in result}
    # Preserve the resolved target's selector/uid so replay-verify can compare.
    target = result.get("target")
    if isinstance(target, dict):
        summary["target"] = {
            "selector":   target.get("selector"),
            "window_uid": target.get("window_uid"),
        }
    err = result.get("error")
    if isinstance(err, dict):
        summary["error"] = {"code": err.get("code")}
    elif isinstance(err, str):
        summary["error"] = {"code": "Internal"}
    return summary


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

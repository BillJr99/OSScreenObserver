"""
replay.py — Trace replay (design doc §15.3 + §15.4 comparison rules table).

Modes:
  execute  — re-issue each tool call; ignore recorded result; emit fresh trace.
  verify   — re-issue each tool call; compare result against the recorded
             one using the per-tool comparison rules; record divergences.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ─── §15.4: per-tool comparison rules ────────────────────────────────────────

COMPARE_FIELDS: Dict[str, List[str]] = {
    "list_windows":             ["count"],
    "get_window_structure":     ["node_count", "tree_hash"],
    "find_element":             ["ok", "error.code", "ambiguous_matches>0"],
    "click_element":            ["ok", "error.code", "target.selector", "changed"],
    "focus_element":            ["ok", "error.code", "target.selector", "changed"],
    "invoke_element":           ["ok", "error.code", "target.selector", "changed"],
    "set_value":                ["ok", "error.code", "target.selector", "changed"],
    "select_option":            ["ok", "error.code", "target.selector", "changed"],
    "click_at":                 ["ok", "error.code"],
    "hover_at":                 ["ok", "error.code"],
    "drag":                     ["ok", "error.code"],
    "type_text":                ["ok", "error.code"],
    "press_key":                ["ok", "error.code"],
    "scroll":                   ["ok", "error.code"],
    "get_screen_description":   ["ok", "effective_mode"],
    "get_screenshot":           ["ok", "width", "height"],
    "get_screenshot_cropped":   ["ok"],
    "get_ocr":                  ["ok"],
    "wait_for":                 ["ok", "matched_index"],
    "wait_idle":                ["ok"],
    "assert_state":             ["ok", "all_passed"],
    "snapshot":                 ["ok"],
    "snapshot_get":             ["ok"],
    "snapshot_diff":            ["ok"],
    "snapshot_drop":            ["ok"],
    "get_capabilities":         ["ok"],
    "get_monitors":             ["ok"],
    "observe_window":           ["ok", "format"],
    "click_element_and_observe": ["ok", "target.selector", "changed"],
    "type_and_observe":         ["ok"],
    "press_key_and_observe":    ["ok"],
    "bring_to_foreground":      ["ok"],
    "get_visible_areas":        ["ok"],
}


def _get_path(d: Dict[str, Any], path: str) -> Any:
    """Resolve dotted path with optional tail '>0' suffix."""
    if path.endswith(">0"):
        v = _get_path(d, path[:-2])
        try:
            return int(v) > 0
        except (TypeError, ValueError):
            return False
    cur: Any = d
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


# ─── Replay state ────────────────────────────────────────────────────────────

@dataclass
class Replay:
    path: str
    mode: str                                 # execute | verify
    on_divergence: str                        # stop | warn | resume
    rows: List[Dict[str, Any]] = field(default_factory=list)
    position: int = 0
    divergences: List[Dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    label: str = ""


def load(path: str, *, mode: str = "execute",
         on_divergence: str = "warn") -> Replay:
    if mode not in ("execute", "verify"):
        raise ValueError(f"unknown mode {mode!r}")
    if on_divergence not in ("stop", "warn", "resume"):
        raise ValueError(f"unknown on_divergence {on_divergence!r}")
    rows: List[Dict[str, Any]] = []
    label = ""
    abs_path = path if os.path.isabs(path) else path
    # If the caller gave us a directory, look for trace.jsonl inside.
    if os.path.isdir(abs_path):
        abs_path = os.path.join(abs_path, "trace.jsonl")
    with open(abs_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "header":
                label = row.get("label", "")
                continue
            if row.get("type") == "footer":
                continue
            rows.append(row)
    return Replay(path=abs_path, mode=mode, on_divergence=on_divergence,
                  rows=rows, label=label)


def step(rep: Replay, *, dispatch: Any) -> Dict[str, Any]:
    """
    Advance one row.  *dispatch* must be callable as dispatch(name, args)
    returning a result dict.
    """
    if rep.finished:
        return {"finished": True, "position": rep.position, "total": len(rep.rows)}
    if rep.position >= len(rep.rows):
        rep.finished = True
        return {"finished": True, "position": rep.position, "total": len(rep.rows)}

    row = rep.rows[rep.position]
    tool = row.get("tool", "")
    args = row.get("args", {}) or {}
    recorded = row.get("result_summary", {}) or {}

    # Re-issue.
    actual = dispatch(tool, args)

    diff: List[Dict[str, Any]] = []
    if rep.mode == "verify":
        for fp in COMPARE_FIELDS.get(tool, ["ok"]):
            want = _get_path(recorded, fp)
            got  = _get_path(actual, fp)
            if want is None and got is None:
                continue
            if want != got:
                diff.append({"path": fp, "want": want, "got": got})
        if diff:
            rep.divergences.append({
                "step_id": row.get("step_id"),
                "trace_seq": row.get("trace_seq"),
                "tool": tool, "differences": diff,
            })
            if rep.on_divergence == "stop":
                rep.finished = True

    rep.position += 1
    if rep.position >= len(rep.rows):
        rep.finished = True

    return {
        "finished": rep.finished,
        "position": rep.position,
        "total": len(rep.rows),
        "tool": tool,
        "divergence": diff or None,
        "actual_summary": {k: actual.get(k) for k in
                           ("ok", "step_id", "action", "changed",
                            "matched_index", "effective_mode")
                           if k in actual},
    }

#!/usr/bin/env python3
"""
check_module_size.py — CI guard against god-files creeping back.

The design doc (agentic_features_design.md) caps modules at ~600 LOC:
"Keep modules under ~600 LOC; split when growing past that."  After the
P3 decomposition every tracked non-test source module fits under that
cap except a small set of intentional remainders listed in ALLOWLIST.

Fails (exit 1) when any git-tracked non-test .py file exceeds MAX_LINES
and is not allowlisted, and also when an allowlisted file shrinks back
under the cap (so stale allowlist entries are removed rather than
becoming silent escape hatches).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MAX_LINES = 600

# Intentional remainders.  Every entry needs a justification; new code
# modules must be split instead of added here.
ALLOWLIST: dict[str, str] = {
    # Declarative data blobs, not logic: one schema/asset entry per line
    # would gain nothing from splitting across files.
    "web_inspector/assets.py":
        "single inline HTML/CSS/JS template string for the SPA UI",
    "window_agent/tool_schemas.py":
        "declarative LLM tool catalogue (schemas + tiers + keyword groups)",
    "mcp_server/tool_schemas.py":
        "declarative MCP tools/list schema payload (_TOOLS)",
    # Cohesive single-responsibility modules slightly over the cap;
    # splitting them would separate tightly coupled halves.
    "ascii_renderer.py":
        "one renderer: grid projection + glyph rules + OCR overlay share state",
    "description.py":
        "one generator: accessibility/OCR/VLM description passes share helpers",
    "observer/adapters/windows.py":
        "one adapter: UIA COM walker + pywinauto walk + tree synthesis",
}


def tracked_py_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.splitlines()
            if p and not p.startswith("tests/")]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    failures: list[str] = []
    stale: list[str] = []
    seen_allowlisted: set[str] = set()

    for rel in tracked_py_files(root):
        path = root / rel
        try:
            n = sum(1 for _ in path.open("rb"))
        except OSError as e:
            failures.append(f"{rel}: unreadable ({e})")
            continue
        if rel in ALLOWLIST:
            seen_allowlisted.add(rel)
            if n <= MAX_LINES:
                stale.append(f"{rel}: {n} lines — now under the cap; "
                             f"remove its ALLOWLIST entry")
            continue
        if n > MAX_LINES:
            failures.append(
                f"{rel}: {n} lines exceeds the {MAX_LINES}-line module cap "
                f"(design doc: split modules growing past ~600 LOC)")

    for rel in sorted(set(ALLOWLIST) - seen_allowlisted):
        stale.append(f"{rel}: allowlisted but not tracked — remove the entry")

    for msg in failures + stale:
        print(f"check_module_size: {msg}", file=sys.stderr)
    if failures or stale:
        return 1
    print(f"check_module_size: OK — all tracked non-test modules are "
          f"<= {MAX_LINES} lines ({len(ALLOWLIST)} allowlisted exceptions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

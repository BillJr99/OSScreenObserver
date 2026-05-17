"""
setup_config.py — Interactive one-shot config fixups for the start scripts.

Called by start.sh / start-mac.sh / start.bat after the tesseract install
step. Does exactly two things, no more:

  1. If config.json does not exist, copy it from config.json.example so
     the user has a real file to edit (gitignored — config.json.example
     is the source of truth).

  2. Check ocr.tesseract_cmd. If it is set but points to a path that does
     not exist (the bundled example ships the Windows path, which is wrong
     on Linux/macOS), search PATH and a few common install locations,
     then offer to write the discovered path back to config.json.

Everything is opt-in via a Y/n prompt. If the prompts are skipped or the
script is run non-interactively (no TTY), no changes are made.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from typing import Optional


_COMMON_TESSERACT_PATHS = [
    # Linux
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    # macOS — Homebrew on Apple Silicon and Intel
    "/opt/homebrew/bin/tesseract",
    # Windows (the script will normalise PATH separators)
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def _confirm(prompt: str) -> bool:
    """Y-by-default Y/n prompt. False on EOF or non-TTY."""
    if not sys.stdin.isatty():
        return False
    try:
        reply = input(f"{prompt} [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("", "y", "yes")


def _find_tesseract_on_path() -> Optional[str]:
    """which/where tesseract — return absolute path or None."""
    path = shutil.which("tesseract")
    if path:
        return path
    # On Windows shutil.which may need the .exe suffix explicitly.
    return shutil.which("tesseract.exe")


def _find_tesseract() -> Optional[str]:
    """Return the first existing tesseract path: PATH first, then well-known."""
    p = _find_tesseract_on_path()
    if p and os.path.exists(p):
        return p
    for candidate in _COMMON_TESSERACT_PATHS:
        if os.path.exists(candidate):
            return candidate
    return None


def _atomic_write_json(path: str, data: dict) -> None:
    """Write *data* to *path* via temp-file + rename so a Ctrl-C mid-flush
    cannot truncate the user's config."""
    dir_name = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".json.tmp",
                               dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def bootstrap_config(config_path: str = "config.json",
                     example_path: str = "config.json.example") -> None:
    """Copy the example into place when config.json is missing."""
    if os.path.exists(config_path):
        return
    if not os.path.exists(example_path):
        print(f"[setup_config] {example_path!r} not found — cannot bootstrap "
              f"{config_path!r}.", file=sys.stderr)
        return
    shutil.copyfile(example_path, config_path)
    print(f"[setup_config] Seeded {config_path!r} from {example_path!r}.",
          file=sys.stderr)


def fix_tesseract_path(config_path: str = "config.json") -> None:
    """If ocr.tesseract_cmd in config_path is unset or broken, offer to set
    it to a tesseract binary discovered on the system."""
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[setup_config] Could not read {config_path!r}: {e}",
              file=sys.stderr)
        return

    ocr_section = cfg.get("ocr") or {}
    configured = ocr_section.get("tesseract_cmd")

    # Case A — the configured path is a real file. Nothing to do.
    if configured and os.path.exists(configured):
        return

    # Case B — no path set AND tesseract is on PATH. Also fine; pytesseract
    # will discover it. Don't pester the user.
    if not configured and _find_tesseract_on_path():
        return

    # Case C — broken configured path, OR tesseract is not on PATH but we
    # can find it at a well-known location. Either way, offer a fix.
    discovered = _find_tesseract()
    if discovered is None:
        # Truly missing — the install step in the launcher already warned.
        return

    if configured:
        print(f"[setup_config] ocr.tesseract_cmd in {config_path!r} points to "
              f"{configured!r}, which does not exist on this system.",
              file=sys.stderr)
    else:
        print(f"[setup_config] tesseract is not on PATH but was found at "
              f"{discovered!r}.", file=sys.stderr)

    if not _confirm(
        f"  Update ocr.tesseract_cmd to {discovered!r}?"
    ):
        print("  (skipping — OCR may not work until you fix this manually)",
              file=sys.stderr)
        return

    cfg.setdefault("ocr", {})["tesseract_cmd"] = discovered
    try:
        _atomic_write_json(config_path, cfg)
        print(f"[setup_config] Updated {config_path!r}: "
              f"ocr.tesseract_cmd = {discovered!r}", file=sys.stderr)
    except Exception as e:
        print(f"[setup_config] Could not write {config_path!r}: {e}",
              file=sys.stderr)


def main() -> int:
    bootstrap_config()
    fix_tesseract_path()
    return 0


if __name__ == "__main__":
    sys.exit(main())

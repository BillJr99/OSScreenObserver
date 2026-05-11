"""
ocr_util.py — Shared Tesseract/pytesseract configuration helper.

Every code path that calls pytesseract should call configure(config) first.
This is idempotent — the underlying state is module-level on pytesseract.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LAST_CONFIGURED: Optional[str] = None

# Reused by every code path that surfaces an OCR install / configuration
# failure so the fix instructions stay consistent.
INSTALL_HINT = (
    "Install Tesseract: Windows → "
    "https://github.com/tesseract-ocr/tesseract/releases  ·  "
    "macOS → 'brew install tesseract'  ·  "
    "Linux → 'sudo apt install tesseract-ocr'.  "
    "Then `pip install pytesseract`.  "
    "If the tesseract binary is not on PATH (the default on Windows), set "
    'ocr.tesseract_cmd in config.json to its full path.  Backslashes inside '
    'JSON strings must be escaped, e.g. '
    '"tesseract_cmd": "c:\\\\program files\\\\tesseract-ocr\\\\tesseract.exe", '
    'or use forward slashes: '
    '"tesseract_cmd": "c:/program files/tesseract-ocr/tesseract.exe".'
)


def configure(config: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Apply ocr.tesseract_cmd from *config* to pytesseract.

    Returns the path that was applied (or detected via PATH), or None when
    pytesseract isn't installed.  Safe to call many times; only re-applies
    when the configured path changes.
    """
    global _LAST_CONFIGURED
    try:
        import pytesseract
    except ImportError:
        return None

    cmd: Optional[str] = None
    if config:
        cmd = (config.get("ocr") or {}).get("tesseract_cmd") or None

    if cmd:
        # Normalise: trim quotes, expand env vars, accept forward slashes.
        cmd = os.path.expanduser(os.path.expandvars(cmd.strip().strip('"').strip("'")))
        cmd = cmd.replace("\\\\", "\\")
        if cmd != _LAST_CONFIGURED:
            pytesseract.pytesseract.tesseract_cmd = cmd
            _LAST_CONFIGURED = cmd
            logger.info(f"[ocr_util] tesseract_cmd set to {cmd!r}")
        return cmd

    # No explicit cmd — let pytesseract's default discovery (PATH) handle it.
    discovered = shutil.which("tesseract")
    return discovered


def diagnose(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Diagnostic snapshot of the Tesseract setup.  Useful when OCR fails;
    surface this via /api/healthz or include it in OCR error messages.
    """
    info: Dict[str, Any] = {"pytesseract_installed": False,
                             "configured_path": None,
                             "configured_path_exists": False,
                             "path_discovered": shutil.which("tesseract"),
                             "version": None,
                             "error": None,
                             "hint": INSTALL_HINT}
    try:
        import pytesseract  # noqa: F401
        info["pytesseract_installed"] = True
    except ImportError as e:
        info["error"] = (f"pytesseract not installed: {e}.  {INSTALL_HINT}")
        return info

    cmd = configure(config)
    info["configured_path"] = cmd
    if cmd and not os.path.exists(cmd) and shutil.which(cmd) is None:
        info["configured_path_exists"] = False
        info["error"] = (f"tesseract_cmd={cmd!r} does not exist on disk and "
                         f"is not on PATH.  {INSTALL_HINT}")
        return info
    info["configured_path_exists"] = bool(cmd) and (
        os.path.exists(cmd) or shutil.which(cmd) is not None
    )
    try:
        import pytesseract as _pt
        info["version"] = str(_pt.get_tesseract_version())
    except Exception as e:
        info["error"] = (f"pytesseract.get_tesseract_version() failed: {e}.  "
                         f"{INSTALL_HINT}")
    return info

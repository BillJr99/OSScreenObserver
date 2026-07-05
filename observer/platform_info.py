"""
Platform detection (Windows / macOS / Linux / WSL).

Split out of observer.py (P3); behavior is unchanged.
"""

import platform

PLATFORM = platform.system()


def _is_wsl() -> bool:
    """True when running inside Windows Subsystem for Linux (WSL 1 or WSL 2)."""
    if PLATFORM != "Linux":
        return False
    try:
        with open("/proc/version") as _f:
            return "microsoft" in _f.read().lower()
    except Exception:
        return False


IS_WSL = _is_wsl()
EFFECTIVE_PLATFORM = "WSL" if IS_WSL else PLATFORM

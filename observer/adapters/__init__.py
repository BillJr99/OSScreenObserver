"""
Platform adapters for ScreenObserver.

Split out of observer.py (P3); behavior is unchanged.  The top-level
mac_adapter.py / linux_adapter.py modules still hold the optional
pyobjc / pyatspi runtime upgrades that ScreenObserver installs over the
macOS / Linux stub adapters defined here.
"""

from __future__ import annotations

from observer.adapters.linux import LinuxAdapter
from observer.adapters.macos import MacOSAdapter
from observer.adapters.mock import MockAdapter
from observer.adapters.windows import (
    WindowsAdapter,
    _UIA_ACCEL_KEY,
    _UIA_ACCESS_KEY,
    _UIA_AUTOMATION_ID,
    _UIA_BOUNDING_RECT,
    _UIA_CACHED_PROPS,
    _UIA_CTRL_TYPE,
    _UIA_ENABLED,
    _UIA_EXPAND_STATE,
    _UIA_FOCUSED,
    _UIA_HELP_TEXT,
    _UIA_IS_SELECTED,
    _UIA_NAME,
    _UIA_RANGE_MAX,
    _UIA_RANGE_MIN,
    _UIA_RANGE_VALUE,
    _UIA_SCOPE_CHILDREN,
    _UIA_TYPE_TO_ROLE,
    _UIA_VALUE,
)
from observer.adapters.wsl import WSLAdapter

__all__ = [
    "LinuxAdapter", "MacOSAdapter", "MockAdapter", "WindowsAdapter",
    "WSLAdapter",
    "_UIA_ACCEL_KEY", "_UIA_ACCESS_KEY", "_UIA_AUTOMATION_ID",
    "_UIA_BOUNDING_RECT", "_UIA_CACHED_PROPS", "_UIA_CTRL_TYPE",
    "_UIA_ENABLED", "_UIA_EXPAND_STATE", "_UIA_FOCUSED", "_UIA_HELP_TEXT",
    "_UIA_IS_SELECTED", "_UIA_NAME", "_UIA_RANGE_MAX", "_UIA_RANGE_MIN",
    "_UIA_RANGE_VALUE", "_UIA_SCOPE_CHILDREN", "_UIA_TYPE_TO_ROLE",
    "_UIA_VALUE",
]

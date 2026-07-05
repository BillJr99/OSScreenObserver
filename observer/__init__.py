"""
observer — Core screen observation package (package form of observer.py).

Provides a platform-aware ScreenObserver that exposes a uniform interface
for: enumerating windows, walking the accessibility element tree, capturing
screenshots, and dispatching input actions. Platform adapters (Windows/macOS/
Linux/WSL/Mock) share a common protocol and are selected automatically at
runtime.

Data model
----------
  Bounds       — screen-coordinate bounding rectangle
  UIElement    — one node of the accessibility tree
  WindowInfo   — top-level window metadata

P3 decomposition: the implementation now lives in submodules (models,
platform_info, adapters/, core, activation, occlusion).  This __init__
re-exports the entire pre-split public surface so `import observer` /
`from observer import X` keep working unchanged.
"""

from __future__ import annotations

from observer.adapters import (
    LinuxAdapter,
    MacOSAdapter,
    MockAdapter,
    WindowsAdapter,
    WSLAdapter,
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
from observer.activation import ActivationMixin
from observer.core import ScreenObserver
from observer.models import (
    Bounds,
    UIElement,
    WindowInfo,
    WindowResolution,
    find_element_by_path,
    prune_tree_depth,
)
from observer.occlusion import OcclusionMixin, _intersect_bounds, _subtract_rect
from observer.platform_info import EFFECTIVE_PLATFORM, IS_WSL, PLATFORM, _is_wsl

__all__ = [
    # models
    "Bounds", "UIElement", "WindowInfo", "WindowResolution",
    "find_element_by_path", "prune_tree_depth",
    # platform detection
    "EFFECTIVE_PLATFORM", "IS_WSL", "PLATFORM", "_is_wsl",
    # adapters
    "LinuxAdapter", "MacOSAdapter", "MockAdapter", "WindowsAdapter",
    "WSLAdapter",
    # UIA constants
    "_UIA_ACCEL_KEY", "_UIA_ACCESS_KEY", "_UIA_AUTOMATION_ID",
    "_UIA_BOUNDING_RECT", "_UIA_CACHED_PROPS", "_UIA_CTRL_TYPE",
    "_UIA_ENABLED", "_UIA_EXPAND_STATE", "_UIA_FOCUSED", "_UIA_HELP_TEXT",
    "_UIA_IS_SELECTED", "_UIA_NAME", "_UIA_RANGE_MAX", "_UIA_RANGE_MIN",
    "_UIA_RANGE_VALUE", "_UIA_SCOPE_CHILDREN", "_UIA_TYPE_TO_ROLE",
    "_UIA_VALUE",
    # core + mixins
    "ScreenObserver", "ActivationMixin", "OcclusionMixin",
    # geometry helpers
    "_intersect_bounds", "_subtract_rect",
]

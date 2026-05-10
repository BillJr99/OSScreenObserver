"""
budgets.py — Per-process budget enforcement (design doc §16).

Limits supported:
  max_actions, max_screenshots, max_vlm_tokens, max_session_seconds,
  actions_per_minute (sliding window).

Plumbed into tools.dispatch via session.budgets.note() (post-call accounting)
and via session.budgets.gate() (pre-call check, returning an error_dict if
the budget is exhausted).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional

from errors import Code, error_dict

logger = logging.getLogger(__name__)


_INPUT_TOOLS = {
    "click_at", "type_text", "press_key", "scroll", "bring_to_foreground",
    "click_element", "focus_element", "set_value", "invoke_element",
    "select_option", "hover_at", "hover_element",
    "right_click_at", "right_click_element",
    "double_click_at", "double_click_element",
    "drag", "key_into_element", "clear_text",
    "click_element_and_observe", "type_and_observe", "press_key_and_observe",
}

_SCREENSHOT_TOOLS = {
    "get_screenshot", "get_screenshot_cropped", "get_full_screenshot",
}


@dataclass
class _Limit:
    used: int = 0
    limit: Optional[int] = None

    def is_set(self) -> bool:
        return self.limit is not None

    def remaining(self) -> Optional[int]:
        if not self.is_set():
            return None
        return max(0, self.limit - self.used)

    def trip(self) -> bool:
        return self.is_set() and self.used >= self.limit


class BudgetStore:
    def __init__(self, *, max_actions: Optional[int] = None,
                 max_screenshots: Optional[int] = None,
                 max_vlm_tokens: Optional[int] = None,
                 max_session_seconds: Optional[int] = None,
                 actions_per_minute: Optional[int] = None) -> None:
        self.lock = threading.Lock()
        self.actions       = _Limit(limit=max_actions)
        self.screenshots   = _Limit(limit=max_screenshots)
        self.vlm_tokens    = _Limit(limit=max_vlm_tokens)
        self.session_secs  = _Limit(limit=max_session_seconds)
        self.actions_per_minute = actions_per_minute
        self.action_window: Deque[float] = deque()
        self.started_at = time.time()

    @classmethod
    def from_args(cls, args: Any) -> Optional["BudgetStore"]:
        kwargs = {}
        for a, b in (
            ("max_actions",         "max_actions"),
            ("max_screenshots",     "max_screenshots"),
            ("max_vlm_tokens",      "max_vlm_tokens"),
            ("max_session_seconds", "max_session_seconds"),
            ("actions_per_minute",  "actions_per_minute"),
        ):
            v = getattr(args, a, None)
            if v is not None:
                kwargs[b] = v
        if not kwargs:
            return None
        return cls(**kwargs)

    def gate(self, tool: str) -> Optional[Dict[str, Any]]:
        """Pre-call check.  Returns None if allowed, error_dict if blocked."""
        with self.lock:
            now = time.time()
            elapsed = now - self.started_at
            if self.session_secs.is_set() and elapsed >= self.session_secs.limit:
                return error_dict(Code.BUDGET_EXCEEDED,
                                  "max_session_seconds reached",
                                  elapsed=int(elapsed))
            if tool in _INPUT_TOOLS:
                if self.actions.trip():
                    return error_dict(Code.BUDGET_EXCEEDED,
                                      "max_actions reached",
                                      used=self.actions.used)
                if self.actions_per_minute is not None:
                    cutoff = now - 60.0
                    while self.action_window and self.action_window[0] < cutoff:
                        self.action_window.popleft()
                    if len(self.action_window) >= self.actions_per_minute:
                        return error_dict(Code.RATE_LIMITED,
                                          "actions_per_minute exceeded",
                                          window=len(self.action_window))
            if tool in _SCREENSHOT_TOOLS and self.screenshots.trip():
                return error_dict(Code.BUDGET_EXCEEDED,
                                  "max_screenshots reached")
            return None

    def note(self, tool: str, result: Dict[str, Any]) -> None:
        with self.lock:
            now = time.time()
            if tool in _INPUT_TOOLS and result.get("ok"):
                self.actions.used += 1
                if self.actions_per_minute is not None:
                    self.action_window.append(now)
            if tool in _SCREENSHOT_TOOLS and result.get("ok"):
                self.screenshots.used += 1
            if tool == "get_screen_description" and result.get("effective_mode") == "vlm":
                # Approximate token count by description length / 4
                desc = result.get("description") or ""
                approx = max(1, len(desc) // 4)
                self.vlm_tokens.used += approx

    def status(self) -> Dict[str, Any]:
        with self.lock:
            now = time.time()
            elapsed = now - self.started_at
            cutoff = now - 60.0
            in_window = len([t for t in self.action_window if t >= cutoff])
            return {
                "actions": {"used": self.actions.used, "limit": self.actions.limit,
                            "remaining": self.actions.remaining()},
                "screenshots": {"used": self.screenshots.used,
                                "limit": self.screenshots.limit,
                                "remaining": self.screenshots.remaining()},
                "vlm_tokens": {"used": self.vlm_tokens.used,
                               "limit": self.vlm_tokens.limit,
                               "remaining": self.vlm_tokens.remaining()},
                "session_seconds": {"elapsed": int(elapsed),
                                    "limit": self.session_secs.limit,
                                    "remaining": (None if self.session_secs.limit is None
                                                   else max(0, int(self.session_secs.limit - elapsed)))},
                "actions_per_minute": {"in_window": in_window,
                                        "limit": self.actions_per_minute,
                                        "remaining": (None if self.actions_per_minute is None
                                                       else max(0, self.actions_per_minute - in_window))},
            }

    def summary(self) -> Dict[str, Any]:
        return {k: v for k, v in {
            "max_actions": self.actions.limit,
            "max_screenshots": self.screenshots.limit,
            "max_vlm_tokens": self.vlm_tokens.limit,
            "max_session_seconds": self.session_secs.limit,
            "actions_per_minute": self.actions_per_minute,
        }.items() if v is not None}

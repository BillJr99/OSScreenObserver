"""
errors.py — Structured error taxonomy.

Every tool returns either a success dict (with ok=True, success=True) or an
error dict produced by Error.to_dict().  See agentic_features_design.md §22.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ─── Error codes ──────────────────────────────────────────────────────────────

class Code:
    ELEMENT_NOT_FOUND       = "ElementNotFound"
    ELEMENT_OCCLUDED        = "ElementOccluded"
    ELEMENT_DISABLED        = "ElementDisabled"
    WINDOW_GONE             = "WindowGone"
    WINDOW_OCCLUDED         = "WindowOccluded"
    TIMEOUT                 = "Timeout"
    PATTERN_UNSUPPORTED     = "PatternUnsupported"
    RATE_LIMITED            = "RateLimited"
    BUDGET_EXCEEDED         = "BudgetExceeded"
    PERMISSION_DENIED       = "PermissionDenied"
    CONFIRMATION_REQUIRED   = "ConfirmationRequired"
    CONFIRMATION_INVALID    = "ConfirmationInvalid"
    SNAPSHOT_EXPIRED        = "SnapshotExpired"
    SCENARIO_INVALID        = "ScenarioInvalid"
    PLATFORM_UNSUPPORTED    = "PlatformUnsupported"
    PREDICATE_UNSUPPORTED   = "PredicateUnsupported"
    BAD_REQUEST             = "BadRequest"
    INTERNAL                = "Internal"


_RECOVERABLE = {
    Code.ELEMENT_NOT_FOUND:     ("find_element",        True),
    Code.ELEMENT_OCCLUDED:      ("bring_to_foreground", True),
    Code.ELEMENT_DISABLED:      ("wait_for",            True),
    Code.WINDOW_GONE:           ("list_windows",        True),
    Code.WINDOW_OCCLUDED:       ("bring_to_foreground", True),
    Code.TIMEOUT:               (None,                  True),
    Code.PATTERN_UNSUPPORTED:   ("click_element",       True),
    Code.RATE_LIMITED:          ("get_budget_status",   True),
    Code.CONFIRMATION_REQUIRED: ("propose_action",      True),
    Code.CONFIRMATION_INVALID:  ("propose_action",      True),
    Code.SNAPSHOT_EXPIRED:      (None,                  True),

    Code.BUDGET_EXCEEDED:       (None,                  False),
    Code.PERMISSION_DENIED:     (None,                  False),
    Code.SCENARIO_INVALID:      (None,                  False),
    Code.PLATFORM_UNSUPPORTED:  ("get_capabilities",    False),
    Code.PREDICATE_UNSUPPORTED: (None,                  False),
    Code.BAD_REQUEST:           (None,                  False),
    Code.INTERNAL:              (None,                  False),
}


# ─── HTTP status mapping ──────────────────────────────────────────────────────

_HTTP_STATUS = {
    Code.ELEMENT_NOT_FOUND:     404,
    Code.WINDOW_GONE:           404,
    Code.SNAPSHOT_EXPIRED:      410,
    Code.PERMISSION_DENIED:     403,
    Code.CONFIRMATION_REQUIRED: 412,
    Code.CONFIRMATION_INVALID:  412,
    Code.RATE_LIMITED:          429,
    Code.BUDGET_EXCEEDED:       429,
    Code.BAD_REQUEST:           400,
    Code.SCENARIO_INVALID:      400,
    Code.PLATFORM_UNSUPPORTED:  501,
    Code.PREDICATE_UNSUPPORTED: 501,
    Code.PATTERN_UNSUPPORTED:   501,
    Code.TIMEOUT:               408,
    Code.ELEMENT_OCCLUDED:      409,
    Code.ELEMENT_DISABLED:      409,
    Code.WINDOW_OCCLUDED:       409,
    Code.INTERNAL:              500,
}


def http_status_for(code: str) -> int:
    return _HTTP_STATUS.get(code, 500)


# ─── Error class ──────────────────────────────────────────────────────────────

@dataclass
class Error:
    code: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)

    @property
    def recoverable(self) -> bool:
        return _RECOVERABLE.get(self.code, (None, False))[1]

    @property
    def suggested_next_tool(self) -> Optional[str]:
        return _RECOVERABLE.get(self.code, (None, False))[0]

    def to_dict(self, step_id: Optional[int] = None) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ok": False,
            "success": False,  # legacy, see design doc D5
            "error": {
                "code": self.code,
                "message": self.message,
                "recoverable": self.recoverable,
                "suggested_next_tool": self.suggested_next_tool,
                "context": self.context,
            },
        }
        if step_id is not None:
            d["step_id"] = step_id
        return d


def error_dict(code: str, message: str, *, step_id: Optional[int] = None,
               **context: Any) -> Dict[str, Any]:
    """Convenience wrapper.  Filters None-valued context keys."""
    ctx = {k: v for k, v in context.items() if v is not None}
    return Error(code=code, message=message, context=ctx).to_dict(step_id=step_id)


# ─── Translate legacy result dicts into the new shape ────────────────────────

def annotate_legacy_result(result: Dict[str, Any], step_id: int,
                           caused_by_step_id: Optional[int]) -> Dict[str, Any]:
    """
    Take a result dict from one of today's tool handlers (which may use
    success/error string conventions) and add the new fields without removing
    the old ones, per design doc D5.
    """
    out = dict(result) if isinstance(result, dict) else {"value": result}
    out.setdefault("step_id", step_id)
    out.setdefault("caused_by_step_id", caused_by_step_id)

    if "ok" not in out:
        if "error" in out and isinstance(out["error"], str):
            out["ok"] = False
            out["error"] = {
                "code": Code.INTERNAL,
                "message": out["error"],
                "recoverable": False,
                "suggested_next_tool": None,
                "context": {},
            }
            out.setdefault("success", False)
        elif out.get("success") is False:
            out["ok"] = False
        else:
            out["ok"] = True
            out.setdefault("success", True)
    return out

"""
audit.py — Append-only audit log (design doc §20).

One human-readable line per tool call.  Off by default; enable via
config.logging.audit = true.  Path defaults to ./audit.log.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_REDACT_KEYS = ["text", "value", "password", "api_key",
                        "Authorization", "confirm_token"]


class AuditLogger:
    def __init__(self, *, path: str, max_bytes: int, backups: int,
                 redact_keys: list) -> None:
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.handler = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.redact_keys = list(redact_keys)

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> Optional["AuditLogger"]:
        log_cfg = config.get("logging") or {}
        if not log_cfg.get("audit", False):
            return None
        audit_cfg = config.get("audit") or {}
        path = log_cfg.get("audit_path", "./audit.log")
        max_bytes = int(log_cfg.get("audit_max_bytes", 10 * 1024 * 1024))
        backups = int(log_cfg.get("audit_backups", 3))
        redact_keys = audit_cfg.get("redact_arg_keys", _DEFAULT_REDACT_KEYS)
        return cls(path=path, max_bytes=max_bytes, backups=backups,
                   redact_keys=redact_keys)

    def record(self, *, tool: str, caller: str, args: Dict[str, Any],
               result: Dict[str, Any]) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        step_id = result.get("step_id", "?")
        ok = "true" if result.get("ok", True) else "false"
        changed = result.get("changed")
        redactions: list = []
        arg_pieces: list = []
        for k, v in (args or {}).items():
            if k in self.redact_keys:
                redactions.append(f"args.{k}")
                continue
            if k.startswith("_"):
                continue
            arg_pieces.append(f"args.{k}={_short(v)}")
        if redactions:
            arg_pieces.append("redactions=[" + ",".join(redactions) + "]")
        line_parts = [ts, f"step={step_id}", f"caller={caller}",
                      f"tool={tool}", f"ok={ok}"]
        if changed is not None:
            line_parts.append(f"changed={'true' if changed else 'false'}")
        line_parts.extend(arg_pieces)
        line = " ".join(line_parts)
        rec = logging.LogRecord(
            name="audit", level=logging.INFO, pathname="", lineno=0,
            msg=line, args=None, exc_info=None,
        )
        with self.lock:
            try:
                self.handler.emit(rec)
                self.handler.flush()
            except Exception:
                logger.exception("audit emit failed")


def _short(v: Any) -> str:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, separators=(",", ":"), default=str)
    else:
        s = str(v)
    if len(s) > 200:
        s = s[:200] + "…"
    return s

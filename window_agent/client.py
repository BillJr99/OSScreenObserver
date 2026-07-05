"""
ANSI colour, HTTP and REST/LLM client helpers.

Split out of window_agent.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# ─── ANSI colour helpers ──────────────────────────────────────────────────────

_NO_COLOR = not sys.stdout.isatty()

def _c(text: str, *codes: str) -> str:
    if _NO_COLOR:
        return text
    _MAP = {
        "bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
        "white": "\033[37m", "bright_white": "\033[97m",
    }
    return "".join(_MAP.get(c, "") for c in codes) + text + "\033[0m"

# ─── HTTP helpers (stdlib only, no extra deps) ────────────────────────────────

def _get(base: str, path: str, params: Optional[Dict] = None, timeout: int = 30) -> Any:
    url = base.rstrip("/") + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urllib.parse.urlencode(filtered)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise on any redirect so a POST is never silently converted to GET."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code,
                                     f"Redirect to {newurl} — update your base URL",
                                     headers, fp)

_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _post(base: str, path: str, data: Any, headers: Optional[Dict] = None,
          timeout: int = 60) -> Any:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

# ─── REST server poller ───────────────────────────────────────────────────────

def wait_for_server(rest_base: str, retries: int = 40, delay: float = 0.5) -> bool:
    print(_c("  Waiting for REST server …", "dim"), end="", flush=True)
    for _ in range(retries):
        try:
            _get(rest_base, "/api/windows", timeout=2)
            print(_c(" ready.", "green"))
            return True
        except Exception:
            print(".", end="", flush=True)
            time.sleep(delay)
    print(_c(" timed out.", "red"))
    return False

# ─── REST API wrappers ────────────────────────────────────────────────────────

def _win_params(uid: Optional[str], index: Optional[int],
                title: Optional[str] = None) -> Dict[str, Any]:
    """Return the minimal window-selector dict: uid > index > title substring."""
    if uid:
        return {"window_uid": uid}
    if index is not None:
        return {"window_index": index}
    if title:
        return {"window_title": title}
    return {}


def api_list_windows(rest: str) -> Dict:
    return _get(rest, "/api/windows")


def api_observe(rest: str, uid: Optional[str], index: Optional[int],
                title: Optional[str] = None) -> Dict:
    params = _win_params(uid, index, title)
    sketch = _get(rest, "/api/sketch", params)
    desc   = _get(rest, "/api/description", params)
    return {
        "window":      sketch.get("window", "unknown"),
        "sketch":      sketch.get("sketch", ""),
        "description": desc.get("description", ""),
    }


def api_element_tree(rest: str, uid: Optional[str], index: Optional[int],
                     title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/structure", _win_params(uid, index, title))


def api_description(rest: str, uid: Optional[str], index: Optional[int],
                    title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/description", _win_params(uid, index, title))


def api_sketch(rest: str, uid: Optional[str], index: Optional[int],
               grid_width: Optional[int] = None, grid_height: Optional[int] = None,
               ocr: bool = False, title: Optional[str] = None) -> Dict:
    params: Dict[str, Any] = _win_params(uid, index, title)
    if grid_width is not None:
        params["grid_width"] = grid_width
    if grid_height is not None:
        params["grid_height"] = grid_height
    if ocr:
        params["ocr"] = "1"
    return _get(rest, "/api/sketch", params)


def api_screenshot(rest: str, uid: Optional[str], index: Optional[int],
                   title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/screenshot", _win_params(uid, index, title))


def api_full_screenshot(rest: str, uid: Optional[str], index: Optional[int],
                        grid_width: Optional[int] = None,
                        grid_height: Optional[int] = None,
                        title: Optional[str] = None) -> Dict:
    params: Dict[str, Any] = _win_params(uid, index, title)
    if grid_width is not None:
        params["grid_width"] = grid_width
    if grid_height is not None:
        params["grid_height"] = grid_height
    return _get(rest, "/api/full_screenshot", params)


def api_visible_areas(rest: str, uid: Optional[str], index: Optional[int],
                      title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/visible_areas", _win_params(uid, index, title))


def api_bring_to_foreground(rest: str, uid: Optional[str], index: Optional[int],
                            title: Optional[str] = None) -> Dict:
    return _get(rest, "/api/bring_to_foreground", _win_params(uid, index, title))


def api_action(rest: str, payload: Dict) -> Dict:
    return _post(rest, "/api/action", payload)

# OpenWebUI OpenAI-compatible API prefix (centralised so both endpoints stay in sync)
_OWU_PREFIX = "/api/v1"

# ─── LLM client (OpenAI-compatible, OpenWebUI) ────────────────────────────────

class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.model    = model

    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict:
        payload: Dict[str, Any] = {
            "model":       self.model,
            "messages":    messages,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return _post(self.base_url, f"{_OWU_PREFIX}/chat/completions", payload, headers, timeout=240)

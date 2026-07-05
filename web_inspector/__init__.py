"""
web_inspector — Human-facing inspection interface (package form of the
former web_inspector.py).

Exposes a Flask HTTP server on localhost:5001 (configurable) with:

  GET  /                   — Single-page inspection UI
  GET  /api/windows        — List windows
  GET  /api/structure      — Accessibility tree JSON
  GET  /api/description    — Textual description (combined: accessibility + OCR + VLM)
  GET  /api/sketch         — ASCII layout sketch
  GET  /api/screenshot     — Screenshot as base64 PNG
  POST /api/action         — Execute an input action

The HTML/CSS/JS is inlined as a template string (assets.py) so the entire
server is importable with no external static files.

CORS policy: no CORS headers are emitted unless web_ui.cors_origins is set
in config (default [] = same-origin only). See create_web_app().

P3 decomposition: the implementation now lives in submodules (assets,
views, server).  This __init__ re-exports the pre-split surface so
`from web_inspector import create_web_app` keeps working unchanged.
"""

from __future__ import annotations

from web_inspector.assets import _HTML
from web_inspector.server import create_web_app
from web_inspector.views import register_routes

__all__ = ["_HTML", "create_web_app", "register_routes"]

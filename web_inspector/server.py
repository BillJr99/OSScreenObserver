"""
Flask application factory for the inspection server.

Split out of web_inspector.py (P3); behavior is unchanged.
"""

from __future__ import annotations

import logging

from flask import Flask
from flask_cors import CORS

from ascii_renderer import ASCIIRenderer
from description import DescriptionGenerator
from observer import ScreenObserver
import tools as _tools

from web_inspector.views import register_routes

logger = logging.getLogger(__name__)


def create_web_app(
    observer:  ScreenObserver,
    renderer:  ASCIIRenderer,
    describer: DescriptionGenerator,
    config:    dict,
) -> Flask:
    """
    Create and configure the Flask inspection application.

    All routes are defined inside this factory so they close over the
    shared observer/renderer/describer instances.
    """
    app = Flask(__name__)

    # CORS is opt-in (web_ui.cors_origins, default []).  With the default no
    # Access-Control-Allow-Origin header is ever sent, so browsers enforce
    # same-origin — the bundled inspector UI at "/" is served same-origin and
    # keeps working.  Operators can list explicit origins, or ["*"] for
    # Docker/testing scenarios where cross-origin dashboards need access.
    # Never enable "*" together with a non-loopback bind outside an isolated
    # environment: /api/action is unauthenticated and destructive.
    cors_origins = list((config.get("web_ui") or {}).get("cors_origins") or [])
    if cors_origins:
        CORS(app, origins=cors_origins)
        logger.warning(f"CORS enabled for origins: {cors_origins}")

    ctx = _tools.ToolContext(observer=observer, renderer=renderer,
                              describer=describer, config=config)

    register_routes(app, observer=observer, renderer=renderer,
                    describer=describer, config=config, ctx=ctx)
    return app

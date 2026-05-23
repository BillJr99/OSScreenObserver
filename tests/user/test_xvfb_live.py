"""
Live X11 tests against a real Xvfb display.

These boot OSO WITHOUT --mock so the linux_adapter takes over, then spawn
an xterm via the xterm_window fixture and verify the adapter picks the
window up.

Skipped when no display is reachable.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.user, pytest.mark.needs_display]


def test_live_list_windows_finds_xterm(oso_server_factory, xterm_window, has_display):
    if not has_display:
        pytest.skip("DISPLAY not set or xdpyinfo failed")
    # Bring up OSO without --mock so it talks to the live X server.
    srv = oso_server_factory(mock=False)
    from tests.user.conftest import HttpJson
    http = HttpJson(srv["base_url"])
    _, body = http.get("/api/windows")
    assert body["ok"] is True
    titles = [w["title"] for w in body["windows"]]
    assert any(xterm_window["title"] in t for t in titles), \
        f"{xterm_window['title']!r} not found in {titles!r}"


def test_live_screenshot_returns_png_data(oso_server_factory, xterm_window, has_display):
    if not has_display:
        pytest.skip("DISPLAY not set or xdpyinfo failed")
    srv = oso_server_factory(mock=False)
    from tests.user.conftest import HttpJson
    http = HttpJson(srv["base_url"])
    _, body = http.get("/api/screenshot", {"window_index": 0})
    assert body["encoding"] == "base64"
    assert len(body["data"]) > 100

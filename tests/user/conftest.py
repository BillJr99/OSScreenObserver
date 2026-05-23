"""
OSScreenObserver user-test fixtures.

These fixtures spin up real `python main.py` subprocesses (mock adapter
by default) and yield handles that the test files can drive. The goal is
to exercise the wire format, not the in-process function calls — that's
what tests/conftest.py already does.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout: float = 15.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _kill_proc(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=5.0)
            return
        except subprocess.TimeoutExpired:
            pass
        p.kill()
        p.wait(timeout=2.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Subprocess fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def oso_server_factory(tmp_path_factory):
    """Factory that boots OSO subprocesses with configurable flags.

    Tests call ``oso_server_factory(extra_args=[...])`` to get a fresh
    OSScreenObserver server with their own flags. The factory tracks all
    spawned children and kills them on module teardown.
    """
    spawned: list[subprocess.Popen] = []

    def _spawn(extra_args: list[str] | None = None,
               config_overrides: dict | None = None,
               mock: bool = True,
               mode: str = "inspect") -> dict:
        port = _free_port()
        cwd = tmp_path_factory.mktemp("oso_cwd")
        cfg_path = cwd / "config.json"
        if config_overrides is not None:
            cfg_path.write_text(json.dumps(config_overrides))
        argv: list[str] = [
            sys.executable, str(ROOT / "main.py"),
            "--mode", mode,
            "--port", str(port),
            "--config", str(cfg_path) if cfg_path.exists() else "config.json",
        ]
        if mock:
            argv.append("--mock")
        if extra_args:
            argv.extend(extra_args)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        # Force a TTY-less stdin so the auto-mode picker chooses correctly.
        stderr_log = cwd / "stderr.log"
        # MCP mode needs a writable stdin (we drive it via framed JSON-RPC).
        # Other modes don't read stdin; we still give them a PIPE so the
        # subprocess never blocks on an unexpected isatty probe.
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if mode == "mcp" else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_log.open("wb"),
        )
        spawned.append(proc)
        base_url = f"http://127.0.0.1:{port}"
        # For inspect/both modes the Flask server must be up before we yield.
        if mode in ("inspect", "both"):
            if not _wait_for_http(f"{base_url}/api/healthz"):
                proc.terminate()
                proc.wait(timeout=5)
                raise RuntimeError(
                    f"OSScreenObserver did not become healthy. "
                    f"stderr:\n{stderr_log.read_text(errors='replace')}"
                )
        return {"proc": proc, "base_url": base_url, "port": port,
                "cwd": cwd, "stderr_log": stderr_log}

    yield _spawn

    for p in spawned:
        _kill_proc(p)


@pytest.fixture
def oso_server(oso_server_factory):
    """A default OSO server with mock adapter on a free port."""
    return oso_server_factory()


@pytest.fixture
def oso_mcp_server(oso_server_factory):
    """An OSO server running in MCP stdio mode (no HTTP)."""
    return oso_server_factory(mode="mcp")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

class HttpJson:
    """Tiny urllib-based JSON HTTP client used by the user tests.

    Keeping the dependency surface minimal — Flask's test client is fine
    for in-process tests but we want to drive a *real* spawned subprocess
    here, so we go over the loopback socket.
    """

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, params: dict | None = None) -> tuple[int, dict]:
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        req = urllib.request.Request(url)
        return self._send(req)

    def post(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = self.base_url + path
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def delete(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(self.base_url + path, method="DELETE")
        return self._send(req)

    def get_text(self, path: str, params: dict | None = None) -> tuple[int, str]:
        """Like get(), but returns the raw body as text (for Prometheus etc.)."""
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, (e.read() or b"").decode(errors="replace")

    def _send(self, req) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return r.status, {"_raw": raw.decode(errors="replace")}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except Exception:
                payload = {"_error": str(e)}
            return e.code, payload


@pytest.fixture
def http(oso_server):
    return HttpJson(oso_server["base_url"])


# ---------------------------------------------------------------------------
# MCP framing helper
# ---------------------------------------------------------------------------

class MCPClient:
    """Drives an OSScreenObserver MCP server over its stdio framing channel.

    OSScreenObserver's mcp_server.py uses newline-delimited JSON-RPC 2.0
    (one JSON object per line on each direction). That's simpler than the
    LSP Content-Length framing some MCP servers use.
    """

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._next_id = 0

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None, "MCP server stdin closed"
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read_line(self, timeout: float = 10.0) -> dict:
        """Read one NDJSON line from the server."""
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        buf = b""
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("MCP read line timeout")
            chunk = self.proc.stdout.read(1)
            if not chunk:
                raise RuntimeError("MCP stdout closed unexpectedly")
            if chunk == b"\n":
                if not buf:
                    continue
                return json.loads(buf.decode("utf-8"))
            buf += chunk

    def request(self, method: str, params: dict | None = None,
                timeout: float = 10.0) -> dict:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        while True:
            r = self._read_line(timeout=timeout)
            if r.get("id") == self._next_id:
                return r


@pytest.fixture
def mcp(oso_mcp_server):
    """Live MCP client wired to a freshly-spawned OSO --mode mcp server."""
    return MCPClient(oso_mcp_server["proc"])


# ---------------------------------------------------------------------------
# Image / OCR helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def text_image_bytes():
    """Render a known string into a PNG (white bg, large dark text).

    Used by the OCR tests to confirm Tesseract recognises text put on
    the OSO /api/ocr endpoint. Returns a function taking (text, size).
    """
    from PIL import Image, ImageDraw, ImageFont

    def _render(text: str, size: tuple[int, int] = (480, 120)) -> bytes:
        img = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(img)
        # PIL falls back to a built-in bitmap font when no TTF is loaded.
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 36)
        except OSError:
            font = ImageFont.load_default()
        draw.text((20, 30), text, fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    return _render


@pytest.fixture
def tesseract_available():
    return shutil.which("tesseract") is not None


# ---------------------------------------------------------------------------
# Display + Ollama probes
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def has_display():
    """True if $DISPLAY is set and xdpyinfo can probe it."""
    if not os.environ.get("DISPLAY"):
        return False
    return shutil.which("xdpyinfo") is not None and \
        subprocess.run(["xdpyinfo"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL).returncode == 0


@pytest.fixture(scope="session")
def ollama_base_url():
    """Returns the URL of a reachable Ollama (or compatible) server, else None."""
    candidates = [
        os.environ.get("AUTOGUI_LLM_BASE_URL"),
        os.environ.get("OLLAMA_BASE_URL"),
        "http://127.0.0.1:11434",
    ]
    for url in candidates:
        if not url:
            continue
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=1.5) as r:
                if r.status == 200:
                    return url.rstrip("/")
        except Exception:
            continue
    return None


@pytest.fixture(scope="session")
def vlm_model():
    return os.environ.get("AUTOGUI_VLM_MODEL", "qwen2.5vl:3b")


@pytest.fixture(scope="session")
def chat_model():
    return os.environ.get("AUTOGUI_LLM_MODEL", "qwen2.5:0.5b")


@pytest.fixture
def xterm_window():
    """Spawn an xterm window and yield its title. Skips if no display."""
    if not os.environ.get("DISPLAY"):
        pytest.skip("DISPLAY not set; cannot spawn xterm")
    if not shutil.which("xterm"):
        pytest.skip("xterm not installed")
    title = f"user-test-{os.getpid()}-{int(time.time()*1000) % 100000}"
    # xterm -e holds the window open by running a slow command.
    proc = subprocess.Popen(
        ["xterm", "-T", title, "-geometry", "60x10", "-e",
         "bash", "-c", "echo USERTEST-VISIBLE-TEXT; sleep 60"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for the window to actually exist by polling wmctrl.
    if shutil.which("wmctrl"):
        for _ in range(50):
            r = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True)
            if title in (r.stdout or ""):
                break
            time.sleep(0.1)
    else:
        time.sleep(1.5)
    try:
        yield {"title": title, "proc": proc}
    finally:
        _kill_proc(proc)

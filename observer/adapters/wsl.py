"""
WSL adapter (WSL 1 + WSL 2: X11 when DISPLAY is set,
PowerShell interop fallback).

Split out of observer.py (P3); behavior is unchanged.
"""

import logging
import os
from typing import List, Optional

from observer.adapters.linux import LinuxAdapter
from observer.models import Bounds, WindowInfo

logger = logging.getLogger(__name__)


class WSLAdapter(LinuxAdapter):
    """Adapter for Windows Subsystem for Linux.

    Prefers X11-based tools (wmctrl, mss) when DISPLAY is set.  Falls back to
    PowerShell / cmd.exe interop, which is always available in both WSL 1 and
    WSL 2 via the Windows binary execution layer.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self._has_display = bool(os.environ.get("DISPLAY"))
        logger.info(
            "[WSLAdapter:__init__] WSL detected; "
            "DISPLAY=%s", "set" if self._has_display else "not set (PowerShell fallback active)",
        )

    # ── Window listing ────────────────────────────────────────────────────────

    def list_windows(self) -> List[WindowInfo]:
        if self._has_display:
            result = LinuxAdapter.list_windows(self)
            if result:
                return result
        return self._list_windows_ps()

    def _list_windows_ps(self) -> List[WindowInfo]:
        """Enumerate visible Windows windows via PowerShell ConvertTo-Json."""
        try:
            import json
            import subprocess
            ps = (
                "Get-Process "
                "| Where-Object { $_.MainWindowTitle -ne '' } "
                "| Select-Object Id,ProcessName,MainWindowTitle "
                "| ConvertTo-Json -Compress"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return []
            data = json.loads(r.stdout)
            if isinstance(data, dict):
                data = [data]
            results: List[WindowInfo] = []
            for i, item in enumerate(data or []):
                pid   = int(item.get("Id", 0))
                name  = str(item.get("ProcessName", "unknown"))
                title = str(item.get("MainWindowTitle", ""))
                if not title:
                    continue
                results.append(WindowInfo(
                    handle=pid, title=title, process_name=name, pid=pid,
                    bounds=Bounds(0, 0, 1920, 1080), is_focused=(i == 0),
                    window_uid=f"wsl:{pid}",
                ))
            return results
        except Exception as e:
            logger.debug("[WSLAdapter:_list_windows_ps] %s", e)
            return []

    # ── Screenshot ────────────────────────────────────────────────────────────

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        if self._has_display:
            result = LinuxAdapter.get_screenshot(self, hwnd)
            if result:
                return result
        return self._screenshot_ps()

    def _screenshot_ps(self) -> Optional[bytes]:
        """Capture the primary screen via PowerShell, returning PNG bytes."""
        try:
            import base64
            import subprocess
            # Capture screen to a MemoryStream and emit as base64 — avoids
            # WSL↔Windows path translation issues entirely.
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                "$ms=New-Object System.IO.MemoryStream;"
                "$bmp.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png);"
                "$g.Dispose();$bmp.Dispose();"
                "[Convert]::ToBase64String($ms.ToArray())"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0 and r.stdout.strip():
                return base64.b64decode(r.stdout.strip())
        except Exception as e:
            logger.debug("[WSLAdapter:_screenshot_ps] %s", e)
        return None

    # ── get_windows_above_bounds: returns [] (inherited from LinuxAdapter) ────
    # ── get_element_tree: upgraded by linux_adapter.install_into if pyatspi ──
    # ── perform_action: inherited (pyautogui; needs DISPLAY) ─────────────────

"""
Mock adapter (no OS dependencies; safe anywhere).

Split out of observer.py (P3); behavior is unchanged.
"""

import io
import traceback
from typing import Any, Callable, Dict, List, Optional

from observer.models import (
    Bounds, UIElement, WindowInfo, find_element_by_path, prune_tree_depth,
)


class MockAdapter:
    """Synthetic data adapter for development and testing."""

    def __init__(self) -> None:
        import secrets as _s
        self._nonce = _s.token_hex(4)
        # Optional scenario hook (design doc §15.5).  Set by main.py when
        # --scenario is supplied; methods route through the scenario when
        # active so that input actions can drive state transitions.
        self.scenario: Optional[Any] = None
        # Test hooks (P1 perf work): capture_count increments on every tree
        # walk so tests can assert cache hits avoided adapter work;
        # tree_mutator, when set, post-processes (or replaces) each captured
        # tree so tests can simulate UI changes between captures.
        self.capture_count: int = 0
        self.tree_mutator: Optional[Callable[[UIElement], UIElement]] = None

    def get_windows_above_bounds(self, hwnd) -> List["Bounds"]:
        return []  # Mock assumes the target window is on top

    def list_windows(self) -> List[WindowInfo]:
        if self.scenario is not None:
            return self.scenario.list_windows(self._nonce)
        return [
            WindowInfo(1001, "Untitled — Notepad", "notepad.exe", 1234,
                       Bounds(80, 60, 800, 600), True,
                       window_uid=f"mock:0:{self._nonce}"),
            WindowInfo(1002, "GitHub · Where software is built — Google Chrome",
                       "chrome.exe", 5678, Bounds(0, 0, 1920, 1050), False,
                       window_uid=f"mock:1:{self._nonce}"),
            WindowInfo(1003, "screen_observer.py — Visual Studio Code",
                       "code.exe", 9012, Bounds(960, 0, 960, 1050), False,
                       window_uid=f"mock:2:{self._nonce}"),
        ]

    def get_element_tree(self, hwnd=None) -> Optional[UIElement]:
        self.capture_count += 1
        tree = self._build_tree(hwnd)
        if tree is not None and self.tree_mutator is not None:
            mutated = self.tree_mutator(tree)
            if mutated is not None:
                tree = mutated
        return tree

    def get_element_subtree(self, hwnd=None, element_path: str = "root",
                            max_depth: Optional[int] = None
                            ) -> Optional[UIElement]:
        """Walk only the subtree rooted at *element_path*, to *max_depth*
        levels below it.  The mock world is synthetic, so this navigates the
        positional element-id path of a fresh capture."""
        tree = self.get_element_tree(hwnd)
        sub = find_element_by_path(tree, element_path)
        return prune_tree_depth(sub, max_depth)

    def _build_tree(self, hwnd=None) -> Optional[UIElement]:
        if self.scenario is not None:
            return self.scenario.get_element_tree(hwnd)
        root = UIElement("root", "Untitled — Notepad", "Window",
                         bounds=Bounds(80, 60, 800, 600))

        # Menu bar
        menubar = UIElement("root.0", "MenuBar", "MenuBar",
                            bounds=Bounds(80, 60, 800, 22))
        for i, lbl in enumerate(["File", "Edit", "Format", "View", "Help"]):
            menubar.children.append(UIElement(
                f"root.0.{i}", lbl, "MenuItem",
                bounds=Bounds(80 + i * 58, 60, 56, 22)
            ))
        root.children.append(menubar)

        # Main editing area
        editor = UIElement(
            "root.1", "Text Editor", "Document",
            value="Hello, world!\nThis is a test document.\nLine 3 has some content here.\n",
            bounds=Bounds(80, 82, 800, 514), focused=True
        )
        root.children.append(editor)

        # Horizontal scroll bar
        hscroll = UIElement("root.2", "Horizontal ScrollBar", "ScrollBar",
                            bounds=Bounds(80, 596, 784, 18))
        root.children.append(hscroll)

        # Vertical scroll bar
        vscroll = UIElement("root.3", "Vertical ScrollBar", "ScrollBar",
                            bounds=Bounds(864, 82, 18, 532))
        root.children.append(vscroll)

        # Progress bar — exercises value_now/min/max for the role glyph path.
        root.children.append(UIElement(
            "root.5", "Saving", "ProgressBar",
            bounds=Bounds(560, 600, 200, 10),
            value_now=40.0, value_min=0.0, value_max=100.0,
        ))

        # Word-wrap toggle checkbox — exercises selected=True path.
        root.children.append(UIElement(
            "root.6", "Word Wrap", "CheckBox",
            bounds=Bounds(80, 636, 120, 18), selected=True,
        ))

        # Status bar
        sb = UIElement("root.4", "Status Bar", "StatusBar",
                       bounds=Bounds(80, 614, 800, 22))
        for i, (lbl, val) in enumerate([
            ("Position",  "Ln 1, Col 1"),
            ("Zoom",      "100%"),
            ("Encoding",  "UTF-8"),
            ("EOL",       "Windows (CRLF)"),
        ]):
            sb.children.append(UIElement(
                f"root.4.{i}", lbl, "Text", value=val,
                bounds=Bounds(80 + i * 190, 614, 188, 22)
            ))
        root.children.append(sb)

        return root

    def get_screenshot(self, hwnd=None) -> Optional[bytes]:
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (800, 600), "#1a1e2e")
            draw = ImageDraw.Draw(img)

            # Title bar
            draw.rectangle([0, 0, 800, 30], fill="#2d3250")
            draw.text((10, 8), "Untitled — Notepad", fill="#c8d3f5")

            # Menu bar
            draw.rectangle([0, 30, 800, 52], fill="#1e2030")
            for i, m in enumerate(["File", "Edit", "Format", "View", "Help"]):
                draw.text((10 + i * 58, 36), m, fill="#a9b1d6")

            # Editor area
            draw.rectangle([0, 52, 782, 570], fill="#1a1e2e")
            for i, ln in enumerate(["Hello, world!", "This is a test document.",
                                     "Line 3 has some content here.", ""]):
                draw.text((6, 58 + i * 18), ln, fill="#c0caf5")

            # Scrollbars
            draw.rectangle([782, 52, 800, 570], fill="#24283b")
            draw.rectangle([0, 570, 782, 586], fill="#24283b")

            # Status bar
            draw.rectangle([0, 586, 800, 600], fill="#16161e")
            draw.text((6, 589), "Ln 1, Col 1     100%     UTF-8     Windows (CRLF)",
                      fill="#565f89")

            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        except Exception as e:
            print(f"[MockAdapter:get_screenshot] {e}")
            traceback.print_exc()
            return None

    def perform_action(self, action: str, element_id: Optional[str] = None,
                       value: Any = None, hwnd=None) -> Dict:
        if self.scenario is not None:
            handled = self.scenario.handle_action(action=action,
                                                   element_id=element_id,
                                                   value=value, hwnd=hwnd)
            if handled is not None:
                return handled
        return {
            "success": True,
            "action": action,
            "note": "Mock adapter — no real OS action performed",
        }

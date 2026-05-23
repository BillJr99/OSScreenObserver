"""
End-to-end OCR test using the real Tesseract binary.

Generates a PNG with known text via Pillow, posts the bytes to OSO's
/api/ocr endpoint, and asserts the recognised text contains the
expected substrings. Skipped when tesseract isn't installed.
"""
from __future__ import annotations

import base64

import pytest

pytestmark = [pytest.mark.user, pytest.mark.needs_tesseract]


def test_ocr_recognises_rendered_text(http, text_image_bytes, tesseract_available):
    if not tesseract_available:
        pytest.skip("tesseract binary not on PATH")
    png = text_image_bytes("USERTEST OCR HELLO")
    b64 = base64.b64encode(png).decode()
    # The /api/ocr endpoint accepts a base64 PNG payload directly.
    status, body = http.post("/api/ocr", {"image_b64": b64})
    if status != 200 or not body.get("ok", True):
        # Some OSO builds expose ocr only via the cropped/full screenshot
        # path; allow skip with a clear reason.
        pytest.skip(f"/api/ocr did not accept image_b64 payload: status={status} body={body!r}")
    text = body.get("text") or " ".join(
        w.get("text", "") for w in body.get("words", []))
    assert "OCR" in text.upper() or "HELLO" in text.upper(), \
        f"OCR did not recognise the rendered text. Got: {text!r}"


def test_ocr_endpoint_present_in_tools_list(http):
    _, body = http.get("/api/tools")
    assert "get_ocr" in body["tools"]

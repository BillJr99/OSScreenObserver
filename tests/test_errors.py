"""Tests for the error taxonomy."""
from __future__ import annotations

from errors import Code, Error, annotate_legacy_result, error_dict, http_status_for


def test_recoverable_table():
    e = Error(code=Code.ELEMENT_NOT_FOUND, message="x")
    assert e.recoverable is True
    assert e.suggested_next_tool == "find_element"

    e2 = Error(code=Code.BUDGET_EXCEEDED, message="x")
    assert e2.recoverable is False
    assert e2.suggested_next_tool is None


def test_http_status_for():
    assert http_status_for(Code.ELEMENT_NOT_FOUND) == 404
    assert http_status_for(Code.PERMISSION_DENIED) == 403
    assert http_status_for(Code.TIMEOUT) == 408
    assert http_status_for(Code.INTERNAL) == 500


def test_error_dict_shape():
    d = error_dict(Code.ELEMENT_NOT_FOUND, "no match", step_id=42, selector="X")
    assert d["ok"] is False
    assert d["success"] is False
    assert d["step_id"] == 42
    assert d["error"]["code"] == Code.ELEMENT_NOT_FOUND
    assert d["error"]["recoverable"] is True
    assert d["error"]["context"] == {"selector": "X"}


def test_annotate_legacy_success():
    legacy = {"success": True, "action": "click"}
    out = annotate_legacy_result(legacy, step_id=1, caused_by_step_id=1)
    assert out["ok"] is True
    assert out["step_id"] == 1
    assert out["success"] is True


def test_annotate_legacy_error_string():
    legacy = {"error": "boom"}
    out = annotate_legacy_result(legacy, step_id=2, caused_by_step_id=1)
    assert out["ok"] is False
    assert out["error"]["code"] == Code.INTERNAL
    assert out["error"]["message"] == "boom"

"""Tests for element_selectors."""
from __future__ import annotations

import pytest

import element_selectors as sel
from observer import Bounds, UIElement


def _tree():
    root = UIElement("r", "Login", "Window", bounds=Bounds(0, 0, 800, 600))
    user = UIElement("r.0", "Username", "Edit",
                     bounds=Bounds(10, 10, 200, 24))
    pwd = UIElement("r.1", "Password", "Edit",
                    bounds=Bounds(10, 40, 200, 24), value="hidden")
    btn1 = UIElement("r.2", "Login", "Button", bounds=Bounds(10, 70, 80, 28))
    btn2 = UIElement("r.3", "Cancel", "Button",
                     bounds=Bounds(100, 70, 80, 28), enabled=False)
    root.children = [user, pwd, btn1, btn2]
    return root


def test_xpath_basic():
    s = sel.parse('Window/Edit[name="Username"]')
    assert s.grammar == "xpath"
    assert s.steps[0].role == "Window"
    assert s.steps[1].role == "Edit"
    assert s.steps[1].predicates[0].key == "name"


def test_css_basic():
    s = sel.parse("Window > Button[name=\"Login\"]")
    assert s.grammar == "css"
    assert s.steps[0].axis == "child"
    assert s.steps[1].axis == "child"


def test_resolve_unique():
    r = sel.find(_tree(), 'Window/Edit[name="Username"]')
    assert len(r.matches) == 1
    assert r.matches[0].element_id == "r.0"
    assert r.ambiguous is False


def test_resolve_ambiguous_by_role():
    r = sel.find(_tree(), "Window/Button")
    assert len(r.matches) == 2
    assert r.ambiguous is True


def test_resolve_index_predicate():
    r = sel.find(_tree(), "Window/Button[index=1]")
    assert len(r.matches) == 1
    assert r.matches[0].name == "Cancel"


def test_resolve_focused_disabled_predicates():
    r = sel.find(_tree(), "Window/Button[enabled=false]")
    assert len(r.matches) == 1
    assert r.matches[0].name == "Cancel"


def test_resolve_regex():
    r = sel.find(_tree(), 'Window/Edit[name~="(Username|Password)"]')
    assert len(r.matches) == 2


def test_css_descendant():
    s = sel.parse("Window Button")
    assert s.steps[1].axis == "descendant"
    r = sel.resolve(_tree(), s)
    assert len(r.matches) == 2


def test_css_nth_of_type():
    r = sel.find(_tree(), "Window > Button:nth-of-type(2)")
    assert len(r.matches) == 1
    assert r.matches[0].name == "Cancel"


def test_no_match():
    r = sel.find(_tree(), 'Window/Button[name="Nope"]')
    assert r.matches == []


def test_parse_error():
    with pytest.raises(sel.SelectorParseError):
        sel.parse("")


def test_selector_for_unique_name():
    s = sel.selector_for(_tree(), "r.2")
    # The root in _tree() also has name="Login", so the selector emits the
    # full path including the root's [name] predicate.
    assert s == 'Window[name="Login"]/Button[name="Login"]'


def test_selector_for_index_fallback():
    """When elements have no name we should emit [index=N]."""
    root = UIElement("r", "", "Window", bounds=Bounds(0, 0, 100, 100))
    a = UIElement("r.0", "", "Button", bounds=Bounds(0, 0, 50, 20))
    b = UIElement("r.1", "", "Button", bounds=Bounds(0, 0, 50, 20))
    root.children = [a, b]
    s = sel.selector_for(root, "r.1")
    assert s == "Window/Button[index=1]"


def test_wildcard_role():
    r = sel.find(_tree(), "Window/*")
    assert len(r.matches) == 4

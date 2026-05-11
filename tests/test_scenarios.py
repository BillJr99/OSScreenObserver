"""Tests for the scenario loader and state machine."""
from __future__ import annotations

import pytest

from scenarios import Scenario, ScenarioError, load


def test_load_login_yaml():
    sc = load("scenarios_examples/login.yaml")
    assert sc.name == "login-happy-path"
    assert sc.initial_state == "start"
    assert "start" in sc.states
    assert "welcome" in sc.states
    assert len(sc.reactions) == 3
    # The 'on' key may have been re-keyed via the True alias workaround.
    assert all(r.on for r in sc.reactions)


def test_scenario_state_transition(observer):
    import scenarios as scn
    sc = scn.load("scenarios_examples/login.yaml")
    scn.attach_to_observer(sc, observer)
    # initial state
    ws = observer.list_windows()
    assert ws[0].title == "Acme Login"

    # Apply reactions manually by going through observer.perform_action paths
    # similar to what a click and a type would do.
    observer.perform_action("click_at", element_id="root.0",
                             value={"x": 0, "y": 0})
    observer.perform_action("type", value="alice")
    observer.perform_action("click_at", element_id="root.1",
                             value={"x": 0, "y": 0})
    observer.perform_action("type", value="hunter2")
    observer.perform_action("click_at", element_id="root.2",
                             value={"x": 0, "y": 0})

    ws2 = observer.list_windows()
    assert ws2[0].title == "Acme — Welcome"


def test_invalid_scenario_path():
    with pytest.raises(Exception):
        load("scenarios_examples/does_not_exist.yaml")


def test_lookup_returns_found_flag_even_for_idless_match(observer):
    """Regression test for the spec-id resolver: when the matched node
    has no .id, the resolver must report the match (found=True, id=None)
    rather than continuing the walk and possibly returning a stale id
    from a different branch."""
    import scenarios as scn
    spec = scn._ElemSpec(
        role="Window", name="X", id=None,
        children=[
            scn._ElemSpec(role="Group", name="g", id=None,
                          children=[scn._ElemSpec(role="Button",
                                                   name="OK", id="ok")]),
            scn._ElemSpec(role="Button", name="other", id="other"),
        ],
    )
    sa = scn.ScenarioAdapter(scn.Scenario(
        name="t", initial_state="s",
        states={"s": scn._StateSpec(name="s", windows=[scn._WindowSpec(
            uid="mock:t", title="t", bounds={"x":0,"y":0,"width":1,"height":1},
            tree=spec,
        )])},
        reactions=[], oracles={},
    ))
    # The root has id=None; matching it must NOT return "other" from the
    # next branch.
    found, sid = sa._lookup(spec, "root", "root")
    assert found is True
    assert sid is None


def test_target_name_reaction_resolves_actual_element_name():
    """A reaction keyed on target_name should only fire when the actual
    spec element's .name matches the configured target_name."""
    import scenarios as scn
    spec = scn._ElemSpec(
        role="Window", name="W", id=None,
        children=[
            scn._ElemSpec(role="Button", name="Save",   id="save"),
            scn._ElemSpec(role="Button", name="Cancel", id="cancel"),
        ],
    )
    sc = scn.Scenario(
        name="t", initial_state="s",
        states={"s": scn._StateSpec(name="s", windows=[scn._WindowSpec(
            uid="mock:t", title="t",
            bounds={"x":0,"y":0,"width":1,"height":1}, tree=spec,
        )])},
        reactions=[
            scn._Reaction(
                on={"tool": "click_element", "target_name": "Save"},
                set=[], transition_to="welcome",
            )
        ],
        oracles={},
    )
    sc.states["welcome"] = scn._StateSpec(name="welcome", windows=[])
    sa = scn.ScenarioAdapter(sc)
    # Click on the Cancel button (root.1).  The reaction is keyed by
    # target_name=Save, so it must NOT fire.
    sa.handle_action(action="click_at", element_id="root.1",
                      value={"x":0,"y":0}, hwnd=None)
    assert sc.current_state == "s"
    # Click on Save (root.0) — the reaction must fire and transition.
    sa.handle_action(action="click_at", element_id="root.0",
                      value={"x":0,"y":0}, hwnd=None)
    assert sc.current_state == "welcome"

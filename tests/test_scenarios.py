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

"""
scenarios.py — Mock-mode scenario loader and state machine (design doc §15.5).

YAML format:

    name: …
    initial_state: start
    states:
      <name>:
        windows:
          - uid, title, process, pid, bounds, tree (nested)
    reactions:
      - on:    {tool: …, target_id: …, text_regex: …, target_name: …}
        when:  [{id: …, value: …}, …]
        set:   [{id: …, value: …}]
        transition_to: <state-name>
    oracles:
      success: [<predicate>]
      failure: [<predicate>]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from observer import Bounds, UIElement, WindowInfo

logger = logging.getLogger(__name__)


# ─── Loader ───────────────────────────────────────────────────────────────────

class ScenarioError(ValueError):
    pass


def load(path: str) -> "Scenario":
    try:
        import yaml
    except ImportError as e:
        raise ScenarioError(f"PyYAML required to load scenarios: {e}") from e
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ScenarioError(f"scenario root must be a mapping, got {type(data)}")
    return Scenario.from_dict(data, source_path=path)


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class _ElemSpec:
    role: str
    name: str = ""
    value: Optional[str] = None
    id: Optional[str] = None
    secret: bool = False
    bounds: Optional[Dict[str, int]] = None
    enabled: bool = True
    children: List["_ElemSpec"] = field(default_factory=list)


@dataclass
class _WindowSpec:
    uid: str
    title: str
    process: str = ""
    pid: int = 0
    bounds: Dict[str, int] = field(default_factory=lambda: {"x":0,"y":0,"width":800,"height":600})
    tree: Optional[_ElemSpec] = None


@dataclass
class _StateSpec:
    name: str
    windows: List[_WindowSpec] = field(default_factory=list)


@dataclass
class _Reaction:
    on: Dict[str, Any]
    when: List[Dict[str, Any]] = field(default_factory=list)
    set: List[Dict[str, Any]] = field(default_factory=list)
    transition_to: Optional[str] = None


@dataclass
class Scenario:
    name: str
    initial_state: str
    states: Dict[str, _StateSpec]
    reactions: List[_Reaction]
    oracles: Dict[str, List[Dict[str, Any]]]
    source_path: str = ""

    # Mutable runtime state
    current_state: str = ""
    overrides: Dict[Tuple[str, str], Dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.current_state:
            self.current_state = self.initial_state

    @staticmethod
    def from_dict(data: Dict[str, Any], *, source_path: str = "") -> "Scenario":
        name = data.get("name", "scenario")
        initial = data.get("initial_state")
        states_raw = data.get("states") or {}
        if not states_raw:
            raise ScenarioError("scenario must define at least one state")
        if initial is None:
            initial = next(iter(states_raw.keys()))

        states: Dict[str, _StateSpec] = {}
        for sname, sval in states_raw.items():
            wins = []
            for w in (sval.get("windows") or []):
                wins.append(_WindowSpec(
                    uid=w.get("uid", f"mock:{sname}"),
                    title=w.get("title", ""),
                    process=w.get("process", ""),
                    pid=int(w.get("pid", 0)),
                    bounds=dict(w.get("bounds") or
                                {"x":0,"y":0,"width":800,"height":600}),
                    tree=_load_elem(w.get("tree")) if w.get("tree") else None,
                ))
            states[sname] = _StateSpec(name=sname, windows=wins)

        # PyYAML 1.1 treats unquoted 'on' as boolean True; accept either spelling.
        reactions = [
            _Reaction(
                on=dict(r.get("on") or r.get(True) or r.get("match") or {}),
                when=list(r.get("when") or []),
                set=list(r.get("set") or []),
                transition_to=r.get("transition_to"),
            )
            for r in (data.get("reactions") or [])
        ]
        oracles = data.get("oracles") or {}
        return Scenario(name=name, initial_state=initial, states=states,
                        reactions=reactions, oracles=oracles,
                        source_path=source_path)


def _load_elem(d: Dict[str, Any]) -> _ElemSpec:
    return _ElemSpec(
        role=d.get("role", "Unknown"),
        name=d.get("name", ""),
        value=d.get("value"),
        id=d.get("id"),
        secret=bool(d.get("secret")),
        bounds=dict(d["bounds"]) if d.get("bounds") else None,
        enabled=bool(d.get("enabled", True)),
        children=[_load_elem(c) for c in (d.get("children") or [])],
    )


# ─── Scenario adapter (plugged into MockAdapter via .scenario attribute) ─────

class ScenarioAdapter:
    """Bridges a Scenario to the MockAdapter contract."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.last_focused_spec_id: Optional[str] = None

    # ── observation ──────────────────────────────────────────────────────────

    def list_windows(self, nonce: str) -> List[WindowInfo]:
        out: List[WindowInfo] = []
        state = self.scenario.states[self.scenario.current_state]
        for i, w in enumerate(state.windows):
            b = w.bounds
            out.append(WindowInfo(
                handle=1000 + i, title=w.title,
                process_name=w.process or "scenario.exe",
                pid=w.pid or (10000 + i),
                bounds=Bounds(b["x"], b["y"], b["width"], b["height"]),
                is_focused=(i == 0),
                window_uid=w.uid or f"mock:{i}:{nonce}",
            ))
        return out

    def get_element_tree(self, hwnd) -> Optional[UIElement]:
        state = self.scenario.states[self.scenario.current_state]
        # Pick window by handle if given, else first.
        spec = state.windows[0] if state.windows else None
        if hwnd is not None:
            for i, w in enumerate(state.windows):
                if 1000 + i == hwnd:
                    spec = w
                    break
        if spec is None or spec.tree is None:
            return UIElement("root", "(empty)", "Window",
                             bounds=Bounds(0, 0, 800, 600))
        return self._build(spec.tree, "root", spec.uid, spec.bounds)

    def _build(self, e: _ElemSpec, eid: str, win_uid: str,
               win_bounds: Dict[str, int]) -> UIElement:
        b = e.bounds or win_bounds
        bounds = Bounds(b.get("x", 0), b.get("y", 0),
                        b.get("width", 0), b.get("height", 0))
        # Apply overrides
        ov = self.scenario.overrides.get((win_uid, e.id or "")) if e.id else None
        value = e.value
        if ov and "value" in ov:
            value = ov["value"]
        return UIElement(
            element_id=eid, name=e.name, role=e.role,
            value=value, bounds=bounds, enabled=e.enabled,
            focused=False,
            children=[self._build(c, f"{eid}.{i}", win_uid, win_bounds)
                      for i, c in enumerate(e.children)],
        )

    # ── action handler ───────────────────────────────────────────────────────

    def handle_action(self, *, action: str, element_id: Optional[str],
                      value: Any, hwnd: Any) -> Optional[Dict[str, Any]]:
        # Map low-level action -> normalised tool name
        tool = self._action_to_tool(action, value)

        target_spec_id = self._resolve_target_to_spec_id(element_id)
        # type_text/press_key inherit the most recently clicked element's spec id.
        if target_spec_id is None and tool in ("type_text", "press_key"):
            target_spec_id = self.last_focused_spec_id
        if tool == "click_element" and target_spec_id:
            self.last_focused_spec_id = target_spec_id

        text = ""
        if isinstance(value, str):
            text = value
        elif isinstance(value, dict):
            text = str(value.get("text", "") or value.get("value", ""))

        state = self.scenario.states[self.scenario.current_state]
        win = state.windows[0] if state.windows else None

        for r in self.scenario.reactions:
            on = r.on
            on_tool = on.get("tool")
            if on_tool != tool:
                continue
            on_target = on.get("target_id")
            if on_target and on_target != target_spec_id:
                continue
            tname = on.get("target_name")
            if tname and (target_spec_id is None or
                          self._spec_name(tname) != tname):
                continue
            text_regex = on.get("text_regex")
            captured: Dict[str, str] = {}
            if text_regex:
                m = re.fullmatch(text_regex, text or "")
                if not m:
                    continue
                if m.groups():
                    captured["text"] = m.group(1)
                else:
                    captured["text"] = text

            if not self._when_matches(r.when, win):
                continue

            # Apply set.
            for s in r.set:
                sid = s.get("id")
                if not sid or win is None:
                    continue
                key = (win.uid, sid)
                ov = self.scenario.overrides.setdefault(key, {})
                if "value" in s:
                    v = s["value"]
                    if isinstance(v, str) and "{text}" in v and "text" in captured:
                        v = v.replace("{text}", captured["text"])
                    elif v == "{text}" and "text" in captured:
                        v = captured["text"]
                    ov["value"] = v

            if r.transition_to and r.transition_to in self.scenario.states:
                self.scenario.current_state = r.transition_to

            return {"success": True, "action": action, "scenario_reaction": True}

        return None  # no reaction matched; fall through to mock default

    # ── helpers ──────────────────────────────────────────────────────────────

    def _action_to_tool(self, action: str, value: Any) -> str:
        if action == "type":
            return "type_text"
        if action == "key":
            return "press_key"
        if action == "click_at":
            return "click_element"  # element-targeted in scenarios
        return action

    def _resolve_target_to_spec_id(self, element_id: Optional[str]
                                    ) -> Optional[str]:
        """Map an observed element_id (e.g. 'root.1.2') to the spec's id."""
        if not element_id:
            return None
        state = self.scenario.states[self.scenario.current_state]
        for spec_w in state.windows:
            if spec_w.tree is None:
                continue
            sid = self._lookup(spec_w.tree, element_id, "root")
            if sid:
                return sid
        return None

    def _lookup(self, e: _ElemSpec, target_id: str,
                cur_id: str) -> Optional[str]:
        if cur_id == target_id:
            return e.id
        for i, c in enumerate(e.children):
            r = self._lookup(c, target_id, f"{cur_id}.{i}")
            if r is not None:
                return r
        return None

    def _spec_name(self, name: str) -> str:
        return name

    def _when_matches(self, when: List[Dict[str, Any]],
                      win: Optional[_WindowSpec]) -> bool:
        if not when:
            return True
        if win is None:
            return False
        for cond in when:
            sid = cond.get("id")
            if not sid:
                continue
            ov = self.scenario.overrides.get((win.uid, sid)) or {}
            spec_value = ov.get("value", self._spec_value_by_id(win.tree, sid))
            if "value" in cond:
                if spec_value != cond["value"]:
                    return False
            if cond.get("value_not_empty"):
                if spec_value is None or spec_value == "":
                    return False
        return True

    def _spec_value_by_id(self, e: Optional[_ElemSpec],
                          target_id: str) -> Any:
        if e is None:
            return None
        if e.id == target_id:
            return e.value
        for c in e.children:
            v = self._spec_value_by_id(c, target_id)
            if v is not None:
                return v
        return None


# ─── Discovery helper ────────────────────────────────────────────────────────

def attach_to_observer(scenario: Scenario, observer: Any) -> None:
    """Set the scenario adapter on the observer's MockAdapter."""
    from observer import MockAdapter
    adapter = getattr(observer, "_adapter", None)
    if not isinstance(adapter, MockAdapter):
        raise ScenarioError("scenarios require --mock; live adapter is in use")
    adapter.scenario = ScenarioAdapter(scenario)

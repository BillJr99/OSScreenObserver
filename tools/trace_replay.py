"""
Tracing, replay, scenario loading and state oracles.

Split out of tools.py (P3); behavior is unchanged.
"""

from __future__ import annotations

from typing import Any, Dict

from errors import Code, error_dict
from session import get_session

from tools.context import ToolContext, _new_step_id


def trace_start(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import tracing as _tracing
    step_id, caused_by = _new_step_id("trace_start")
    sess = get_session()
    if sess.active_trace is not None and not sess.active_trace.closed:
        return error_dict(Code.BAD_REQUEST, "trace already active",
                          step_id=step_id,
                          trace_id=sess.active_trace.trace_id)
    handle = _tracing.start(label=args.get("label", ""), config=ctx.config)
    sess.active_trace = handle
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "trace_id": handle.trace_id,
        "started_at": handle.started_at,
        "dir": handle.dir,
    }


def trace_stop(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import tracing as _tracing
    step_id, caused_by = _new_step_id("trace_stop")
    sess = get_session()
    if sess.active_trace is None:
        return error_dict(Code.BAD_REQUEST, "no active trace",
                          step_id=step_id)
    info = _tracing.stop(sess.active_trace)
    sess.active_trace = None
    info.update({"ok": True, "success": True,
                 "step_id": step_id, "caused_by_step_id": caused_by})
    return info


def trace_status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("trace_status")
    sess = get_session()
    if sess.active_trace is None:
        return {"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "active_trace_id": None, "step_count": 0, "dir": None}
    h = sess.active_trace
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "active_trace_id": h.trace_id,
        "step_count": h.counter.value,
        "dir": h.dir,
    }


_REPLAYS: Dict[str, Any] = {}


def replay_start(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import replay as _replay
    step_id, caused_by = _new_step_id("replay_start")
    path = args.get("path")
    if not path:
        return error_dict(Code.BAD_REQUEST, "path is required",
                          step_id=step_id)
    mode = args.get("mode", "execute")
    on_div = args.get("on_divergence", "warn")
    try:
        rep = _replay.load(path, mode=mode, on_divergence=on_div)
    except Exception as e:
        return error_dict(Code.BAD_REQUEST, f"could not load trace: {e}",
                          step_id=step_id, path=path)
    handle_id = "rep:" + str(len(_REPLAYS) + 1)
    _REPLAYS[handle_id] = rep
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "replay_id": handle_id,
        "total": len(rep.rows),
        "mode": rep.mode,
        "label": rep.label,
    }


def replay_step(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import replay as _replay
    step_id, caused_by = _new_step_id("replay_step")
    rid = args.get("replay_id") or ""
    rep = _REPLAYS.get(rid)
    if rep is None:
        return error_dict(Code.BAD_REQUEST, "unknown replay_id",
                          step_id=step_id, replay_id=rid)

    def _disp(name: str, a: Dict[str, Any]) -> Dict[str, Any]:
        # Local import: tools.dispatch imports this module to build REGISTRY.
        from tools.dispatch import dispatch
        return dispatch(ctx, name, a)

    out = _replay.step(rep, dispatch=_disp)
    out.update({"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "replay_id": rid})
    return out


def replay_status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("replay_status")
    rid = args.get("replay_id") or ""
    rep = _REPLAYS.get(rid)
    if rep is None:
        return error_dict(Code.BAD_REQUEST, "unknown replay_id",
                          step_id=step_id, replay_id=rid)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "replay_id": rid,
        "position": rep.position,
        "total": len(rep.rows),
        "finished": rep.finished,
        "divergences": rep.divergences,
        "mode": rep.mode,
    }


def replay_stop(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    step_id, caused_by = _new_step_id("replay_stop")
    rid = args.get("replay_id")
    if rid in _REPLAYS:
        _REPLAYS.pop(rid)
        return {"ok": True, "success": True,
                "step_id": step_id, "caused_by_step_id": caused_by,
                "stopped": True}
    return error_dict(Code.BAD_REQUEST, "unknown replay_id",
                      step_id=step_id, replay_id=rid)


def load_scenario(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import scenarios as _scn
    step_id, caused_by = _new_step_id("load_scenario")
    path = args.get("path")
    if not path:
        return error_dict(Code.BAD_REQUEST, "path is required",
                          step_id=step_id)
    try:
        sc = _scn.load(path)
        _scn.attach_to_observer(sc, ctx.observer)
        # The scenario replaces the mock world — cached trees are stale.
        get_session().tree_cache.invalidate_all()
    except _scn.ScenarioError as e:
        return error_dict(Code.SCENARIO_INVALID, str(e),
                          step_id=step_id, path=path)
    except Exception as e:
        return error_dict(Code.SCENARIO_INVALID, f"{type(e).__name__}: {e}",
                          step_id=step_id, path=path)
    return {
        "ok": True, "success": True,
        "step_id": step_id, "caused_by_step_id": caused_by,
        "scenario": sc.name,
        "state": sc.current_state,
        "states": list(sc.states.keys()),
    }


def assert_state(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import oracles as _oracles
    step_id, caused_by = _new_step_id("assert_state")
    pred = args.get("predicate") or args.get("predicates") or []
    out = _oracles.evaluate(ctx.observer, pred, config=ctx.config)
    if out.get("ok"):
        out["step_id"] = step_id
        out["caused_by_step_id"] = caused_by
    return out

"""
tools — Central tool implementations (package form of the former tools.py).

Both mcp_server.py and web_inspector.py dispatch into this package; the
MCP and REST surfaces are thin wrappers.  Every tool returns a dict in
one of two shapes:

    {ok: true,  step_id: …, …tool-specific fields…}
    {ok: false, success: false, step_id: …, error: {code, message, …}}

For backwards compatibility (design doc D5) success-shaped legacy fields
are preserved alongside the new `ok` / `error` object on existing tools.

P3 decomposition: the implementation now lives in submodules (context,
receipts, actions, observe, vision, snapshots, trace_replay, meta,
dispatch).  This __init__ re-exports the entire pre-split public surface
so `import tools` / `from tools import X` keep working unchanged.
"""

from __future__ import annotations

from tools.context import (
    ToolContext,
    _find_by_id,
    _focused_window,
    _is_input_tool,
    _new_dialogs,
    _new_step_id,
    _resolve_element,
    _resolve_window,
)
from tools.receipts import (
    _build_receipt,
    _check_confirmation,
    _confirmation_rules_match,
)
from tools.observe import (
    _count_nodes,
    _effective_depth,
    _filter_tree,
    _flat_to_tree,
    _flatten,
    _intersects_any,
    _observe_changed_only,
    _page_tree,
    _perf_dict,
    _serialize_full_observation,
    _truncate_depth,
    find_element,
    get_visible_areas,
    get_window_structure,
    observe_window,
)
from tools.vision import (
    _apply_crop,
    get_ocr,
    get_screen_description,
    get_screenshot,
    get_screenshot_cropped,
)
from tools.actions import (
    _compose_observe,
    _do_element_action,
    bring_to_foreground,
    clear_text,
    click_at,
    click_element,
    click_element_and_observe,
    double_click_at,
    double_click_element,
    drag,
    focus_element,
    hover_at,
    hover_element,
    invoke_element,
    key_into_element,
    press_key,
    press_key_and_observe,
    right_click_at,
    right_click_element,
    scroll,
    select_option,
    set_value,
    type_and_observe,
    type_text,
)
from tools.snapshots import (
    _check_condition,
    snapshot,
    snapshot_diff,
    snapshot_drop,
    snapshot_get,
    wait_for,
    wait_idle,
)
from tools.trace_replay import (
    _REPLAYS,
    assert_state,
    load_scenario,
    replay_start,
    replay_status,
    replay_step,
    replay_stop,
    trace_start,
    trace_status,
    trace_stop,
)
from tools.meta import (
    get_budget_status,
    get_capabilities,
    get_monitors,
    get_redaction_status,
    list_windows,
    propose_action,
)
from tools.dispatch import (
    REGISTRY,
    _ALLOWLIST_TOOLS,
    _apply_redaction,
    _check_allowlist,
    dispatch,
)

__all__ = [
    # context
    "ToolContext", "_find_by_id", "_focused_window", "_is_input_tool",
    "_new_dialogs", "_new_step_id", "_resolve_element", "_resolve_window",
    # receipts
    "_build_receipt", "_check_confirmation", "_confirmation_rules_match",
    # observe
    "_count_nodes", "_effective_depth", "_filter_tree", "_flat_to_tree",
    "_flatten", "_intersects_any", "_observe_changed_only", "_page_tree",
    "_perf_dict", "_serialize_full_observation", "_truncate_depth",
    "find_element", "get_visible_areas", "get_window_structure",
    "observe_window",
    # vision
    "_apply_crop", "get_ocr", "get_screen_description", "get_screenshot",
    "get_screenshot_cropped",
    # actions
    "_compose_observe", "_do_element_action", "bring_to_foreground",
    "clear_text", "click_at", "click_element", "click_element_and_observe",
    "double_click_at", "double_click_element", "drag", "focus_element",
    "hover_at", "hover_element", "invoke_element", "key_into_element",
    "press_key", "press_key_and_observe", "right_click_at",
    "right_click_element", "scroll", "select_option", "set_value",
    "type_and_observe", "type_text",
    # snapshots / waits
    "_check_condition", "snapshot", "snapshot_diff", "snapshot_drop",
    "snapshot_get", "wait_for", "wait_idle",
    # trace / replay / scenarios / oracles
    "_REPLAYS", "assert_state", "load_scenario", "replay_start",
    "replay_status", "replay_step", "replay_stop", "trace_start",
    "trace_status", "trace_stop",
    # meta
    "get_budget_status", "get_capabilities", "get_monitors",
    "get_redaction_status", "list_windows", "propose_action",
    # dispatch
    "REGISTRY", "_ALLOWLIST_TOOLS", "_apply_redaction", "_check_allowlist",
    "dispatch",
]

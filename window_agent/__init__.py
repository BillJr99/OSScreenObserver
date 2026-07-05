"""
window_agent — Interactive window inspection and LLM agent for
OSScreenObserver (package form of the former window_agent.py).

Usage:
    python -m window_agent [--rest http://127.0.0.1:5001]

At startup you are prompted for:
    OpenWebUI base URL  (e.g. http://localhost:3000)
    OpenWebUI API key
    Model name          (e.g. llama3.2:3b or mistral)

The program then:
    1. Polls the REST server until it responds.
    2. Lists all open windows; you choose one.
    3. Displays the ASCII sketch + accessibility description.
    4. Opens an interactive prompt where you can type a task for the LLM.
    5. The LLM runs an agentic loop, calling screen tools (observe, click,
       type, press keys, get OCR, etc.) until the task is done or it stops.

No API credentials are saved to disk.

P3 decomposition: the implementation now lives in submodules (client,
dispatch, tool_schemas, prompts, loop, cli).  This __init__ re-exports
the pre-split surface so `import window_agent` keeps working unchanged.
"""

from __future__ import annotations

from window_agent.cli import (
    _BANNER,
    ask_connection,
    list_models,
    main,
    pick_model,
    print_banner,
    print_window_list,
    print_window_view,
    prompt,
)
from window_agent.client import (
    _NO_COLOR,
    _NO_REDIRECT_OPENER,
    _OWU_PREFIX,
    LLMClient,
    _c,
    _get,
    _NoRedirectHandler,
    _post,
    _win_params,
    api_action,
    api_bring_to_foreground,
    api_description,
    api_element_tree,
    api_full_screenshot,
    api_list_windows,
    api_observe,
    api_screenshot,
    api_sketch,
    api_visible_areas,
    wait_for_server,
)
from window_agent.dispatch import dispatch_tool
from window_agent.loop import (
    _LLM_MAX_RETRIES,
    _LLM_RETRY_DELAY,
    MAX_TURNS,
    _print_tool_result,
    run_agent,
)
from window_agent.prompts import SYSTEM_PROMPT
from window_agent.tool_schemas import (
    _KEYWORD_GROUPS,
    _META_TOOLS,
    _TOOL_BY_NAME,
    _TOOL_TIER,
    SCREEN_TOOLS,
    _initial_active_tools,
    _tool_defs_for,
)

__all__ = [
    # cli
    "_BANNER", "ask_connection", "list_models", "main", "pick_model",
    "print_banner", "print_window_list", "print_window_view", "prompt",
    # client
    "_NO_COLOR", "_NO_REDIRECT_OPENER", "_OWU_PREFIX", "LLMClient", "_c",
    "_get", "_NoRedirectHandler", "_post", "_win_params", "api_action",
    "api_bring_to_foreground", "api_description", "api_element_tree",
    "api_full_screenshot", "api_list_windows", "api_observe",
    "api_screenshot", "api_sketch", "api_visible_areas", "wait_for_server",
    # dispatch
    "dispatch_tool",
    # loop
    "_LLM_MAX_RETRIES", "_LLM_RETRY_DELAY", "MAX_TURNS",
    "_print_tool_result", "run_agent",
    # prompts
    "SYSTEM_PROMPT",
    # tool schemas
    "_KEYWORD_GROUPS", "_META_TOOLS", "_TOOL_BY_NAME", "_TOOL_TIER",
    "SCREEN_TOOLS", "_initial_active_tools", "_tool_defs_for",
]

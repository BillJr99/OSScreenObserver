# OSScreenObserver — Agentic LLM & Harness Feature Design (v2)

Status: **Locked, implementation-ready.** Every open question from v1 has a
decision; every new tool has a request/response schema; every config key has a
default; every error code has a recovery hint.

Audience: implementers (this doc drives `claude/plan-agentic-llm-features-82sCL`),
authors of agent harnesses targeting OSScreenObserver, and reviewers.

Companion to: `README.md` (user-facing), `screen_observer_api_reference.md`
(updated alongside the implementation).

---

## 1. Goals

1. **Reliable agentic loops.** Every action returns enough signal that the LLM
   can decide what to do next without guessing.
2. **Cheap long sessions.** Diffs, crops, and filters keep token spend bounded
   as the trajectory grows.
3. **First-class evaluation substrate.** Record/replay, scripted mock
   scenarios, and declarative oracles let harnesses score agents reproducibly.
4. **Two-interface invariant.** Every new capability lands simultaneously on
   the REST API (`web_inspector.py`) and the MCP server (`mcp_server.py`),
   backed by shared logic.
5. **Backwards compatible.** Existing tools and response shapes keep working.
   New fields are additive.

## 2. Non-goals

- Cross-machine remoting, sandboxing, or container orchestration.
- Replacing UIA / AX / AT-SPI with a custom inspector.
- Web-app DOM access. Browser windows are observed through OS accessibility.
- A new agent runtime. `window_agent.py` stays a reference client.

## 3. Locked decisions

| ID | Question | Decision |
|---|---|---|
| D1 | Implementation scope | All six phases, production-grade. Tests + GitHub Actions CI. |
| D2 | Session model | **Single global session.** REST and MCP share one in-memory state object. |
| D3 | Selector grammar | **Both XPath-ish and CSS-ish, share AST.** Parser auto-detects from the leading character. XPath default in docs. |
| D4 | Diff format | **Both, custom default.** Server emits the custom shape; pass `format="json-patch"` to get RFC 6902 instead. |
| D5 | Legacy `success` field | **Emit both.** New endpoints add `ok` and `error.code`; legacy endpoints add the new fields alongside today's `success` / `error` string. |
| D6 | Replay non-determinism | **Per-tool comparison rules table** (§15.4). Unrecorded fields are ignored. |
| D7 | Redaction depth | **Opt-in image blur.** Default = tree+OCR text + VLM preamble. `redaction.blur_screenshots: true` enables PIL blur. |
| D8 | Scenario DSL | **YAML.** Adds PyYAML dep. State-machine reactions are pattern-matched. |
| D9 | Confirmation token binding | **`(window_uid, selector, bbox)` with ±20 px tolerance.** |
| D10 | Trace screenshots | **Full screen + per-window thumbnail** at the cadence. |
| D11 | Cross-platform | **All three platforms first-class.** Real macOS `pyobjc` and Linux `pyatspi` AX adapters. Per-platform occlusion testing. |
| D12 | Tests / CI | Unit + mock-mode integration tests under `tests/`, plus `.github/workflows/ci.yml` running pytest + ruff. No Docker/Xvfb in CI. |
| D13 | "Changed" hash | Tree hash excludes `focused` and absolute timestamps; **includes** bounds, name, value, role, enabled. Drift in any of the latter is meaningful change. |
| D14 | `ElementOccluded` | Implemented on Windows via Z-order; on macOS via `CGWindowListCopyWindowInfo` Z-order; on Linux via `_NET_CLIENT_LIST_STACKING` (X11) — falls back to "assumed visible" when unavailable. |
| D15 | `wait_for` cost | Polling. Per-call `poll_ms` (default 200, min 50). Server enforces a max total wait (`wait_for.max_timeout_ms`, default 60_000). |
| D16 | Trace storage | `traces/<trace_id>/trace.jsonl` + `traces/<trace_id>/screenshots/step-NNNNN-full.png` + `…-window.png`. |
| D17 | Audit log redaction | Separate config block (`audit.redact_arg_keys`, default includes `text` for `type_text` and `value` for `set_value`). |

## 4. Design principles

- **Two interfaces, one core.** Logic lives on `ScreenObserver` or new sibling
  modules. Both `mcp_server.py` and `web_inspector.py` are thin wrappers.
- **Stable IDs over positional indexes.** `window_uid` and `element_selector`
  are the new primary keys. `window_index` and integer `id` stay as legacy.
- **Diffs over snapshots.** Anything tree-shaped accepts `since=<token>`.
- **Receipts on every action.** Every input tool returns an `ActionReceipt`.
- **Errors are data.** A typed taxonomy with `recoverable` and
  `suggested_next_tool` so agents branch programmatically.
- **Mock parity.** Every new feature works under `--mock`. If it doesn't,
  it isn't shipped.

## 5. Glossary

| Term | Definition |
|---|---|
| `window_uid` | Stable opaque string identifying a window; `win:{pid}:{hwnd}`, `mac:{cg_window_number}`, `x11:{wmctrl_id}`, or `mock:{idx}:{nonce}`. Persists until window closes. |
| `element_selector` | XPath-ish or CSS-ish ancestry path through the accessibility tree. See §6.2. |
| `tree_token` | Opaque cursor returned by any tree-producing tool; pass back via `since=` for a diff. TTL 5 min, ring buffer of 16 per window. |
| `snapshot_id` | Server-side handle pointing to a frozen `(windows, trees, screenshots, ocr, ts)` tuple. TTL 5 min, max 32. |
| `step_id` | Monotonic per-process integer assigned to every tool call. |
| `trace` | JSONL stream of `{step_id, ts, caller, tool, args, result_summary, …}` rows under `traces/<trace_id>/`. |
| `scenario` | YAML file consumed by mock mode that scripts initial state plus reactions. |
| `ActionReceipt` | Structured response from any input tool — see §7. |

---

## 6. Stable identity, monitors, and selectors

### 6.1 `window_uid`

`WindowInfo` gains a `window_uid` field, populated per-adapter:

| Adapter | Format |
|---|---|
| Windows | `win:{pid}:{hwnd}` |
| macOS | `mac:{cg_window_number}` |
| Linux | `x11:{wmctrl_id}` (hex stripped) |
| Mock | `mock:{index}:{8-hex-nonce}` (nonce regenerated per process) |

Every existing tool that takes `window_index` also accepts `window_uid`. If
both are supplied, `window_uid` wins and the response includes
`warning: "both window_index and window_uid given; window_uid used"`.
Resolving a stale uid returns `WindowGone`.

### 6.2 Selector grammar

The parser auto-detects format by leading character.

**XPath-ish (default; starts with role token).**
```
role[name="literal"]/role[name~="regex"]/role[index=2]
Window[name="Notepad"]/Pane/Button[name="OK"]
```
Predicates: `name=`, `name~=` (regex, anchored), `value=`, `value~=`,
`role=`, `keyboard_shortcut=`, `enabled=true`, `focused=true`, `index=N`
(zero-based among same-role siblings). `*` matches any role. Empty
predicate brackets allowed: `Window/Group/Button[index=0]`.

**CSS-ish (starts with `>` or `Window>` style with combinators).**
```
Window > Pane Button[name="OK"]
```
- `>` = direct child.
- whitespace = descendant.
- `[attr=...]` and `[attr~=...]` predicates as above.
- `:nth-of-type(n)` is the CSS spelling of `[index=n-1]`.

Both compile to the same AST: a list of `Step(role, predicates, axis)` where
`axis ∈ {child, descendant}`. Resolution walks the **full** tree (filtered
trees from §10 are *display* filters; selectors always resolve against the
unfiltered walk).

`find_element(window_uid, selector)` returns:
```json
{
  "ok": true,
  "element_id": "root.1.2",
  "selector": "Window/Pane/Button[name=\"OK\"]",
  "bounds": {"x":320,"y":480,"width":80,"height":28},
  "ambiguous_matches": 1,
  "all_matches": [{"element_id":"root.1.2","bounds":{...}}]
}
```
`ambiguous_matches > 1` flags brittle selectors. `all_matches` is capped at
10. When zero match, returns `ElementNotFound`.

### 6.3 Monitors / DPI

`get_monitors()` returns:
```json
{
  "ok": true,
  "monitors": [
    {"index":0, "primary":true, "bounds":{...},
     "scale_factor":1.0, "logical_bounds":{...}, "physical_bounds":{...}}
  ]
}
```
`list_windows` adds `monitor_index`, `scale_factor`, `logical_bounds`,
`physical_bounds`. Click coordinates remain physical pixels by default; an
optional `coordinate_space: "logical"` parameter is accepted on
`click_at`, `click_element`, `hover_at`, `drag`.

### 6.4 Capabilities

`get_capabilities()`:
```json
{
  "ok": true,
  "platform": "Linux",
  "adapter": "linux-atspi",
  "version": "0.2.0",
  "protocol_version": 2,
  "supports": {
    "accessibility_tree": true,
    "uia_invoke": false,
    "occlusion_detection": true,
    "drag": true,
    "ocr": true,
    "vlm": true,
    "redaction": true,
    "scenarios": true,
    "tracing": true,
    "replay": true,
    "image_blur": true
  },
  "config": {"tree_max_depth": 8, "ascii_grid": {"width":110,"height":38}}
}
```

---

## 7. ActionReceipt

Every input tool (new and composite) returns:

```json
{
  "ok": true,
  "step_id": 42,
  "caused_by_step_id": 42,
  "action": "click_element",
  "dry_run": false,
  "target": {
    "window_uid": "win:1234:0xabc",
    "element_id": "root.1.2",
    "selector": "Window/Pane/Button[name=\"OK\"]",
    "bounds": {"x":320,"y":480,"width":80,"height":28}
  },
  "before": {"tree_hash":"sha1:…", "focused_selector":"…"},
  "after":  {"tree_hash":"sha1:…", "focused_selector":"…"},
  "changed": true,
  "new_dialogs": [{"window_uid":"…","title":"Confirm"}],
  "duration_ms": 184,
  "success": true
}
```

Notes:
- `success` is the legacy field (D5); duplicates `ok` for backwards compat.
- `changed` is computed from `tree_hash_before != tree_hash_after` (D13).
- `new_dialogs` is the diff between window lists before and after the call.
- `target.bounds` is the resolved bbox at action time (used by confirm
  tokens, §22).
- For `dry_run: true`, the action is not invoked; `before` and `after`
  point to the same observation; `changed: false`.

---

## 8. Element-targeted actions

| MCP tool | REST | Required args | Notes |
|---|---|---|---|
| `click_element` | `POST /api/element/click` | window + element id/selector | `button=left\|right\|middle` (default `left`), `count=1\|2` (default `1`) |
| `focus_element` | `POST /api/element/focus` | window + element | UIA `SetFocus` on Windows; AXUIElementSetAttributeValue on macOS; AT-SPI grab_focus on Linux |
| `set_value` | `POST /api/element/set_value` | window + element + `value` | `clear_first=true`. UIA `ValuePattern` preferred; falls back to focus + select-all + type |
| `invoke_element` | `POST /api/element/invoke` | window + element | UIA `InvokePattern` preferred; falls back to `click_element` |
| `select_option` | `POST /api/element/select` | window + element + `option_name`/`option_index` | UIA `SelectionItemPattern` |

All return `ActionReceipt`. Errors: `ElementNotFound`, `ElementOccluded`,
`ElementDisabled`, `PatternUnsupported`, `WindowGone`.

---

## 9. Richer input verbs

Beyond click/type/key/scroll, add:

| Tool | REST | Behavior |
|---|---|---|
| `hover_at` / `hover_element` | `POST /api/hover` | Move pointer; pause `hover_ms` (default 250). |
| `right_click_at` / `right_click_element` | `POST /api/element/right_click` | Synonym for `click_element(button=right)`. |
| `double_click_at` / `double_click_element` | `POST /api/element/double_click` | Synonym for `click_element(count=2)`. |
| `drag` | `POST /api/drag` | `from`/`to` accept `{x,y}` or `{element}`. `modifiers: ["shift", …]`. Records `path` (3 sample points). |
| `key_into_element` | `POST /api/element/key` | `focus_element` then `press_key` atomically. |
| `clear_text` | `POST /api/element/clear_text` | Focus, select-all, delete. |

All return `ActionReceipt`. `drag` adds `path` to the receipt.

---

## 10. Tree filtering & paging

`get_window_structure` (existing) gains optional params:

```json
{
  "window_uid": "...",
  "roles": ["Button","Edit","Hyperlink"],
  "exclude_roles": ["Image"],
  "visible_only": true,
  "name_regex": "Save|Submit",
  "max_text_len": 80,
  "prune_empty": true,
  "max_nodes": 500,
  "page_cursor": null
}
```

Response adds `truncated: bool`, `next_cursor: string|null`, `node_count`.

`page_cursor` is the post-order `element_id` of the last returned node; the
server resumes the walk from the next sibling. Cursors are stable as long
as the underlying tree shape hasn't changed; if it has, the server returns
`SnapshotExpired` (recoverable, retry without the cursor).

---

## 11. Cropped & token-budgeted perception

### 11.1 `get_screenshot` cropping

```json
{
  "window_uid":"...",
  "element_id":"root.1.2",
  "bbox":{"x":..,"y":..,"width":..,"height":..},
  "padding_px":8,
  "max_width":1024
}
```
Either `element_id` *or* `bbox` *or* neither (whole window). `max_width`
downscales preserving aspect ratio. Response adds `source_bbox` (absolute
pixels).

### 11.2 `get_screen_description` budgeting

Adds:
- `max_tokens` — hard cap (approx; cuts at character boundary with `…
  [truncated]`).
- `focus_element` — element id; biases the prompt and filters OCR overlay.
- `mode="auto"` — server picks: accessibility if tree size ≥ 10 nodes; else
  OCR if available; else VLM if enabled; else accessibility. The chosen
  mode is reported as `effective_mode`.

### 11.3 `get_ocr` (new)

Region-scoped OCR: input `window_uid` + (`element_id` | `bbox`); output
`[{text, confidence, bbox}]`.

---

## 12. Wait & synchronization

### 12.1 `wait_for`

```json
{
  "window_uid": "win:…",
  "any_of": [
    {"type":"element_appears", "selector":"Window/.../Button[name=\"OK\"]"},
    {"type":"element_disappears", "element_id":"root.1.2"},
    {"type":"text_visible", "regex":"Saved"},
    {"type":"window_appears", "title_regex":"Confirm"},
    {"type":"window_disappears", "window_uid":"…"},
    {"type":"tree_changes", "since":"<tree_token>"},
    {"type":"focused_changes"}
  ],
  "timeout_ms": 5000,
  "poll_ms": 200
}
```
Response:
```json
{"ok":true, "matched_index":0, "matched_detail":{...},
 "elapsed_ms":420, "polls":3}
```
On timeout: `{"ok":false, "error":{"code":"Timeout","recoverable":true},
"elapsed_ms":5000, "polls":25, "last_observation":{...}}`.

`max(timeout_ms) = config.wait_for.max_timeout_ms` (default 60000).

### 12.2 `wait_idle`

Returns once `tree_hash` is stable for `quiet_ms` (default 750) or
`timeout_ms` is reached.

---

## 13. Observe-with-diff

Every tree-producing tool (`get_window_structure`, `observe_window`,
`get_screen_description`, `get_visible_areas`, `get_ocr`) returns a
`tree_token`. Passing `since=<tree_token>` returns a diff.

### 13.1 Custom format (default)
```json
{
  "ok": true,
  "window_uid": "…",
  "tree_token": "tt:abc123",
  "base_token": "tt:prev",
  "format": "custom",
  "changes": [
    {"op":"add",     "path":"0/2/3", "node":{...}},
    {"op":"remove",  "path":"0/4"},
    {"op":"replace", "path":"0/2/3", "fields":{"value":"hello"}},
    {"op":"move",    "from":"0/5", "to":"0/2/4"}
  ],
  "unchanged": false
}
```
`path` is slash-delimited child-index trail. `unchanged: true` ⇒ empty
`changes`.

### 13.2 RFC 6902 format (`format="json-patch"`)
Standard JSON Patch over the serialized tree. `move` is emitted when both
sides agree on identity (matched by stable element role + name signature);
otherwise add/remove pairs.

### 13.3 Token lifecycle
- Stored per-`window_uid` in a per-process LRU keyed by token, max 16
  trees per window, TTL 5 minutes.
- Tokens survive across REST and MCP calls (single global session, D2).
- An expired or unknown token returns the **full** tree with
  `base_token: null` and `format: "full"` (recoverable; the agent simply
  caches the new token).

---

## 14. Composite action+observe

| Tool | REST | Behavior |
|---|---|---|
| `click_element_and_observe` | `POST /api/element/click_and_observe` | `click_element`, sleep `wait_after_ms` (default 200), then `observe_window(since=…)`. |
| `type_and_observe` | `POST /api/type_and_observe` | `type_text`, sleep, observe. |
| `press_key_and_observe` | `POST /api/key_and_observe` | `press_key`, sleep, observe. |

Response = `ActionReceipt` plus `observation` field (the diff or full
observation from §13).

---

## 15. Snapshots, tracing, replay (harness substrate)

### 15.1 Snapshots

| Tool | REST |
|---|---|
| `snapshot()` | `POST /api/snapshot` |
| `snapshot_get(snapshot_id)` | `GET  /api/snapshot/<id>` |
| `snapshot_diff(a, b, format?)` | `POST /api/snapshot/diff` |
| `snapshot_drop(snapshot_id)` | `DELETE /api/snapshot/<id>` |

Stored in memory: TTL 5 min, LRU 32. Diff reuses §13 machinery.

### 15.2 Tracing

```text
trace_start(label?)  → {trace_id, started_at, dir}
trace_stop(trace_id) → {trace_id, path, step_count, duration_ms}
trace_status()       → {active_trace_id?, step_count, dir?}
```

Layout (D16):
```
traces/
  <trace_id>/
    trace.jsonl
    screenshots/
      step-00001-full.png
      step-00001-window.png
      step-00006-full.png
      …
```

Each line:
```json
{
  "step_id":17,"ts":"2026-05-10T14:05:01.234Z","caller":"mcp",
  "tool":"click_element","args":{...},"result_summary":{"ok":true,"changed":true},
  "screenshot_full_ref":"screenshots/step-00017-full.png",
  "screenshot_window_ref":"screenshots/step-00017-window.png",
  "tree_hash_before":"sha1:…","tree_hash_after":"sha1:…",
  "duration_ms":184
}
```

`config.tracing`:
```json
{
  "dir":"./traces",
  "screenshot_every_n_actions":5,
  "max_args_bytes":4096,
  "redact_keys":["api_key","password","Authorization"]
}
```

### 15.3 Replay

| Tool | Behavior |
|---|---|
| `replay_start(path, mode="execute"\|"verify", on_divergence="stop"\|"warn"\|"resume")` | Loads JSONL, primes engine. |
| `replay_step()` | Advance one step; returns `{position, total, divergence?}`. |
| `replay_status()` | `{position, total, divergences:[]}`. |
| `replay_stop()` | Frees resources. |

### 15.4 Replay comparison rules (D6)

Per-tool dictionary of fields to compare. Anything not listed is ignored.

| Tool | Compared fields |
|---|---|
| `list_windows` | `count`, set of `(title)` (positions ignored) |
| `get_window_structure` | `node_count`, `tree_hash` |
| `find_element` | `ok`, `error.code`, `ambiguous_matches > 0` |
| `click_element` / `focus_element` / `invoke_element` / `set_value` / `select_option` | `ok`, `error.code`, `target.selector`, `changed` |
| `click_at` / `hover_at` / `drag` | `ok`, `error.code` |
| `type_text` / `press_key` / `scroll` | `ok`, `error.code` |
| `get_screen_description` | `ok`, `effective_mode` (text body ignored — non-deterministic) |
| `get_screenshot*` | `ok`, `width`, `height` (image bytes ignored) |
| `get_ocr` | `ok` (text ignored) |
| `wait_for` / `wait_idle` | `ok`, `matched_index` (timing ignored) |
| `assert_state` | `ok`, `failures[].kind` |
| `snapshot*` | `ok` |
| All read-only `get_*` | `ok` |

A divergence row:
```json
{"step_id":42,"tool":"click_element",
 "differences":[{"path":"target.selector","want":"…","got":"…"}]}
```

### 15.5 Mock scenario DSL (D8)

```yaml
name: login-happy-path
initial_state: start

states:
  start:
    windows:
      - uid: mock:app
        title: "Acme Login"
        process: acme.exe
        pid: 1234
        bounds: {x: 100, y: 100, width: 800, height: 600}
        tree:
          role: Window
          name: "Acme Login"
          children:
            - {role: Edit,   name: "Username", id: u}
            - {role: Edit,   name: "Password", id: p, value: "", secret: true}
            - {role: Button, name: "Login",    id: btn}

  welcome:
    windows:
      - uid: mock:app
        title: "Acme — Welcome"
        process: acme.exe
        pid: 1234
        bounds: {x: 100, y: 100, width: 800, height: 600}
        tree:
          role: Window
          children:
            - {role: Text, name: "Hello, alice"}

reactions:
  - on: {tool: type_text, target_id: u, text_regex: ".+"}
    set: [{id: u, value: "{text}"}]
  - on: {tool: type_text, target_id: p, text_regex: ".+"}
    set: [{id: p, value: "<filled>"}]
  - on: {tool: click_element, target_id: btn}
    when: [{id: u, value: "alice"}, {id: p, value_not_empty: true}]
    transition_to: welcome

oracles:
  success:
    - {kind: text_visible, regex: "Hello, alice"}
  failure:
    - {kind: window_appears, title_regex: "Error"}
```

**Reaction semantics.**
1. Reactions fire **after** the action would have succeeded, in declaration
   order. First matching reaction wins; later reactions for the same tool
   call are skipped.
2. `set` mutates element fields in the current state (not a transition).
3. `transition_to` swaps the active state; subsequent `observe` calls see
   the new state's tree.
4. `when` is an AND list of element predicates; missing IDs evaluate false.
5. `text_regex` is matched against the action's `text` argument and the
   first capture group is bound to `{text}` for substitution in `set`.
6. If no reaction matches, the action returns its standard `ActionReceipt`
   with `changed: false`.

Loaded via `python main.py --mock --scenario scenarios/login.yaml`.

### 15.6 Oracles

`assert_state(predicate)` — `predicate` is a list (AND) of:

| Kind | Args |
|---|---|
| `element_exists` | `selector`, `window_uid?` |
| `element_absent` | `selector`, `window_uid?` |
| `value_equals` | `selector`, `expected` |
| `value_matches` | `selector`, `regex` |
| `text_visible` | `regex`, `window_uid?`, `mode?` (`tree`/`ocr`/`auto`, default `auto`) |
| `window_focused` | `title_regex` |
| `window_exists` | `title_regex` or `window_uid` |
| `tree_hash_equals` | `expected_hash` |
| `screenshot_similar` | `reference_path`, `min_ssim=0.95` (requires `scikit-image`; returns `PredicateUnsupported` otherwise) |

Returns:
```json
{"ok":true, "all_passed":true,
 "results":[{"kind":"element_exists","passed":true,"observed":{...}}]}
```
Never raises; harness branches on `all_passed`.

---

## 16. Budgets

Per-process limits set via CLI flags (forwarded to config) or
`initialization_options` on MCP `initialize`:

| Limit | Default | Trip behavior |
|---|---|---|
| `max_actions` | unlimited | `BudgetExceeded` (non-recoverable) |
| `max_screenshots` | unlimited | `BudgetExceeded` |
| `max_vlm_tokens` | unlimited | `BudgetExceeded` |
| `max_session_seconds` | unlimited | `BudgetExceeded` |
| `actions_per_minute` | unlimited | `RateLimited` (recoverable, retry hint) |

`get_budget_status()` returns remaining counts:
```json
{"ok":true,
 "actions":{"used":17,"limit":100,"remaining":83},
 "screenshots":{"used":3,"limit":50,"remaining":47},
 "session_seconds":{"elapsed":42,"limit":1800,"remaining":1758},
 "actions_per_minute":{"in_window":4,"limit":30,"remaining":26}}
```

---

## 17. Step IDs & causality

Every result includes:
```json
{"step_id":42, "caused_by_step_id":41}
```
- `step_id` is monotonic per-process across REST + MCP.
- For input tools, `caused_by_step_id == step_id`.
- For read-only tools, `caused_by_step_id` is the most recent input action's
  `step_id`; `null` until the first action.

---

## 18. Sensitive-region redaction (D7)

`config.redaction`:
```json
{
  "enabled": true,
  "window_title_patterns": ["1Password","Bitwarden"],
  "element_name_patterns":  ["Password","PIN","SSN"],
  "element_role_patterns":  ["PasswordEdit"],
  "ocr_text_patterns":      ["\\b\\d{3}-\\d{2}-\\d{4}\\b"],
  "replacement":            "[REDACTED]",
  "blur_screenshots":       false
}
```

Effects:
- Tree node `name`/`value` matching → replacement string.
- OCR output → regex sweep with replacement.
- VLM prompt → preamble: "do not transcribe content of any field whose role
  matches: …".
- Screenshots → if `blur_screenshots: true`, matched bboxes painted solid
  black (cheap, no PIL filter).
- The redaction patterns themselves are logged to traces; the redacted
  content is not.

`get_redaction_status()` reports `{enabled, patterns_count, applied_count}`.

---

## 19. Dry-run and action allowlist

### 19.1 `dry_run`
Every input tool accepts `dry_run: true`. Server resolves the target,
performs all checks, returns the receipt **without** invoking the platform
layer. `dry_run: true, changed: false` in the receipt.

### 19.2 Allowlist
`config.actions`:
```json
{"allow": ["click_element","focus_element","wait_for"],
 "deny":  ["press_key","type_text"],
 "default": "allow"}
```
Mismatches return `PermissionDenied` (non-recoverable).

---

## 20. Audit log

`audit.log` (path configurable via `logging.audit_path`, default
`./audit.log`). One line per call:

```text
2026-05-10T14:05:01.234Z step=42 caller=mcp tool=click_element ok=true changed=true
  args.window_uid=win:1234:0xabc args.element_id=root.1.2 redactions=args.text
```

Off by default; `logging.audit: true` to enable. Rotation via
`logging.audit_max_bytes` (default 10 MB) and `logging.audit_backups`
(default 3).

`config.audit.redact_arg_keys` (D17) lists arg keys whose values are
replaced with `<redacted>` before serialization. Defaults:
`["text", "value", "password", "api_key", "Authorization"]`.

---

## 21. Confirmation tokens (D9)

Destructive actions require a `confirm_token`. Config:
```json
{
  "confirmation_required": [
    {"name_regex": "(?i)delete|remove|send|pay|sign"},
    {"role": "Button", "name_regex": "(?i)submit"}
  ]
}
```

Flow:
1. Agent: `propose_action(action, args)` → server resolves the target and
   returns:
   ```json
   {"ok":true, "confirm_token":"ct:abc123", "expires_at":"…",
    "would_target":{"window_uid":"…","selector":"…","bounds":{...},
                    "screenshot_b64":"…"}}
   ```
   Tokens TTL 60 s (config: `confirmation.ttl_seconds`).
2. Agent calls the actual action with `confirm_token=…`.
3. Server validates: token unused, not expired, action+selector match,
   window_uid matches, **resolved bbox within ±20 px of recorded bbox**
   (config: `confirmation.bbox_tolerance_px`). Otherwise
   `ConfirmationInvalid` (recoverable; agent should re-propose).

Without a token, destructive actions return `ConfirmationRequired`
(recoverable; suggested next: `propose_action`).

---

## 22. Structured error taxonomy

All errors:
```json
{"ok":false,"success":false,"step_id":99,
 "error":{"code":"ElementNotFound","message":"…",
          "recoverable":true,"suggested_next_tool":"find_element",
          "context":{"selector":"…","window_uid":"…"}}}
```

| Code | Recoverable | Suggested next |
|---|---|---|
| `ElementNotFound` | yes | `find_element` |
| `ElementOccluded` | yes | `bring_to_foreground` |
| `ElementDisabled` | yes | `wait_for` (idle) |
| `WindowGone` | yes | `list_windows` |
| `WindowOccluded` | yes | `bring_to_foreground` |
| `Timeout` | yes | retry with longer `timeout_ms` |
| `PatternUnsupported` | yes | `click_element` (fall back) |
| `RateLimited` | yes | `get_budget_status` |
| `BudgetExceeded` | no | — |
| `PermissionDenied` | no | — |
| `ConfirmationRequired` | yes | `propose_action` |
| `ConfirmationInvalid` | yes | `propose_action` |
| `SnapshotExpired` | yes | retry without `since` / re-take |
| `ScenarioInvalid` | no | — |
| `PlatformUnsupported` | no | `get_capabilities` |
| `PredicateUnsupported` | no | — |
| `Internal` | no | — |
| `BadRequest` | no | — |

---

## 23. Telemetry endpoints

`GET /api/healthz` — `{ok, uptime_s, adapter, version, step_count}`.

`GET /api/metrics` — Prometheus text format. Counters: `oso_tool_calls_total`
(label: `tool`, `ok`), `oso_errors_total` (label: `code`),
`oso_screenshots_total`, `oso_vlm_tokens_total`. Gauges:
`oso_active_traces`, `oso_snapshot_count`. Off by default
(`telemetry.metrics_enabled`).

---

## 24. CI / headless

`.github/workflows/ci.yml` runs:
1. `pip install -r requirements.txt -r requirements-dev.txt`.
2. `ruff check .`
3. `pytest tests/ -q`

No Docker, no Xvfb. Tests run in `--mock` or via direct module imports —
they never need a desktop session.

---

## 25. File / module layout

New / modified files:

```
selectors.py          # Selector AST, parser (XPath + CSS), resolver
errors.py             # Error code constants + Error class + http_status_for
diff.py               # Tree diff (custom + RFC 6902); tree hashing
session.py            # Single global session: step_ids, tree_tokens, snapshots
hashing.py            # Stable tree hash (excludes 'focused', timestamps)
redaction.py          # Pattern matching + text/tree/OCR/screenshot redaction
budgets.py            # Counter store, decorator
audit.py              # Append-only audit logger
tracing.py            # Trace recording (writer thread, JSONL + screenshots)
replay.py             # Trace replay (execute / verify, comparison table)
scenarios.py          # YAML loader, scenario state machine, scenario adapter
oracles.py            # assert_state predicates
tools.py              # Central tool implementations (callable from REST + MCP)
mac_adapter.py        # macOS pyobjc AX adapter (split out for size)
linux_adapter.py      # Linux pyatspi AT-SPI adapter
observer.py           # +window_uid, +get_monitors, +ElementOccluded check
mcp_server.py         # +new tool schemas, dispatch into tools.py
web_inspector.py      # +new REST routes
main.py               # +CLI flags for budgets, scenarios, redaction
description.py        # +max_tokens / focus_element / mode=auto support
config.json           # +new config blocks
requirements.txt      # +PyYAML; pyobjc/pyatspi documented as platform-optional
requirements-dev.txt  # pytest, pytest-cov, ruff
.github/workflows/ci.yml
tests/
  __init__.py
  conftest.py
  test_selectors.py
  test_diff.py
  test_hashing.py
  test_redaction.py
  test_session.py
  test_scenarios.py
  test_oracles.py
  test_tracing.py
  test_replay.py
  test_tools_mock.py
screen_observer_api_reference.md   # updated
```

Approximately 5–7 kLOC end state.

---

## 26. Phasing & acceptance

| Phase | Scope | Acceptance |
|---|---|---|
| **P1** | window_uid, selectors, element actions, error taxonomy, step IDs, capabilities, ActionReceipt, monitors | All new tools exposed on REST + MCP. Mock-mode tests for selector parser, find_element happy path + ambiguous, click_element happy + ElementNotFound + ElementDisabled, capabilities. |
| **P2** | wait_for, wait_idle, observe-with-diff (custom + JSON Patch), composite tools, snapshots, snapshot_diff | Diff round-trip tests; wait_for happy + timeout; composite tool returns receipt + observation. |
| **P3** | tree filtering, paging, screenshot crop, get_ocr, description budgeting, mode=auto | Filter test (roles, prune_empty); paging cursor stability test; crop test under mock. |
| **P4** | tracing, replay (execute + verify), scenarios (YAML loader + state machine + scenario adapter), oracles | Round-trip test: scenario → run agent moves → trace → replay verify with zero divergence. Oracle predicate tests. |
| **P5** | budgets, redaction, dry_run, allowlist, confirmation tokens, audit log | Budget trip test; redaction test for tree/OCR; dry_run does not invoke; confirmation flow happy + invalid. |
| **P6** | extra input verbs, telemetry, capabilities polish, real adapter polish | Verbs tested under mock; healthz/metrics smoke test. |

Each phase ships as one or more git commits on
`claude/plan-agentic-llm-features-82sCL`, never in parallel branches.

---

## 27. Migration & backwards compatibility

- `window_index` keeps working everywhere. New tools accept either, prefer
  `window_uid` when both are given.
- All response shapes are extended, not replaced. `success`/`error` (string)
  are emitted alongside `ok`/`error` (object), per D5.
- Existing MCP tool schemas are unchanged; all new params have defaults.
- `protocol_version: 2` in `get_capabilities()`. Today's behavior is
  protocol v1.

---

## 28. Out of scope

- Cross-machine remoting; sandbox/container orchestration; IDE plugins.
- Browser DOM access through DevTools.
- A new agent runtime.
- Localization of selector predicates / oracles.

---

## 29. Implementation policy

- Every public function: type hints; docstring summarizes contract; no
  multi-paragraph docs.
- Errors produced via `errors.Error.raise_or_dict(...)`, never bare strings.
- Every new tool has at least one mock-mode test.
- No new dependency without justification in commit message.
- Keep modules under ~600 LOC; split when growing past that.
- All file paths in code use absolute paths to `os.path.join(repo_root, …)`
  to keep tests hermetic.

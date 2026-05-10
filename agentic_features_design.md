# OSScreenObserver — Agentic LLM & Harness Feature Design

Status: **Draft / planning only.** No code in this branch implements the features
below; this document is the blueprint.

Audience: maintainers of OSScreenObserver, authors of agent harnesses that
target it, and reviewers deciding what to build next.

Companion to: `README.md` (user-facing), `screen_observer_api_reference.md`
(current REST + MCP surface).

---

## 1. Goals

1. Make the system **reliable enough for agentic loops** — every action returns
   enough signal that the LLM can decide what to do next without guessing.
2. Make the system **cheap enough for long sessions** — diffs, crops, and
   filters keep token spend bounded as the trajectory grows.
3. Make the system **a first-class evaluation substrate** — record/replay,
   scripted mock scenarios, and declarative oracles let harnesses score agents
   reproducibly.
4. Keep the **two-interface invariant**: every new capability is exposed
   simultaneously through the REST API (`web_inspector.py`) and the MCP server
   (`mcp_server.py`), backed by shared logic on `ScreenObserver` /
   `DescriptionGenerator` / `ASCIIRenderer`.

## 2. Non-goals

- Cross-machine remoting, sandboxing, or container orchestration.
- A new agent runtime. `window_agent.py` stays a reference client; production
  harnesses bring their own loop.
- Replacing UIA / AX / AT-SPI with a custom inspector. We layer on top.
- Web-app DOM access. Browser windows are observed through the OS
  accessibility tree only.

## 3. Design principles

- **Two interfaces, one core.** Every feature lands as a method on
  `ScreenObserver` (or a new sibling class) and is wired into both
  `mcp_server.py`'s `_TOOLS` table and `web_inspector.py`'s Flask routes.
- **Stable IDs over positional indexes.** Positional `window_index` and
  per-call `id` integers stay for backwards compat, but every new tool also
  accepts a stable `window_uid` and `element_selector`.
- **Diffs over snapshots.** Anything that returns a tree, OCR text, or visible
  regions accepts a `since=<token>` and may return a delta.
- **Receipts on every action.** Every input tool returns a structured
  `ActionReceipt` — never just `{"success": true}`.
- **Errors are data.** A typed error taxonomy with `recoverable` and
  `suggested_next_tool` so agents can branch programmatically.
- **Mock parity.** Every new feature works under `--mock`. If it can't, it
  isn't shipped.

## 4. Glossary

| Term | Definition |
|---|---|
| `window_uid` | Stable opaque string identifying a window across calls; on Windows derived from `(pid, hwnd)`, on macOS from `kCGWindowNumber`, on Linux from `wmctrl` window id. Survives focus changes; invalidated when the window closes. |
| `element_selector` | String of the form `role[name="…"]/role[name="…"]/…` — an ancestry path through the accessibility tree, robust to id renumbering. |
| `tree_token` | Opaque cursor returned by any tree-producing tool; pass it back via `since=` to get only what changed. |
| `snapshot_id` | Server-side handle pointing to a frozen `(window list, tree, screenshot, OCR text, timestamp)` tuple. |
| `step_id` | Monotonic per-session integer assigned to every tool call; appears on every result. |
| `trace` | JSONL stream of `(step_id, tool, args, result_summary, screenshot_ref?)` rows. |
| `scenario` | A YAML/JSON file consumed by mock mode that scripts initial state plus reactions to actions. |
| `ActionReceipt` | Structured response from any input tool — see §6. |

---

## 5. Stable identity & selectors

### 5.1 `window_uid`

**Motivation.** README §"Available MCP Tools" already warns the
positional `window_index` "may change between calls". Agents that store an
index across steps mis-target.

**Behavior.**
- `list_windows` gains a `window_uid` field per entry.
- Every tool that currently takes `window_index` also accepts `window_uid`.
  Exactly one of the two must be supplied; if both, `window_uid` wins and a
  warning is included in the response.
- `window_uid` is stable until the window closes. Reopening the same app
  produces a new uid.

**Implementation.** Extend `WindowInfo` in `observer.py`. Each adapter
populates the field:
- Windows: `f"win:{pid}:{hwnd}"`.
- macOS: `f"mac:{cg_window_number}"`.
- Linux: `f"x11:{wmctrl_id}"`.
- Mock: `f"mock:{index}:{uuid4().hex[:8]}"` generated at adapter init.

**Errors.** If a uid no longer resolves: `WindowGone` (see §22).

### 5.2 `element_selector`

**Motivation.** Element `id` integers reset when the tree is re-walked. A
selector lets the agent persist a target across `observe` calls.

**Format.** `role[name="literal"]/role[name~="regex"]/role[index=2]`
- Bracket predicates are `name=`, `name~=` (regex), `value=`,
  `keyboard_shortcut=`, `index=` (zero-based among siblings of the same role).
- Empty predicate brackets are allowed: `Window/Group/Button[index=0]`.
- The leading segment is matched against the window root.

**API.** `find_element(window_uid, selector)` returns `{element_id, bounds,
ambiguous_matches: int}`. `ambiguous_matches > 1` flags brittle selectors.

**Implementation.** Pure function over the tree returned by
`Adapter.get_element_tree`; no platform-specific code.

---

## 6. Element-targeted actions

**Motivation.** Coordinate-based `click_at` requires the agent to do bbox
math and breaks under any layout shift. Element-targeted verbs solve both.

**New tools.**

| MCP tool | REST | Params |
|---|---|---|
| `click_element` | `POST /api/element/click` | `window_uid`, `element_id` or `selector`, `button=left\|right\|middle`, `count=1\|2` |
| `focus_element` | `POST /api/element/focus` | window + element id/selector |
| `set_value` | `POST /api/element/set_value` | window + element + `value`, `clear_first=true` |
| `invoke_element` | `POST /api/element/invoke` | window + element — uses UIA `Invoke` pattern when available, falls back to a center-click |
| `select_option` | `POST /api/element/select` | window + element + `option_name` or `option_index` for combo boxes / lists |

**Response (all of the above).** An `ActionReceipt`:

```json
{
  "step_id": 42,
  "ok": true,
  "action": "click_element",
  "target": {
    "window_uid": "win:1234:0xabc",
    "element_id": 17,
    "selector": "Window/Pane/Button[name=\"OK\"]",
    "bounds": {"x": 320, "y": 480, "width": 80, "height": 28}
  },
  "before": {"tree_hash": "sha1:…", "focused_selector": "…"},
  "after":  {"tree_hash": "sha1:…", "focused_selector": "…"},
  "changed": true,
  "new_dialogs": [{"window_uid": "…", "title": "Confirm"}],
  "duration_ms": 184
}
```

**Errors.** `ElementNotFound`, `ElementOccluded`, `ElementDisabled`,
`PatternUnsupported` (e.g. `set_value` on a label).

**Implementation.** Layer on top of `Adapter.click_at`. On Windows, prefer
UIA patterns (`InvokePattern`, `ValuePattern`, `SelectionItemPattern`) over
synthetic clicks; fall back to `bring_to_foreground` + center-click.

---

## 7. Richer input verbs

Beyond click/type/key/scroll the agent currently has, add:

| Tool | Purpose |
|---|---|
| `hover_at` / `hover_element` | Surfaces tooltips and hover-only menus. |
| `right_click_at` / `right_click_element` | Context menus. |
| `double_click_at` / `double_click_element` | List-item activation. |
| `drag` | `from={x,y\|element}`, `to={x,y\|element}`, `modifiers=[]`. |
| `key_into_element` | Focus an element first, then `press_key`, atomically. |
| `clear_text` | Select-all + delete on a focused field. |

All return the standard `ActionReceipt`. `drag` adds `path` (sampled
intermediate points) for trace fidelity.

---

## 8. Tree filtering & paging

**Motivation.** Browser windows can have thousands of UIA nodes. Agents pay
the token cost even when they only need buttons.

**`get_window_structure` gains:**

```jsonc
{
  "window_uid": "win:…",
  "roles": ["Button", "Edit", "Hyperlink"],   // include-list
  "exclude_roles": ["Image"],                  // optional
  "visible_only": true,                        // intersect with visible_areas
  "name_regex": "Save|Submit",                 // optional
  "max_text_len": 80,                          // truncate value/name
  "prune_empty": true,                         // drop subtrees with no matches
  "max_nodes": 500,                            // hard cap
  "page_cursor": null                          // pagination
}
```

**Response.** Adds `truncated: true|false` and `next_cursor`. When
`prune_empty` is set, surviving branches keep their ancestry so selectors
remain valid.

**Implementation.** Filtering is a post-walk pass in
`observer.ScreenObserver.get_window_structure`; adapters are unchanged.

---

## 9. Cropped & token-budgeted perception

### 9.1 `get_screenshot` cropping

```jsonc
{
  "window_uid": "win:…",
  "element_id": 17,           // OR
  "bbox": {"x":..,"y":..,"width":..,"height":..},
  "padding_px": 8,
  "max_width": 1024            // downscale cap; preserves aspect ratio
}
```

Returns the same shape as today plus `source_bbox` (the absolute pixel
rectangle that was captured) so VLM callers can map outputs back.

### 9.2 `get_screen_description` budgeting

Adds:

- `max_tokens` — hard cap; description is truncated with a `… [truncated]`
  marker.
- `focus_element` — biases the prose / OCR / VLM prompt to that subtree
  (passed as a hint to the VLM and as a filter to the OCR overlay).
- `mode="auto"` — server picks accessibility-only when the tree is rich,
  OCR when sparse, VLM when both are weak. The chosen mode is reported in
  the response.

### 9.3 OCR region

`get_ocr` (new) — runs OCR over a single bbox / element instead of the whole
window, returning `[{text, confidence, bbox}]`. Cheaper and more useful for
agents than a full prose blob.

---

## 10. Wait & synchronization

### 10.1 `wait_for`

```jsonc
{
  "window_uid": "win:…",          // optional; default = any window
  "any_of": [
    {"type": "element_appears", "selector": "Window/.../Button[name=\"OK\"]"},
    {"type": "element_disappears", "element_id": 17},
    {"type": "text_visible", "regex": "Saved"},
    {"type": "window_appears", "title_regex": "Confirm"},
    {"type": "tree_changes", "since": "<tree_token>"},
    {"type": "focused_changes"}
  ],
  "timeout_ms": 5000,
  "poll_ms": 200
}
```

Returns `{matched_index, matched_detail, elapsed_ms}` or
`{matched_index: null, elapsed_ms, last_observation}` on timeout.

**Implementation.** Server-side polling loop using existing observation
methods. Caps total wait so a stuck agent can't hang the server (`max
timeout_ms = 60000` configurable).

### 10.2 `wait_idle`

Heuristic: returns once the tree hash has been stable for `quiet_ms`
(default 750) or `timeout_ms` is reached. Cheap "page settled" signal.

---

## 11. Observe-with-diff

**Problem.** `observe_window` returns the full tree every call. After 30
steps, the agent has paid for the same tree 30 times.

**Mechanism.** Every tree-producing tool returns a `tree_token`. Passing
`since=<tree_token>` returns:

```jsonc
{
  "window_uid": "win:…",
  "tree_token": "tt:abc123",        // new token
  "base_token": "tt:prev",
  "changes": [
    {"op": "add",     "path": "0/2/3", "node": {...}},
    {"op": "remove",  "path": "0/4"},
    {"op": "replace", "path": "0/2/3", "fields": {"value": "hello"}},
    {"op": "move",    "from": "0/5", "to": "0/2/4"}
  ],
  "unchanged": false
}
```

`path` is a slash-delimited child-index trail. `unchanged: true` means an
empty `changes` array — useful as a heartbeat.

Same pattern applies to:
- `get_screen_description(since=…)` — returns `unchanged: true` when the
  description hash matches.
- `get_visible_areas(since=…)` — returns only added/removed regions.
- `get_ocr(since=…)` — returns added/removed text fragments.

**Implementation.** A per-session ring buffer keyed by `tree_token` storing
the last N (~16) trees per window. On `since=` lookup, compute a JSON-Patch
style diff. If the token has expired, return the full tree with
`base_token: null`.

---

## 12. Composite action+observe

```text
click_element_and_observe   — click_element, then observe_window(since=…)
type_and_observe            — type_text, then observe_window(since=…)
press_key_and_observe       — press_key, then observe_window(since=…)
```

One round-trip. Response = `ActionReceipt` with `observation` field
embedded. Saves a tool call per step, ~halving trajectory length on
typical tasks.

A single `wait_after_ms` parameter (default 200) bridges the action and the
observation — enough for most UIs to settle without a full `wait_idle`.

---

## 13. Snapshots & diffs

```text
snapshot()                  → {snapshot_id, summary}
snapshot_get(snapshot_id)   → {windows, tree_per_window, screenshot_refs, ocr}
snapshot_diff(a, b)         → {windows_added, windows_removed, per_window_changes}
snapshot_drop(snapshot_id)  → {ok}
```

Snapshots live in memory with a TTL (default 5 minutes) and an LRU cap
(default 32). Used by both agents (rollback reasoning: "what did this look
like before I clicked Save?") and harnesses (oracle inputs).

`snapshot_diff` reuses the §11 diff machinery so the output format is
consistent across the API.

---

## 14. Trace recording

```text
trace_start(label?)  → {trace_id, started_at}
trace_stop(trace_id) → {trace_id, path, step_count, duration_ms}
trace_status()       → {active_trace_id?, step_count}
```

While active, every tool call (including failed ones) is appended as one
JSONL line:

```jsonc
{
  "step_id": 17,
  "ts": "2026-05-10T14:05:01.234Z",
  "caller": "mcp:claude-desktop",        // or "rest:127.0.0.1"
  "tool": "click_element",
  "args": {...},
  "result_summary": {"ok": true, "changed": true},
  "screenshot_ref": "trace-abc/step-00017.png",  // optional, see config
  "tree_hash_before": "sha1:…",
  "tree_hash_after":  "sha1:…",
  "duration_ms": 184
}
```

**Config.** New `tracing` block in `config.json`:

```jsonc
{
  "tracing": {
    "dir": "./traces",
    "screenshot_every_n_actions": 5,   // 0 = never, 1 = every action
    "max_args_bytes": 4096,            // truncate large args
    "redact_keys": ["api_key", "password", "Authorization"]
  }
}
```

**Privacy.** Trace files inherit the redaction rules in §20.

---

## 15. Trace replay

```text
replay_start(path, mode="execute"|"verify", on_divergence="stop"|"warn"|"resume")
replay_step()        → advance one step
replay_status()      → {position, total, divergences[]}
replay_stop()
```

Modes:
- `execute` — re-issue each tool call, ignore recorded result, emit a fresh
  trace.
- `verify` — re-issue each tool call, compare result hash to the recorded
  one, record a `divergence` row when they differ. The harness's main
  regression-test mode.

**Determinism notes.** Because window indexes and bounds are not stable
across runs, `verify` compares by `window_uid_kind` (the prefix:
`win:` / `mac:` / `x11:` / `mock:`) plus title regex, not by raw uid. For
hermetic eval we rely on `--mock` (§16).

---

## 16. Mock scenario DSL

**Motivation.** Today `--mock` produces a static fake. Harnesses need
scripted state transitions: clicking "Login" should yield a "Welcome"
screen.

**Format** (`scenarios/login.yaml`):

```yaml
name: login-happy-path
windows:
  - uid: mock:app
    title: "Acme Login"
    geometry: {x: 100, y: 100, width: 800, height: 600}
    tree:
      role: Window
      children:
        - {role: Edit, name: "Username", id: u}
        - {role: Edit, name: "Password", id: p, value: "", secret: true}
        - {role: Button, name: "Login", id: btn}

reactions:
  - on: {tool: type_text, target_id: u, text: "alice"}
    set: {id: u, value: "alice"}
  - on: {tool: type_text, target_id: p, text_regex: ".+"}
    set: {id: p, value: "<filled>"}
  - on: {tool: click_element, target_id: btn}
    when: {id: u, value: "alice"}
    transition_to: welcome

states:
  welcome:
    windows:
      - uid: mock:app
        title: "Acme — Welcome"
        tree:
          role: Window
          children:
            - {role: Text, name: "Hello, alice"}

oracles:
  success:
    - {kind: text_visible, regex: "Hello, alice"}
  failure:
    - {kind: window_appears, title_regex: "Error"}
```

**Loading.**

```bash
python main.py --mock --scenario scenarios/login.yaml
```

**Implementation.** A new `MockScenarioAdapter` in `observer.py` that
extends the existing mock adapter. Reactions are pattern-matched against
incoming action calls before they reach the no-op input layer.

---

## 17. Eval oracles

**Motivation.** Harnesses currently re-implement assertions in glue code.
Move them into the server so agents and harnesses share one vocabulary.

**Tool.** `assert_state(predicate)` — `predicate` is a list (AND) of:

| Kind | Args |
|---|---|
| `element_exists` | `selector`, `window_uid?` |
| `element_absent` | `selector`, `window_uid?` |
| `value_equals` | `selector`, `expected` |
| `value_matches` | `selector`, `regex` |
| `text_visible` | `regex`, `window_uid?` |
| `window_focused` | `title_regex` |
| `tree_hash_equals` | `expected_hash` |
| `screenshot_similar` | `reference_path`, `min_ssim=0.95` |

Returns `{ok: bool, failures: [{kind, args, observed}]}`. Never raises;
the harness branches on `ok`.

`screenshot_similar` requires `pillow` and `scikit-image` as optional
dependencies; it's gated on availability and returns
`PredicateUnsupported` otherwise.

---

## 18. Rate / budget controls

**Per-session limits**, configured at start (CLI flags or MCP-handshake
`initialization_options`):

| Limit | Default | Trip behavior |
|---|---|---|
| `max_actions` | unlimited | Subsequent input tools return `BudgetExceeded`. |
| `max_screenshots` | unlimited | `get_screenshot*` return `BudgetExceeded`. |
| `max_vlm_tokens` | unlimited | `get_screen_description(mode=vlm)` returns `BudgetExceeded`. |
| `max_session_seconds` | unlimited | All tools return `BudgetExceeded`. |
| `actions_per_minute` | unlimited | Sliding window; `RateLimited` (recoverable) when tripped. |

Status tool: `get_budget_status()` returns remaining counts, so agents can
self-pace.

---

## 19. Step IDs & causality

Every tool result includes:

```jsonc
{
  "step_id": 42,
  "caused_by_step_id": 41   // null for read-only or first-of-session
}
```

`caused_by_step_id` is the most recent input action when the tool is
read-only; it is its own `step_id` when the tool is an input. This lets a
harness reconstruct a trajectory graph without parsing the trace.

---

## 20. Sensitive-region redaction

**Motivation.** README §"Known Limitations" already calls out prompt
injection; the same machinery hides credentials and PII.

**Config.**

```jsonc
{
  "redaction": {
    "window_title_patterns": ["1Password", "Bitwarden"],
    "element_name_patterns":  ["Password", "PIN", "SSN"],
    "element_role_patterns":  ["PasswordEdit"],
    "ocr_text_patterns":      ["\\b\\d{3}-\\d{2}-\\d{4}\\b"],
    "replacement": "[REDACTED]"
  }
}
```

**Effects.**
- Tree nodes matching the patterns have `name`/`value` replaced before
  serialization.
- OCR output passes through a regex sweep.
- VLM prompts include a "do not transcribe content of any field whose role
  is …" preamble.
- Screenshots have matched bboxes drawn over with solid black before being
  returned. (Optional, `redaction.blur_screenshots: true`.)
- The redaction list itself is logged to the trace, not the redacted
  content.

---

## 21. Dry-run, allowlist, and confirmation tokens

### 21.1 Dry-run

Every input tool accepts `dry_run: true`. Server resolves the target,
performs all checks (occlusion, enabled, pattern support), and returns the
`ActionReceipt` it *would* return — but never invokes the platform layer.
`receipt.ok: true, receipt.dry_run: true, receipt.changed: false`.

### 21.2 Action allowlist

Config:

```jsonc
{
  "actions": {
    "allow": ["click_element", "focus_element", "wait_for"],
    "deny":  ["press_key", "type_text"],
    "default": "deny"
  }
}
```

Mismatches return `PermissionDenied` (non-recoverable).

### 21.3 Confirmation tokens (for destructive verbs)

Config flags certain element predicates as destructive:

```jsonc
{
  "confirmation_required": [
    {"name_regex": "(?i)delete|remove|send|pay|sign"},
    {"role": "Button", "name_regex": "(?i)submit"}
  ]
}
```

Flow:
1. Agent calls `propose_action(action, args)` → server returns
   `{confirm_token, expires_at, would_target: {...}}`.
2. Agent calls the actual action with `confirm_token=…`. Token is
   single-use and bound to the resolved element.
3. Without a token, the action returns `ConfirmationRequired` (recoverable
   — the suggested next tool is `propose_action`).

---

## 22. Structured error taxonomy

All errors share:

```jsonc
{
  "ok": false,
  "error": {
    "code": "ElementNotFound",
    "message": "No element matches selector …",
    "recoverable": true,
    "suggested_next_tool": "find_element",
    "context": {"selector": "...", "window_uid": "..."}
  },
  "step_id": 99
}
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
| `SnapshotExpired` | yes | `snapshot` (re-take) |
| `ScenarioInvalid` | no | — |
| `Internal` | no | — |

Today's free-text error strings are mapped onto this taxonomy by a
translation table in `observer.py`; legacy callers see the same HTTP
status codes.

---

## 23. Capability discovery

`get_capabilities()` returns:

```jsonc
{
  "platform": "Linux",
  "adapter": "linux-stub",
  "supports": {
    "accessibility_tree": false,
    "uia_invoke": false,
    "drag": true,
    "ocr": true,
    "vlm": true,
    "occlusion_detection": false,
    "multi_monitor": true,
    "redaction": true,
    "scenarios": false
  },
  "config": {
    "tree_max_depth": 8,
    "ascii_grid": {"width": 110, "height": 38}
  },
  "version": "0.2.0"
}
```

Agents call this once at session start and adapt their tool selection.
Harnesses check it before running platform-specific scenarios.

---

## 24. Multi-monitor / DPI

`list_windows` adds:

```jsonc
{
  "monitor_index": 1,
  "monitor_bounds": {"x":-1920,"y":0,"width":1920,"height":1080},
  "scale_factor": 1.5,
  "logical_bounds":  {"x":0,"y":0,"width":1280,"height":720},
  "physical_bounds": {"x":0,"y":0,"width":1920,"height":1080}
}
```

A new `get_monitors()` tool enumerates all monitors. Coordinates passed to
`click_at` are documented as **physical** pixels by default; a
`coordinate_space: "logical"` parameter is accepted for high-DPI safety.

---

## 25. Audit log

Append-only log at `audit.log` (path configurable). Format:

```text
2026-05-10T14:05:01.234Z step=42 caller=mcp:claude-desktop tool=click_element
  args.window_uid=win:1234:0xabc args.element_id=17 ok=true changed=true
  redactions=[args.text]
```

One line per call. Args are serialized after redaction. Distinct from
traces: traces are per-session and JSONL; the audit log is process-lifetime
and human-readable, never truncated.

Configurable rotation (`logging.audit_max_bytes`, `audit_backups`). Off by
default; opt-in via `logging.audit: true`.

---

## 26. Headless / CI mode

Document and ship:

1. `docs/ci.md` (new) covering the full Xvfb recipe for Linux.
2. `Dockerfile.ci` running `python main.py --mock --scenario … --mode both`
   on top of `python:3.11-slim` plus `wmctrl`. No GUI required.
3. `docker-compose.ci.yml` wiring the server alongside an agent harness
   container, sharing a volume for traces.
4. A `--no-action-side-effects` flag that forces every input tool into
   `dry_run` (useful when running scenarios that exist only to test the
   agent's plan).

---

## 27. Telemetry & observability (minor)

- `/api/healthz` — `{ok, uptime_s, adapter, version}`.
- `/api/metrics` — Prometheus-format counters: tool calls, errors per code,
  trace bytes written, OCR latency p50/p95, VLM tokens. Off by default.
- `step_id` and `tool` propagate to the existing `logger` so log scrapers
  can correlate.

---

## 28. Phasing

The list is long; this is the proposed rollout order. Each phase is
independently shippable and additive (no breaking changes — see §29).

| Phase | Includes | Rationale |
|---|---|---|
| **P1 — Identity & receipts** | §5 (`window_uid`, selectors), §6 (element actions), §22 (error taxonomy), §19 (step_id) | Foundation everything else builds on. Biggest reliability win for agents. |
| **P2 — Sync & diff** | §10 (`wait_for`, `wait_idle`), §11 (observe-diff), §12 (composite tools), §13 (snapshots) | Biggest token / latency win. Cheap relative to P1. |
| **P3 — Filters & crops** | §8 (tree filtering), §9 (cropping, budgeting, region OCR), §24 (multi-monitor) | Quality-of-life. Independent of P1/P2. |
| **P4 — Harness substrate** | §14 (trace), §15 (replay), §16 (scenarios), §17 (oracles), §26 (CI) | Unblocks CI evaluation. Largest in scope. |
| **P5 — Safety** | §18 (budgets), §20 (redaction), §21 (dry-run / allowlist / confirm), §25 (audit) | Required before exposing to untrusted agents. Can ship piecemeal. |
| **P6 — Misc** | §7 (extra verbs), §23 (capabilities), §27 (telemetry) | Polish. |

If only three phases get built, P1 + P2 + P4 deliver the majority of the
value: reliable agents, cheap loops, replayable evals.

---

## 29. Migration & backwards compatibility

- `window_index` keeps working. Tools accept either `window_index` or
  `window_uid`; if both are present, `window_uid` wins.
- Existing endpoints' response shapes are extended, not replaced. New
  fields (`window_uid`, `step_id`, `error.code`) are additive.
- `success: true` style responses remain on legacy endpoints; new endpoints
  use `ok: true` to make the distinction visible.
- The MCP `_TOOLS` table grows; existing tool schemas are unchanged
  (parameters can be added with defaults so older clients keep working).
- A new top-level `protocol_version` in `get_capabilities()` lets harness
  authors gate features cleanly.

---

## 30. Open questions

1. **Selector grammar.** Adopt CSS-like syntax (familiar to web devs) vs.
   XPath-like (familiar to UIA tooling) vs. our own. The draft above is a
   simplified XPath. Pick one before P1 ships.
2. **Diff format.** RFC 6902 JSON Patch vs. the custom `{op, path, …}`
   shape sketched in §11. JSON Patch has libraries; the custom shape is
   more readable and lets us include `move` cheaply.
3. **Scenario language.** YAML (readable, popular) vs. Python (full
   expressiveness, no DSL to learn) vs. JSON (zero-dep). YAML is the draft;
   revisit if reactions need real logic.
4. **Trace storage.** Local JSONL only, or pluggable backends (SQLite,
   S3)? Defer to harness needs; keep the file-writing layer behind an
   interface.
5. **Confirmation token UX.** Should the server expose a separate
   "preview" endpoint that returns the bbox + screenshot of the would-be
   target, so a human-in-the-loop can confirm visually? Probably yes for
   P5, but it's outside the strict agent flow.
6. **Selector ambiguity policy.** When `find_element` matches >1 node:
   error vs. return the first vs. return all. Draft picks "return first +
   `ambiguous_matches` count"; revisit if it causes silent mistakes.
7. **VLM cost accounting.** §18 caps VLM tokens, but the cap counts only
   request tokens (we don't see Anthropic's response tokens until after).
   Approximate by `max_tokens` parameter — accept the small over-count.

---

## 31. Out of scope (explicitly)

- Non-OS surfaces: web pages via DevTools, Android/iOS device farms,
  remote VMs.
- LLM choice: this server stays model-agnostic. The current Anthropic VLM
  integration is one description backend, not the system's identity.
- A built-in agent loop. `window_agent.py` is illustrative; harness authors
  bring their own controller.

---

## 32. Acceptance criteria per phase

A phase is "done" when:

- All new tools are exposed on **both** REST and MCP.
- All new tools work under `--mock` (P4 makes this stronger via scenarios).
- `screen_observer_api_reference.md` is updated with request/response
  schemas and at least one example per tool.
- An end-to-end test in the existing test layout (or a new
  `tests/agentic/` directory) exercises the happy path and at least one
  error code per new tool.
- Trace + replay (after P4) round-trips at least one realistic scenario
  with zero divergences.

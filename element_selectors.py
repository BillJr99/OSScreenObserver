"""
element_selectors.py — Element selector parser and resolver.

Supports two grammars sharing a common AST (design doc D3, §6.2):

  XPath-ish (default; starts with role token):
      Window[name="Notepad"]/Pane/Button[name="OK"]
      Window/*/Button[index=0]
      Document[focused=true]

  CSS-ish (whitespace = descendant, > = child):
      Window > Pane Button[name="OK"]
      Window > * Button:nth-of-type(1)

Predicates (both grammars):
      name="literal"
      name~="regex"           anchored full-match
      value="..."  / value~="..."
      role="..."
      keyboard_shortcut="..."
      enabled=true / enabled=false
      focused=true / focused=false
      index=N                 zero-based among same-role siblings under parent
      :nth-of-type(N)         CSS spelling of [index=N-1]

Resolver walks the *unfiltered* tree (filters from §10 are display-only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ─── AST ──────────────────────────────────────────────────────────────────────

@dataclass
class Predicate:
    key: str
    op: str        # '=' or '~='
    value: Any     # str | bool | int

    def matches(self, elem: Any, sibling_same_role_index: int) -> bool:
        v: Any
        if self.key == "name":
            v = elem.name or ""
        elif self.key == "value":
            v = elem.value or ""
        elif self.key == "role":
            v = elem.role or ""
        elif self.key == "keyboard_shortcut":
            v = elem.keyboard_shortcut or ""
        elif self.key == "enabled":
            return bool(elem.enabled) is bool(self.value)
        elif self.key == "focused":
            return bool(elem.focused) is bool(self.value)
        elif self.key == "index":
            try:
                return int(sibling_same_role_index) == int(self.value)
            except (TypeError, ValueError):
                return False
        else:
            return False

        if self.op == "=":
            return v == self.value
        if self.op == "~=":
            try:
                return re.fullmatch(str(self.value), str(v)) is not None
            except re.error:
                return False
        return False


@dataclass
class Step:
    role: str                    # '*' for any
    predicates: List[Predicate] = field(default_factory=list)
    axis: str = "child"          # 'child' | 'descendant'

    def matches(self, elem: Any, idx: int) -> bool:
        if self.role != "*" and (elem.role or "") != self.role:
            return False
        for p in self.predicates:
            if not p.matches(elem, idx):
                return False
        return True


@dataclass
class Selector:
    steps: List[Step]
    grammar: str                 # 'xpath' | 'css'
    raw: str = ""

    def __str__(self) -> str:
        return self.raw or self.canonical()

    def canonical(self) -> str:
        out = []
        for i, s in enumerate(self.steps):
            sep = "/" if i > 0 else ""
            preds = "".join(_format_pred(p) for p in s.predicates)
            out.append(f"{sep}{s.role}{preds}")
        return "".join(out)


def _format_pred(p: Predicate) -> str:
    if isinstance(p.value, bool):
        return f'[{p.key}={"true" if p.value else "false"}]'
    if isinstance(p.value, int) and p.key == "index":
        return f"[{p.key}={p.value}]"
    return f'[{p.key}{p.op}"{p.value}"]'


# ─── Parser ───────────────────────────────────────────────────────────────────

class SelectorParseError(ValueError):
    pass


# Predicates: [key], [key="val"], [key~="re"], [key=true|false], [index=3]
_PRED_RE = re.compile(
    r"""\[\s*
        (?P<key>[A-Za-z_]\w*)\s*
        (?:
            (?P<op>~?=)\s*
            (?:
                "(?P<sval>(?:[^"\\]|\\.)*)" |
                (?P<bval>true|false)        |
                (?P<ival>-?\d+)
            )
        )?\s*
    \]""",
    re.VERBOSE,
)


def parse(text: str) -> Selector:
    s = (text or "").strip()
    if not s:
        raise SelectorParseError("empty selector")

    if _looks_css(s):
        return _parse_css(s)
    return _parse_xpath(s)


def _looks_css(s: str) -> bool:
    # CSS has '>' combinators or whitespace between identifiers (and no '/').
    if ">" in s and "/" not in s:
        return True
    # Pattern like "Window Pane Button" with bare whitespace and no '/'.
    if "/" not in s and re.search(r"[A-Za-z_*]\s+[A-Za-z_*]", s):
        return True
    if ":nth-of-type" in s:
        return True
    return False


# ── XPath-ish ────────────────────────────────────────────────────────────────

def _parse_xpath(text: str) -> Selector:
    parts = _split_top_level(text, "/")
    steps: List[Step] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        role, preds = _parse_role_and_preds(part)
        steps.append(Step(role=role, predicates=preds, axis="child"))
    if not steps:
        raise SelectorParseError(f"no steps parsed from {text!r}")
    return Selector(steps=steps, grammar="xpath", raw=text)


# ── CSS-ish ──────────────────────────────────────────────────────────────────

def _parse_css(text: str) -> Selector:
    # Tokenise into role-with-preds and combinators.
    # Combinators: '>' (direct child), whitespace (descendant).
    tokens: List[Tuple[str, str]] = []  # (kind, value): ('comb', '>'|' '), ('step', '...')
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            # Look ahead: a '>' makes it a child combinator (consume the >).
            if j < n and text[j] == ">":
                tokens.append(("comb", ">"))
                j += 1
                while j < n and text[j].isspace():
                    j += 1
            else:
                if tokens and tokens[-1][0] == "step":
                    tokens.append(("comb", " "))
            i = j
            continue
        if c == ">":
            tokens.append(("comb", ">"))
            i += 1
            continue
        # Step: role plus optional [pred][pred]...:pseudo
        j = i
        while j < n and text[j] not in (" ", "\t", "\n", ">"):
            if text[j] == "[":
                # consume to matching ]
                depth = 0
                while j < n:
                    if text[j] == "[":
                        depth += 1
                    elif text[j] == "]":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                continue
            j += 1
        token = text[i:j]
        tokens.append(("step", token))
        i = j

    steps: List[Step] = []
    pending_axis = "child"
    for kind, val in tokens:
        if kind == "comb":
            pending_axis = "child" if val == ">" else "descendant"
            continue
        # Translate :nth-of-type(N) → [index=N-1]
        nth = re.search(r":nth-of-type\((\d+)\)", val)
        if nth:
            idx = int(nth.group(1)) - 1
            val = re.sub(r":nth-of-type\(\d+\)", f"[index={idx}]", val)
        role, preds = _parse_role_and_preds(val)
        steps.append(Step(role=role, predicates=preds, axis=pending_axis))
        pending_axis = "child"

    if not steps:
        raise SelectorParseError(f"no steps parsed from {text!r}")
    # First step has no predecessor — its axis is irrelevant; force 'child'
    # so the resolver treats it as "match against the root step".
    steps[0].axis = "child"
    return Selector(steps=steps, grammar="css", raw=text)


# ── Shared ───────────────────────────────────────────────────────────────────

def _parse_role_and_preds(token: str) -> Tuple[str, List[Predicate]]:
    """Split 'Role[k=v][k2~="re"]' into ('Role', [Pred,…])."""
    m = re.match(r"\s*([A-Za-z_*][\w*]*)\s*", token)
    if not m:
        raise SelectorParseError(f"cannot parse step role from {token!r}")
    role = m.group(1)
    rest = token[m.end():]
    preds: List[Predicate] = []
    pos = 0
    while pos < len(rest):
        if rest[pos].isspace():
            pos += 1
            continue
        pm = _PRED_RE.match(rest, pos)
        if not pm:
            raise SelectorParseError(
                f"cannot parse predicate at {rest[pos:]!r} in {token!r}"
            )
        key = pm.group("key")
        op = pm.group("op") or "="
        if pm.group("sval") is not None:
            val: Any = _unescape(pm.group("sval"))
        elif pm.group("bval") is not None:
            val = (pm.group("bval") == "true")
        elif pm.group("ival") is not None:
            val = int(pm.group("ival"))
        else:
            # Bare [key] — treated as exists (unsupported here)
            raise SelectorParseError(f"predicate {key!r} requires a value")
        preds.append(Predicate(key=key, op=op, value=val))
        pos = pm.end()
    return role, preds


def _unescape(s: str) -> str:
    return s.replace(r"\"", '"').replace(r"\\", "\\")


def _split_top_level(text: str, sep: str) -> List[str]:
    """Split on sep but skip occurrences inside [ ... ] brackets."""
    out: List[str] = []
    depth = 0
    buf: List[str] = []
    for c in text:
        if c == "[":
            depth += 1
        elif c == "]":
            depth = max(0, depth - 1)
        if c == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    out.append("".join(buf))
    return out


# ─── Resolver ─────────────────────────────────────────────────────────────────

@dataclass
class ResolveResult:
    matches: List[Any]              # list of UIElement
    ambiguous: bool


def resolve(root: Any, selector: Selector, *, max_matches: int = 10) -> ResolveResult:
    """
    Resolve *selector* against *root* (a UIElement).  The first step is
    matched against root itself.  Subsequent steps walk children (axis=child)
    or all descendants (axis=descendant).
    """
    if not selector.steps:
        return ResolveResult(matches=[], ambiguous=False)

    first = selector.steps[0]
    same_role_idx = 0  # root has no siblings; index=0 always
    if not first.matches(root, same_role_idx):
        return ResolveResult(matches=[], ambiguous=False)

    if len(selector.steps) == 1:
        return ResolveResult(matches=[root], ambiguous=False)

    # Walk the rest.
    current: List[Any] = [root]
    for step in selector.steps[1:]:
        next_matches: List[Any] = []
        for parent in current:
            candidates = (parent.children
                          if step.axis == "child"
                          else _descendants(parent))
            # Compute same-role index per parent group.
            role_counter: Dict[str, int] = {}
            for child in candidates:
                idx = role_counter.get(child.role, 0)
                role_counter[child.role] = idx + 1
                if step.matches(child, idx):
                    next_matches.append(child)
        current = next_matches
        if not current:
            return ResolveResult(matches=[], ambiguous=False)

    truncated = current[:max_matches]
    return ResolveResult(matches=truncated, ambiguous=len(current) > 1)


def _descendants(elem: Any) -> List[Any]:
    out: List[Any] = []
    for c in elem.children:
        out.append(c)
        out.extend(_descendants(c))
    return out


# ─── Inverse: derive a selector for an element ────────────────────────────────

def selector_for(root: Any, target_id: str) -> Optional[str]:
    """
    Build a stable XPath-ish selector that uniquely identifies *target_id*
    relative to *root*.  Returns None if the element isn't in the tree.

    Strategy: walk down the path from root, emitting one Step per ancestor.
    Each step uses [name="…"] when the element has a name, else
    [index=N] among its same-role siblings.
    """
    path = _path_to(root, target_id)
    if path is None:
        return None
    parts: List[str] = []
    for parent, child in zip([None] + path[:-1], path):
        if parent is None:
            preds = ""
            if child.name:
                preds = f'[name="{_escape(child.name)}"]'
            parts.append(f"{child.role}{preds}")
        else:
            siblings = parent.children
            same_role = [c for c in siblings if c.role == child.role]
            if child.name:
                parts.append(f'{child.role}[name="{_escape(child.name)}"]')
            elif len(same_role) > 1:
                idx = same_role.index(child)
                parts.append(f"{child.role}[index={idx}]")
            else:
                parts.append(child.role)
    return "/".join(parts)


def _path_to(root: Any, target_id: str) -> Optional[List[Any]]:
    if root.element_id == target_id:
        return [root]
    for c in root.children:
        sub = _path_to(c, target_id)
        if sub is not None:
            return [root, *sub]
    return None


def _escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


# ─── Combined helpers ─────────────────────────────────────────────────────────

def find(root: Any, text: str) -> ResolveResult:
    """Parse *text* and resolve against *root*."""
    return resolve(root, parse(text))

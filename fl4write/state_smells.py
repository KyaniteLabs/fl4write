"""Deterministic detector for implicit state machines in a PR diff.

No model in the loop: each finding is anchored to an exact added line, so it
cannot be hallucinated. It looks only at ADDED lines and only fires on clear
shapes of the bug class ("unenforced invariants" — a lifecycle the code never
names), so ordinary code stays quiet:

  status-text  the same lifecycle variable compared to free-text values in
               3+ places (a typo silently becomes a new, unhandled state)
  flag-soup    3+ different booleans set on one object in one file (several
               flags that together mean one stage)
  copy-drift   the same block of 3+ lines added twice (a transition written
               twice drifts apart; the copies stop agreeing)

Findings are Minor and carry rule_id "state-machines"; the gatekeeper and the
usual caps still apply downstream.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .models import Finding

RULE_ID = "state-machines"
MAX_FINDINGS = 5
STATUS_REPEAT = 3
FLAG_REPEAT = 3
BLOCK_LINES = 3

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_LIFECYCLE_NAME = r"(?:status|state|phase|stage|outcome|lifecycle)"
# x.status == "done" | status != 'x' | row["state"] === "y" | "done" == status
_STATUS_CMP = re.compile(
    r"(?P<lhs>[\w.\]\[\"']*?" + _LIFECYCLE_NAME + r"[\w\"'\]]*)\s*(?:===?|!==?)\s*[\"'][A-Za-z_][\w:-]*[\"']"
    r"|[\"'][A-Za-z_][\w:-]*[\"']\s*(?:===?|!==?)\s*(?P<rhs>[\w.\]\[\"']*?" + _LIFECYCLE_NAME + r"[\w\"'\]]*)",
    re.IGNORECASE)
# obj["key"] = True | obj.key = false | obj['key'] = True
_FLAG_SET = re.compile(
    r"(?P<obj>[A-Za-z_]\w*)\s*(?:\[\s*[\"'](?P<k1>\w+)[\"']\s*\]|\.(?P<k2>[A-Za-z_]\w*))"
    r"\s*=\s*(?:True|False|true|false)\b")


def _added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """{path: [(new_line_number, text), ...]} for every added line."""
    out: dict[str, list[tuple[int, str]]] = defaultdict(list)
    path, line = None, 0
    for raw in (diff_text or "").splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            continue
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or raw.startswith("--- "):
            continue
        if raw.startswith("+"):
            out[path].append((line, raw[1:]))
            line += 1
        elif raw.startswith(" "):
            line += 1
    return out


def _code(text: str) -> str:
    s = text.strip()
    return "" if (not s or s.startswith(("#", "//", "*", "/*"))) else s


def _status_text(files) -> list[Finding]:
    hits: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path, lines in files.items():
        for n, text in lines:
            code = _code(text)
            for m in _STATUS_CMP.finditer(code):
                name = (m.group("lhs") or m.group("rhs") or "").strip("\"'").lower()
                hits[name].append((path, n))
    found = []
    for name, where in hits.items():
        if len(where) >= STATUS_REPEAT:
            path, n = where[0]
            found.append(Finding(
                rule_id=RULE_ID, severity="Minor", path=path, line=n, category="state-machine",
                message=(f"`{name}` is compared to free-text values in {len(where)} places in this "
                         "change: that is an implicit state machine. Name the states once (an enum) "
                         "and compare against its members, so a typo cannot become a new, unhandled "
                         "state."),
                proposal="Define the states in one enum/union and route every change through one "
                         "named transition function."))
    return found


def _flag_soup(files) -> list[Finding]:
    found = []
    for path, lines in files.items():
        keys: dict[str, dict[str, int]] = defaultdict(dict)
        for n, text in lines:
            for m in _FLAG_SET.finditer(_code(text)):
                key = m.group("k1") or m.group("k2")
                keys[m.group("obj")].setdefault(key, n)
        for obj, flags in keys.items():
            if len(flags) >= FLAG_REPEAT:
                first = min(flags.values())
                names = ", ".join(sorted(flags))
                found.append(Finding(
                    rule_id=RULE_ID, severity="Minor", path=path, line=first,
                    category="state-machine",
                    message=(f"{len(flags)} separate booleans are set on `{obj}` ({names}). Flags "
                             "that together mean one stage are an implicit state machine: "
                             "combinations that make no sense become representable."),
                    proposal="Replace the flags with one explicit state field (an enum) and "
                             "named transitions."))
    return found


def _copy_drift(files) -> list[Finding]:
    seen: dict[tuple[str, ...], tuple[str, int]] = {}
    found = []
    for path, lines in files.items():
        code = [(n, _code(t)) for n, t in lines]
        code = [(n, c) for n, c in code if len(c) > 3]
        for i in range(len(code) - BLOCK_LINES + 1):
            window = code[i:i + BLOCK_LINES]
            if any(window[j + 1][0] != window[j][0] + 1 for j in range(BLOCK_LINES - 1)):
                continue  # must be consecutive source lines
            block = tuple(c for _, c in window)
            if block in seen:
                first_path, first_line = seen[block]
                if (first_path, first_line) != (path, window[0][0]):
                    found.append(Finding(
                        rule_id=RULE_ID, severity="Minor", path=path, line=window[0][0],
                        category="state-machine",
                        message=(f"This {BLOCK_LINES}+ line block is also added at "
                                 f"{first_path}:{first_line}. A transition written twice drifts "
                                 "apart: the copies stop agreeing on what the state means."),
                        proposal="Extract the block into one named transition function and call "
                                 "it from both places."))
                    seen[block] = (path, window[0][0])  # one finding per repeated block
            else:
                seen[block] = (path, window[0][0])
    # collapse overlapping windows of the same repeated region
    out, last = [], {}
    for f in found:
        if last.get(f.path, -10) >= f.line - BLOCK_LINES:
            last[f.path] = f.line
            continue
        last[f.path] = f.line
        out.append(f)
    return out


def scan(diff_text: str, review_rules: dict[str, str]) -> list[Finding]:
    """Implicit-state-machine findings for a diff, or [] when the repo's law
    does not include the state-machines rule (grounding)."""
    if RULE_ID not in (review_rules or {}):
        return []
    files = _added_lines(diff_text)
    findings = _status_text(files) + _flag_soup(files) + _copy_drift(files)
    findings.sort(key=lambda f: (f.path, f.line))
    return findings[:MAX_FINDINGS]

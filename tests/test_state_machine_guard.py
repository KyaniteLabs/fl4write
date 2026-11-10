"""Architecture guard: the explicit state machines stay explicit.

Each lifecycle module is the ONLY writer of its phase keys and the only
place its vocabulary is spelled as text. This test scans the package source
and fails CI when a phase key is assigned, or a status literal is written,
anywhere else, so the bug class ("unenforced invariants", implicit state
machines) cannot creep back in one convenient shortcut at a time.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "fl4write"


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with comments removed (strings kept: keys are strings)."""
    src = path.read_text(encoding="utf-8")
    drop: dict[int, int] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            drop[tok.start[0]] = tok.start[1]
    out = []
    for n, line in enumerate(src.splitlines(), start=1):
        out.append((n, line[: drop[n]] if n in drop else line))
    return out


def _assign(key: str) -> str:
    """`x["key"] = ...` or `x["key"] += ...` (not ==)."""
    return r'\[\s*["\']' + re.escape(key) + r'["\']\s*\]\s*[+\-]?=(?!=)'


# (rule name, regex, files allowed to match)
RULES = [
    ("omnisweep phase keys", "|".join(_assign(k) for k in (
        "omni_complete", "omni_published", "omni_report_version", "omni_clean_published",
        "omni_scope_pending", "omni_pub_fail")), {"omni_lifecycle.py"}),
    ("omni finding fix flags", "|".join(_assign(k) for k in ("fix_attempted", "fix_stale")),
     {"omni_lifecycle.py"}),
    ("retro sweep state", "|".join([_assign("retro_complete"), _assign("retro_parked"),
                                    _assign("retro_seen"), r'["\']retro_defer:']),
     {"sweep_lifecycle.py", "state.py"}),
    ("CI watch acted marker", r'f?["\']ci_acted:', {"sweep_lifecycle.py", "state.py"}),
    ("PR review mark", _assign("last_outcome"), {"state.py"}),
    ("exhaustive streak/certification", "|".join(_assign(k) for k in (
        "consecutive_green", "green_sha", "certified_sha")), {"exhaustive_lifecycle.py"}),
    ("fix-lane status literal", r'_finish\(\s*["\']', set()),
    ("fix-status comparison with free text",
     r'status\s*(==|!=)\s*["\'](pr_opened|blocked|skipped|error|testfail|nofix)["\']', set()),
    ("certification status literal",
     r'["\']status["\']\s*:\s*["\'](invalidated|forge_published|forge_certification_pending|'
     r'local_evidence_green|human_action_required)["\']', {"exhaustive_lifecycle.py"}),
    ("atomic fix result literal", r'_result\(\s*["\']', set()),
    ("atomic fix phase literal", _assign("phase") + r'\s*["\']', set()),
]


@pytest.mark.parametrize("name,pattern,allowed", RULES, ids=[r[0] for r in RULES])
def test_state_is_only_written_by_its_lifecycle_module(name, pattern, allowed):
    rx = re.compile(pattern)
    hits = []
    for path in sorted(PKG.glob("*.py")):
        if path.name in allowed:
            continue
        for n, line in _code_lines(path):
            if rx.search(line):
                hits.append(f"{path.name}:{n}: {line.strip()}")
    assert not hits, (
        f"{name} written outside its lifecycle module — route it through the named "
        "transition instead:\n" + "\n".join(hits))


def test_the_guard_itself_detects_a_planted_violation(tmp_path):
    """The rules must match the shapes they are meant to forbid."""
    planted = {
        "omnisweep phase keys": 'st["omni_complete"] = True',
        "retro sweep state": 'st[f"retro_defer:{n}:{sha}"] = 1',
        "CI watch acted marker": 'st[f"ci_acted:{head}"] = True',
        "PR review mark": 'rec["last_outcome"] = "reviewed:1"',
        "exhaustive streak/certification": 'state["consecutive_green"] += 1',
        "fix-lane status literal": 'return _finish("error", "x")',
        "fix-status comparison with free text": 'if status == "pr_opened":',
        "certification status literal": '{"status": "invalidated"}',
        "atomic fix result literal": 'return _result("pending", "r", head)',
        "atomic fix phase literal": 'proof["phase"] = "merging"',
        "omni finding fix flags": 'f["fix_stale"] = True',
    }
    by_name = {r[0]: re.compile(r[1]) for r in RULES}
    assert set(planted) == set(by_name)
    for name, line in planted.items():
        assert by_name[name].search(line), name
    # and reads / comparisons are not writes
    assert not by_name["omnisweep phase keys"].search('if st["omni_complete"] == True:')
    assert not by_name["PR review mark"].search('x = rec["last_outcome"]')

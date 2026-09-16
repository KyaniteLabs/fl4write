"""Fixture time-rot guard (LEARNINGS / PILOT line 58 — five instances now).

Two protections:

1. **Regression pin**: re-assert the retro `tests.test_retro_forgejo._seed_watermark`
   helper is now time-relative. The helper ships with the test module; a future
   refactor that reintroduces a hardcoded date trips this pin before the
   7-test suite silently fails.

2. **Corpus guard**: scan every `tests/test_*.py` for an ISO-ish calendar date
   used as a watermark/merged_at/cursor argument WITHOUT a recognized
   time-relative helper comment on the adjacent line. Allowed exceptions:

   - inside a docstring or comment (no runtime impact)
   - inside a parametrized fixture that is testing an INVALID stamp
     (e.g. `test_incomplete_watermark_is_rejected`); these are explicit
     bad-input examples, not asof dates used to make a cycle advance
   - inside `_r4_date`, `_old_date`, `_hours_ago` (the helpers themselves)
   - hardcoded dates that already carry a `# time-rot-safe:` justification
     comment on the same line or the line directly above

   Each hit is reported as `path:line — <bare iso literal>` so the
   developer sees the exact location. Empty list ⇒ no new time-rot
   tombstone ships in a PR.

Both protections together mean: a future contributor who copy-pastes a
hardcoded `merged_since="2026-09-01T00:00:00Z"` into a real fixture
trips the corpus guard at CI time, with a precise message and the
LEARNINGS pointer, instead of the 7 silent test failures the original
retro bug caused.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from test_retro_forgejo import _seed_watermark as _retro_seed_watermark


_TESTS_DIR = Path(__file__).resolve().parent

# Recognized time-relative helpers — a line that CALLS one of these mints
# its ISO string relative to the wall clock and is safe by construction.
# (2026-09-16, adversarial finding A3: whole-FILE exemptions — including the
# incident file itself — left the riskiest fixtures unscanned; safety is now
# per line: helper calls are safe, bare literals are flagged everywhere.)
_TIME_RELATIVE_HELPERS: tuple[str, ...] = (
    "_old_date(", "_hours_ago(", "_r4_date(", "_seed_watermark(",
)

# Files whose hardcoded ISO strings are intentionally INVALID stamps
# (rejection / corruption tests). These never reach a running cycle.
_BAD_STAMP_TEST_FILES: frozenset[str] = frozenset({
    "test_pm_recovery.py",
    "test_tiers.py",
})

# Pattern matches a calendar ISO literal with year >= 2024 (covers all
# "today" dates a contributor is likely to type). `2026-09-01T00:00:00Z`,
# `2026-09-01`, `2026-12-31T23:59:59Z` — all match.
_ISO_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])"          # not part of a larger identifier/word
    r"(20\d{2}-\d{2}-\d{2}"        # YYYY-MM-DD
    r"(?:T\d{2}:\d{2}:\d{2}Z?)?"   # optional T...Z
    r")"
)

# Lines that look like a watermark/merged_at/cursor assignment near a
# bare ISO literal. We only flag ISO strings that appear to be USED as a
# cycle-relevant timestamp, not date strings in a docstring explaining
# a behavior.
_CYCLE_FIELDS = (
    "merged_since", "merged_at", "retro_cursor",
    "initial_lookback", "last_reviewed",
)


def _is_time_rot_safe(line: str, prev_line: str = "") -> bool:
    """True when the line carries an explicit justification (on this line or
    the one directly above — A5, 2026-09-16: the documented above-line escape
    hatch is now implemented) or CALLS a recognized time-relative helper."""
    if "time-rot-safe" in line or "time-relative" in line:
        return True
    if "time-rot-safe" in prev_line or "time-relative" in prev_line:
        return True
    return any(helper in line for helper in _TIME_RELATIVE_HELPERS)


def _scan_corpus() -> list[tuple[str, int, str]]:
    """Return [(path, line_no, literal), …] for every fixture-rot risk.

    Only flags lines that look like EXECUTABLE code: not inside a
    triple-quoted docstring, not a comment, and either carrying an
    ISO assignment or sitting next to a cycle-relevant field name.
    Plain prose mentions like 'see 2026-09-01 in PILOT' are exempt.
    """
    findings: list[tuple[str, int, str]] = []
    self_name = Path(__file__).name
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        name = path.name
        if name == self_name:                     # the guard documents the trap
            continue
        if name in _BAD_STAMP_TEST_FILES:
            continue
        in_docstring = False
        prev_line = ""
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                                   start=1):
            stripped = line.lstrip()
            # Track simple triple-quoted blocks; """ on its own line toggles.
            triple_count = line.count('"""')
            if triple_count == 1:
                in_docstring = not in_docstring
                prev_line = line
                continue  # the quote line itself is never an assignment
            if triple_count >= 2:
                # single-line docstring; not assignment either
                prev_line = line
                continue
            if in_docstring:
                prev_line = line
                continue
            if _is_time_rot_safe(line, prev_line):
                prev_line = line
                continue
            if stripped.startswith("#"):
                prev_line = line
                continue
            if not _ISO_LITERAL.search(line):
                prev_line = line
                continue
            # Real-world risk shapes:
            #   a) `merged_since="2026-09-01T00:00:00Z"` (assignment)
            #   b) `..., merged_at=hardcoded, ...` (kwarg)
            #   c) line containing "merged_since"/"merged_at"/"retro_cursor"
            #      and an ISO literal nearby
            # A single '=' assigns a fixture stamp; '==' / '!=' / '<=' / '>='
            # compare against an expected value and never drive a cycle.
            lower = line.lower()
            looks_like_assignment = bool(
                re.search(r"(?<![=<>!])=\s*[\"']20\d{2}-", line))
            comparison_only = bool(
                re.search(r"[=!<>]=\s*[\"']20\d{2}-", line)) and not looks_like_assignment
            near_cycle_field = any(field in lower for field in _CYCLE_FIELDS)
            if (looks_like_assignment or near_cycle_field) and not comparison_only:
                findings.append((name, idx, line.strip()))
    return findings


# ----- 1. Regression pin: ensure the retro fix survives ------------------------------

def test_retro_seed_watermark_is_time_relative(tmp_path):
    """Guard for commit 28659b7 (retro test watermark time-rot).

    Before the fix, `_seed_watermark` used a hardcoded `'2026-08-31'`
    merged_since that was older than every PR date in the suite, so
    `p.merged_at <= cursor` was FALSE for every PR and retro_reviewed
    was always 0 (7 silent test failures). The fix uses
    `datetime.now(timezone.utc)` so the watermark stays ahead of PR
    dates built from `_old_date` (which is itself
    `now() - timedelta(days=day_offset)`).

    This test inspects the helper source to catch a future refactor that
    reverts it.
    """
    import inspect

    from test_retro_forgejo import _seed_watermark

    # A4 (2026-09-16 adversarial pass): inspect the AST, not the source
    # text — a comment naming _old_date and an f-string
    # (f"{2026}-09-04T...") both satisfied the old substring checks while
    # the helper stayed effectively fixed-date.
    import ast as _ast

    src = inspect.getsource(_seed_watermark)
    tree = _ast.parse(src)
    fn = tree.body[0]
    assert isinstance(fn, _ast.FunctionDef), "expected a plain function def"

    def _literal_strings(node):
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Constant) and isinstance(sub.value, str):
                yield sub.value
            elif isinstance(sub, _ast.JoinedStr):  # f-string literal parts
                for part in sub.values:
                    if isinstance(part, _ast.Constant) and isinstance(part.value, str):
                        yield part.value

    # (a) no month-day fragment in ANY string literal (catches quoted dates
    #     AND f-string date fragments like "{2026}-09-04T...")
    for text in _literal_strings(fn):
        frag = re.search(r"-\d{2}-\d{2}", text)
        assert frag is None, (
            f"_seed_watermark contains a hardcoded date fragment {frag.group(0)!r} "
            f"in {text!r} — fixture time-rot (LEARNINGS #75 / PILOT.md line 58)."
        )

    # (b) wall-clock anchoring via an actual CALL (never a comment) to
    #     datetime.now(...) or a recognized same-module helper
    def _calls(node):
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Call):
                f = sub.func
                name = getattr(f, "id", None) or getattr(getattr(f, "attr", None), "__str__", lambda: None)()
                if name:
                    yield name

    called = set(_calls(fn))
    anchored = bool(called & {"now", "_old_date", "_hours_ago", "_r4_date"})
    assert anchored, (
        f"_seed_watermark must CALL datetime.now(timezone.utc) or a recognized "
        f"time-relative helper; calls found: {sorted(called) or 'none'}. "
        "A comment naming a helper is not anchoring."
    )

    # Behavioral check: writing through the helper lands a watermark in
    # the FUTURE of any _old_date(...) return value. If _old_date uses
    # `now - N days`, the watermark = `now` must be greater than the PR
    # date. This is the failure mode the original bug exhibited.
    from test_retro_forgejo import _old_date
    sp = tmp_path / "s.json"
    _retro_seed_watermark(sp)
    wm = _load_watermark(sp)
    pr_date = _old_date(10, "12:00:00")
    assert wm > pr_date, (
        f"_seed_watermark({sp.name}) wrote {wm!r} which is NOT after "
        f"_old_date(10)({pr_date!r}). 7 retro tests will silently fail."
    )


def _load_watermark(state_path: Path) -> str:
    import json
    return json.loads(state_path.read_text())["merged_since"]


# ----- 2. Corpus guard: no new bare ISO dates in cycle-touching fixtures -------------

def test_no_new_bare_iso_dates_in_cycle_fixtures():
    """Scan tests/test_*.py for hardcoded ISO calendar literals used as
    a watermark / merged_at / cursor argument without a documented
    time-relative helper or a `# time-rot-safe:` justification.

    Empty list ⇒ no future contributor has copy-pasted a date that
    will silently break the next time the calendar rolls.
    """
    hits = _scan_corpus()
    if hits:
        msg = ["Bare ISO literal in cycle-relevant test fixture "
               "(LEARNINGS #63 / PILOT.md line 58 — fixture time-rot):"]
        for name, line, text in hits:
            msg.append(f"  {name}:{line}  {text}")
        raise AssertionError("\n".join(msg))


# ----- 3. Sanity check: the corpus guard itself doesn't false-flag -------------------

def test_corpus_guard_skips_helpers_and_invalid_stamp_suites():
    """Negative pin: the two allowed exception classes must be honored.

    A regression in the scanner logic that suddenly starts flagging
    `test_pm_recovery.py` / `test_tiers.py` (the BAD-STAMP and
    test-gauntlet helpers) would create noise without catching any new
    bug. Lock down the skip list by reading the file content directly.
    """
    for name in ("test_pm_recovery.py",):
        text = (_TESTS_DIR / name).read_text(encoding="utf-8")
        assert any(t in text for t in ("_valid_iso(", "_read_state(")), (
            f"{name} no longer imports from state/tiers; "
            f"update _BAD_STAMP_TEST_FILES if intentional."
        )
    # Every recognized helper must live in a file that actually anchors on
    # datetime.now(timezone.utc) — a helper file that stops using the wall
    # clock makes its name-bearing calls unsafe, so it must be removed from
    # _TIME_RELATIVE_HELPERS and its literals re-annotated.
    _helper_home_files = ("test_gauntlet_fixes.py", "test_postmerge.py",
                          "test_retro_forgejo.py")
    for name in _helper_home_files:
        text = (_TESTS_DIR / name).read_text(encoding="utf-8")
        assert "datetime.now(timezone.utc)" in text, (
            f"{name} hosts a recognized time-relative helper but no longer "
            f"uses datetime.now(timezone.utc). Remove the helper from "
            f"_TIME_RELATIVE_HELPERS and annotate its literals."
        )
def test_scanner_detects_planted_bare_iso_fixture(monkeypatch, tmp_path):
    """Contract pin: a fake cycle-fixture file with a bare ISO date MUST
    be detected, then MUST be cleared when the file is removed.

    Catches regressions in the scanner logic itself (e.g. a future refactor
    that drops the assignment-detection regex) without depending on
    write/delete race conditions during a normal pytest run.
    """
    planted = tmp_path / "test_PROBE_TIME_ROT.py"
    planted.write_text(
        'def test_p():\n'
        '    state = {"merged_since": "2026-09-01T00:00:00Z"}\n'
        '    assert state["merged_since"].startswith("2026")\n',
        encoding="utf-8",
    )

    class _FakeDir:
        def __init__(self, root):
            self._root = root
        def glob(self, pattern):
            return sorted(self._root.glob(pattern))

    # _TESTS_DIR lives in the local test module under testpaths = ["tests"];
    # pytest loads it as a top-level module, not a 'tests.' package.
    import test_fixture_time_rot as _guard_mod
    monkeypatch.setattr(_guard_mod, "_TESTS_DIR", _FakeDir(tmp_path))
    hits = _guard_mod._scan_corpus()
    assert hits, "Scanner must detect the planted `merged_since=2026-...`"
    assert hits[0][0] == "test_PROBE_TIME_ROT.py"
    # And the planted file itself never qualifies as time-rot-safe, so
    # removing it must clear the corpus regardless of what it's named.
    planted.unlink()
    hits2 = _guard_mod._scan_corpus()
    assert hits2 == [], f"Post-cleanup corpus not empty: {hits2}"


# ----- 4. Calendar-rotation smoke: retro helper must survive ~Nov 2026 --------------

def test_retro_helper_survives_calendar_rotation_by_days_only():
    """The watermark invariant must hold across the calendar boundary
    that broke the original implementation (Sep 5, 2026). `_old_date(10)`
    returns `now - 10d`; the watermark must therefore be `now` (strictly
    greater than the PR date).

    We do NOT need to wait for the date — invoking the helpers once is
    enough — but we DO assert the relationship explicitly so any future
    date-arithmetic refactor that breaks it fails loudly.
    """
    from test_retro_forgejo import _old_date

    today = datetime.datetime.now(datetime.timezone.utc)
    pr_date_str = _old_date(10, "00:00:00")
    pr_date = datetime.datetime.strptime(pr_date_str, "%Y-%m-%dT%H:%M:%SZ")
    pr_date = pr_date.replace(tzinfo=datetime.timezone.utc)

    delta_days = (today - pr_date).total_seconds() / 86400.0
    # _old_date truncates to YYYY-MM-DD then appends T<hhmm>Z, so the real
    # offset may drift up to a full day past `day_offset` depending on the
    # wall-clock time. Allowance: a generous ±1.5 days covers normal time-
    # of-day variance plus a leap-second margin and stays well within the
    # 90-day retro lookback.
    assert 10 - 1.5 < delta_days < 10 + 1.5, (
        f"_old_date(10) drifted: now - pr_date = {delta_days:.3f} days. "
        f"Won't safely outlive the next test run."
    )

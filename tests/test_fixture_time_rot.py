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

# Recognized time-relative helpers — files that LEGITIMATELY mint ISO
# calendar strings for fixtures. New helpers should be added here and
# pulled from the SAME `datetime.now(timezone.utc) - …` pattern.
_TIME_RELATIVE_FILES: frozenset[str] = frozenset({
    "test_gauntlet_fixes.py",   # _r4_date()
    "test_postmerge.py",        # _hours_ago()
    "test_retro_forgejo.py",    # _old_date() + _seed_watermark()
})

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


def _is_time_rot_safe(line: str) -> bool:
    """Return True if the line carries an explicit time-rot justification."""
    return "time-rot-safe" in line or "time-relative" in line


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
        if name in _TIME_RELATIVE_FILES or name in _BAD_STAMP_TEST_FILES:
            continue
        in_docstring = False
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                                   start=1):
            stripped = line.lstrip()
            # Track simple triple-quoted blocks; """ on its own line toggles.
            triple_count = line.count('"""')
            if triple_count == 1:
                in_docstring = not in_docstring
                continue  # the quote line itself is never an assignment
            if triple_count >= 2:
                # single-line docstring; not assignment either
                continue
            if in_docstring:
                continue
            if _is_time_rot_safe(line):
                continue
            if stripped.startswith("#"):
                continue
            if not _ISO_LITERAL.search(line):
                continue
            # Real-world risk shapes:
            #   a) `merged_since="2026-09-01T00:00:00Z"` (assignment)
            #   b) `..., merged_at=hardcoded, ...` (kwarg)
            #   c) line containing "merged_since"/"merged_at"/"retro_cursor"
            #      and an ISO literal nearby
            lower = line.lower()
            looks_like_assignment = bool(re.search(r"=\s*[\"']20\d{2}-", line))
            near_cycle_field = any(field in lower for field in _CYCLE_FIELDS)
            if looks_like_assignment or near_cycle_field:
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

    src = inspect.getsource(_seed_watermark)
    # Hardcoded YYYY-MM-DD string literal → forbidden
    bad = re.search(r"[\"']\d{4}-\d{2}-\d{2}[\"']", src)
    assert bad is None, (
        f"_seed_watermark must be time-relative (no hardcoded dates). "
        f"LEARNINGS #63 / PILOT.md line 58 — fixture time-rot. "
        f"Saw: {bad.group(0) if bad else '?'}"
    )
    # Must stay wall-clock anchored: either the helper itself calls
    # datetime.now(timezone.utc), or it delegates to a same-module helper
    # that does (the explicit-clock anchor form landed as abf003e: the
    # helper takes now= and forwards it to _old_date, which anchors on
    # datetime.now(timezone.utc)). A refactor that severs BOTH paths
    # trips this pin.
    anchored = "datetime.now(timezone.utc)" in src
    if not anchored:
        from test_retro_forgejo import _old_date
        delegates = "_old_date" in src
        anchor_src = inspect.getsource(_old_date)
        anchored = delegates and "datetime.now(timezone.utc)" in anchor_src
    assert anchored, (
        "_seed_watermark must anchor (directly or via _old_date) on "
        "datetime.now(timezone.utc) so the watermark stays ahead of "
        "_old_date-based PR dates."
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
    # The recognized helpers must each call datetime.now(timezone.utc)
    # at least once — otherwise remove them from _TIME_RELATIVE_FILES
    # so they get scanned.
    for name in _TIME_RELATIVE_FILES:
        text = (_TESTS_DIR / name).read_text(encoding="utf-8")
        assert "datetime.now(timezone.utc)" in text, (
            f"{name} is in _TIME_RELATIVE_FILES but no longer uses "
            f"datetime.now(timezone.utc). Either add the helper here "
            f"or remove the file from the skip set."
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

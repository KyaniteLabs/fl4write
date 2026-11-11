"""Explicit outcome machines (fl4write/outcomes.py).

Fix results, persisted review marks and sweep step results used to be free
text shared between modules. These tests hold the explicit versions: every
writer can only emit a member, every reader handles every member by name,
and legacy persisted marks keep reading the same way.
"""

from __future__ import annotations

import pytest

from fl4write import engine, executor, state
from fl4write.models import Finding
from fl4write.outcomes import (FIX_FAILURES, TERMINAL_STEPS, FixResume, FixStatus,
                               ReviewMark, ReviewStep, format_mark, mark_is_shadow)
from test_fl4write import make_config, make_pr


# ---------------------------------------------------------------- vocabularies
def test_vocabularies_keep_their_persisted_spellings():
    assert {s.value for s in FixStatus} == {"pr_opened", "blocked", "skipped", "error",
                                            "testfail", "nofix"}
    assert {m.value for m in ReviewMark} == {"reviewed", "shadow", "retro",
                                             "model-failed-cap", "dependency-skip"}
    assert {s.value for s in ReviewStep} == {"reviewed", "shadow", "fix-deferred",
                                             "model-failed-cap", "model-unavailable",
                                             "diff-unavailable", "deferred"}
    assert {r.value for r in FixResume} == {"gated-off", "dropped", "attempted"}
    assert FIX_FAILURES == {FixStatus.ERROR, FixStatus.TESTFAIL, FixStatus.NOFIX}
    assert TERMINAL_STEPS == {ReviewStep.REVIEWED, ReviewStep.MODEL_FAILED_CAP}


@pytest.mark.parametrize("kind,count,out", [
    (ReviewMark.REVIEWED, 3, "reviewed:3"), (ReviewMark.SHADOW, 0, "shadow:0"),
    (ReviewMark.RETRO, 2, "retro:2"), (ReviewMark.MODEL_FAILED_CAP, None, "model-failed-cap"),
    (ReviewMark.DEPENDENCY_SKIP, None, "dependency-skip"), ("reviewed", 1, "reviewed:1")])
def test_format_mark(kind, count, out):
    assert format_mark(kind, count) == out


@pytest.mark.parametrize("kind,count", [
    (ReviewMark.REVIEWED, None), (ReviewMark.SHADOW, -1), (ReviewMark.RETRO, True),
    (ReviewMark.REVIEWED, "3"), (ReviewMark.MODEL_FAILED_CAP, 0),
    (ReviewMark.DEPENDENCY_SKIP, 1), ("ok", None), ("posted", None)])
def test_format_mark_refuses_free_text_and_bad_counts(kind, count):
    with pytest.raises(ValueError):
        format_mark(kind, count)


@pytest.mark.parametrize("raw,shadow", [
    ("shadow:3", True), ("shadow", True), ("shadow-posted", True),     # legacy forms
    ("reviewed:0", False), ("posted", False), ("ok", False), ("retro:2", False),
    ("model-failed-cap", False), ("", False), (None, False)])
def test_shadow_marks_including_legacy_state(raw, shadow):
    assert mark_is_shadow(raw) is shadow


def test_needs_review_reads_legacy_persisted_marks():
    st = {"prs": {"1": {"last_reviewed_sha": "a", "last_outcome": "posted"},
                  "2": {"last_reviewed_sha": "b", "last_outcome": "shadow-posted"}}}
    assert state.needs_review(st, 1, "a") is False
    assert state.needs_review(st, 2, "b") is True


def test_mark_reviewed_refuses_free_text():
    st = {"prs": {}}
    with pytest.raises(ValueError):
        state.mark_reviewed(st, 1, "a", "ok")
    assert st["prs"] == {}                               # nothing half-written


# ---------------------------------------------------------------- executor writes
def test_attempt_fix_only_returns_enumerated_statuses(monkeypatch):
    out = executor.attempt_fix(make_pr(is_fork=True), Finding(
        rule_id="r", severity="Major", path="a.py", line=1, message="m"),
        make_config(fix={"enabled": True}))
    assert out["status"] is FixStatus.BLOCKED and out["status"] == "blocked"


# ---------------------------------------------------------------- engine reads
class _Forge:
    name = "github"

    def __init__(self):
        self.comments = []

    def path_exists(self, repo, path, ref=None):
        return True

    def create_comment(self, repo, number, body):
        self.comments.append(body)


def _run_fix_lane(monkeypatch, result):
    monkeypatch.setattr(executor, "attempt_fix", lambda pr, f, cfg: result)
    report = engine.CycleReport(repo="o/r")
    st = {"prs": {}}
    f = Finding(rule_id="r", severity="Major", path="a.py", line=1, message="m")
    engine._fix_lane(make_pr(number=7), [f], make_config(fix={"enabled": True}),
                     _Forge(), st, report)
    return report, st


@pytest.mark.parametrize("status", list(FixStatus))
def test_fix_lane_handles_every_status_by_name(monkeypatch, status):
    report, st = _run_fix_lane(monkeypatch, {"status": status, "reason": "why"})
    notes = " ".join(report._fix_failure_notes)
    assert "unknown-status" not in notes
    assert report.fix_attempts == 1
    if status is FixStatus.PR_OPENED:
        assert report.fix_prs_opened == 1 and st["prs"]["7"]["fix_depth"] == 1
    elif status in FIX_FAILURES:
        assert report.fix_failures == 1 and f"#7 {status.value}: why" in notes
    elif status is FixStatus.BLOCKED:
        assert report.fix_escalations == 1 and report.fix_failures == 0
    else:
        assert status is FixStatus.SKIPPED
        assert report.fix_failures == 0 and "#7 skipped: why" in notes


@pytest.mark.parametrize("result", [{"status": "banana"}, {}, None, "pr_opened"])
def test_fix_lane_surfaces_malformed_results(monkeypatch, result):
    report, _ = _run_fix_lane(monkeypatch, result)
    assert report.fix_failures == 1 and "unknown-status" in report._fix_failure_notes[0]


@pytest.mark.parametrize("raw,out", [({"status": "pr_opened"}, FixStatus.PR_OPENED),
                                     ({"status": FixStatus.NOFIX}, FixStatus.NOFIX),
                                     ({"status": "x"}, None), ({}, None), (None, None),
                                     ([], None)])
def test_fix_status_parser(raw, out):
    assert engine._fix_status(raw) is out


def test_every_review_step_has_a_sweep_decision():
    """The post-merge sweep decides on: SHADOW (belt, continue), FIX_DEFERRED
    (stop, hold watermark), TERMINAL (advance) and everything else (retry).
    That partition must cover the whole enum with no overlap."""
    retry = {ReviewStep.MODEL_UNAVAILABLE, ReviewStep.DIFF_UNAVAILABLE, ReviewStep.DEFERRED}
    groups = [{ReviewStep.SHADOW}, {ReviewStep.FIX_DEFERRED}, set(TERMINAL_STEPS), retry]
    assert set().union(*groups) == set(ReviewStep)
    assert sum(len(g) for g in groups) == len(ReviewStep)


def test_unknown_step_results_are_refused_not_misread():
    with pytest.raises(ValueError):
        ReviewStep("reviewd")
    with pytest.raises(ValueError):
        FixResume("gated_off")

"""Completed reviews retain only fix work that the deadline prevented."""
import pytest

from fl4write import engine, state
from fl4write import engine_omni
from fl4write.models import Finding, ReviewDoc
from test_fl4write import FakeForge, make_config, make_pr


@pytest.fixture
def lane(monkeypatch):
    pr = make_pr()
    config = make_config(shadow=False, gatekeeper=False, fix={"enabled": True})
    forge = FakeForge()
    st = {"version": 1, "prs": {}}
    clock = [0]
    calls = []
    findings = [Finding(rule_id="loc-ceiling", severity="Major", path="x.py",
                        line=i, message="Repair") for i in (1, 2)]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engine.fixlane, "fix_allowed", lambda *a: None)
    monkeypatch.setattr(engine, "_fix_freshness_gate", lambda *a: True)
    monkeypatch.setattr(engine_omni, "_fix_freshness_gate", lambda *a: True)
    monkeypatch.setattr("fl4write.executor.attempt_fix",
                        lambda p, f, c, depth_used=0: calls.append(f.line) or {"status": "nofix"})
    return pr, config, forge, st, clock, calls, findings


def test_analysis_expiry_preserves_review_and_unattempted_fixes(lane, monkeypatch, tmp_path):
    pr, config, forge, st, clock, calls, findings = lane
    def analyze(*a):
        clock[0] = 101
        return ReviewDoc(pr=pr, findings=findings)
    monkeypatch.setattr("fl4write.analyzer.analyze", analyze)
    report = engine.CycleReport(repo=config.repo)
    engine._review_pr(pr, config, forge, lambda p: ({"x.py"}, "diff"),
                      None, st, report, True, deadline=100)
    assert calls == []
    assert report.reviewed == 1
    assert not state.needs_review(st, pr.number, pr.head_sha)
    path = tmp_path / "state.json"
    state.save_state(path, st)
    st = state.load_state(path)
    clock[0] = 0
    engine._resume_fix_lane(pr, config, forge, st, report, True, 100)
    assert calls == [1, 2]
    assert report.reviewed == 1
    assert len(forge.posts) == 1
    engine._resume_fix_lane(pr, config, forge, st, report, True, 100)
    assert calls == [1, 2]


@pytest.mark.parametrize("stage", ["entry", "freshness", "between"])
def test_deadline_checks_preserve_only_unattempted_suffix(lane, monkeypatch, stage):
    pr, config, forge, st, clock, calls, findings = lane
    if stage == "entry":
        clock[0] = 100
    elif stage == "freshness":
        def fresh(*a):
            clock[0] = 100
            return True
        monkeypatch.setattr(engine, "_fix_freshness_gate", fresh)
        monkeypatch.setattr(engine_omni, "_fix_freshness_gate", fresh)
    else:
        def attempt(p, f, c, depth_used=0):
            calls.append(f.line)
            clock[0] = 100
            return {"status": "nofix"}
        monkeypatch.setattr("fl4write.executor.attempt_fix", attempt)
    report = engine.CycleReport(repo=config.repo)
    engine._fix_lane(pr, findings, config, forge, st, report, deadline=100)
    assert calls == ([1] if stage == "between" else [])
    assert [f["line"] for f in st["prs"]["1"]["pending_fixes"]] == (
        [2] if stage == "between" else [1, 2])


def test_pending_fix_new_head_and_disabled_controls(lane):
    pr, config, forge, st, clock, calls, findings = lane
    engine._fix_lane(pr, findings, config, forge, st, engine.CycleReport(repo=config.repo), deadline=0)
    engine._resume_fix_lane(pr, config, forge, st, engine.CycleReport(repo=config.repo), False, 100)
    assert st["prs"]["1"]["pending_fixes"]
    engine._resume_fix_lane(make_pr(head_sha="b" * 40), config, forge, st,
                            engine.CycleReport(repo=config.repo), True, 100)
    assert calls == []
    assert "pending_fixes" not in st["prs"]["1"]


def test_no_deadline_preserves_existing_fix_behavior(lane):
    pr, config, forge, st, clock, calls, findings = lane
    engine._fix_lane(pr, findings, config, forge, st, engine.CycleReport(repo=config.repo))
    assert calls == [1, 2]
    assert "pending_fixes" not in st["prs"]["1"]


def test_crashing_attempt_keeps_pending_entry(lane, monkeypatch):
    pr, config, forge, st, clock, calls, findings = lane
    def boom(p, f, c, depth_used=0):
        calls.append(f.line)
        raise RuntimeError("git exploded")
    monkeypatch.setattr("fl4write.executor.attempt_fix", boom)
    report = engine.CycleReport(repo=config.repo)
    # Pre-fix this raised out of the lane mid-loop (entries already popped
    # and lost); now the lane completes with both attempted and counted.
    engine._fix_lane(pr, findings, config, forge, st, report)
    assert calls == [1, 2]
    assert report.fix_failures == 2
    assert sum("entry kept" in a for a in report.alerts) == 2


def test_gated_off_open_pr_drops_stale_pending(lane, monkeypatch, tmp_path):
    """Round-3 hunt: the post-merge twin drops stale records with an
    alert; the open path wedged silently. Same drop, same alert."""
    pr, config, forge, st, clock, calls, findings = lane
    config.fix.enabled = False
    forge.prs = [pr]
    monkeypatch.setattr(engine, "adapter_for", lambda *a: forge)
    monkeypatch.setattr(engine, "_poll_fix_merges", lambda *a, **k: None)
    state.mark_reviewed(st, pr.number, pr.head_sha, "test")
    st["prs"]["1"]["pending_fixes"] = [findings[0].model_dump()]
    st["prs"]["1"]["pending_fix_sha"] = pr.head_sha
    path = tmp_path / "state.json"
    state.save_state(path, st)
    report = engine.run_cycle(config, path, get_diff=lambda p: ({"x.py"}, "diff"),
                              run_fixes=True, deadline=10 ** 6)
    assert report.reviewed == 0
    assert "pending_fixes" not in state.load_state(path)["prs"]["1"]
    assert any("stale pending fixes dropped" in a for a in report.alerts)


def test_open_pr_resume_stall_alerts_like_post_merge(lane, monkeypatch, tmp_path):
    """Zero-findings review: attempted-but-deferred open-PR fixes were
    silent while post-merge stalls alerted — both now signal."""
    pr, config, forge, st, clock, calls, findings = lane
    forge.prs = [pr]
    monkeypatch.setattr(engine, "adapter_for", lambda *a: forge)
    monkeypatch.setattr(engine, "_poll_fix_merges", lambda *a, **k: None)
    state.mark_reviewed(st, pr.number, pr.head_sha, "test")
    st["prs"]["1"]["pending_fixes"] = [findings[0].model_dump()]
    st["prs"]["1"]["pending_fix_sha"] = pr.head_sha
    path = tmp_path / "state.json"
    state.save_state(path, st)
    # Cycle gate sees time 0 (passes); the fix drain sees far-future
    # (expired): resume runs, attempts nothing, entries stay pending.
    ticks = [0.0] + [10.0 ** 9] * 10
    monkeypatch.setattr(engine.time, "monotonic", lambda: ticks.pop(0))
    report = engine.run_cycle(config, path, get_diff=lambda p: ({"x.py"}, "diff"),
                              run_fixes=True, deadline=10 ** 6)
    assert report.reviewed == 0
    assert calls == []
    assert any("stalled" in a and "await resume" in a for a in report.alerts)


def test_diverged_rail_escalates_instead_of_crashing(lane, monkeypatch):
    from fl4write import fixlane
    pr, config, forge, st, clock, calls, findings = lane

    def diverged(pr_, config_, depth):
        raise fixlane.FixLaneBlocked("fork_policy must be comment-only")

    monkeypatch.setattr(engine.fixlane, "fix_allowed", diverged)
    report = engine.CycleReport(repo=config.repo)
    engine._fix_lane(pr, findings, config, forge, st, report)
    assert calls == []
    assert report.fix_escalations == 2
    assert "pending_fixes" not in st["prs"]["1"]


@pytest.mark.parametrize("post_merge", [False, True])
def test_real_cycle_resumes_without_another_review(lane, monkeypatch, tmp_path, post_merge):
    pr, config, forge, st, clock, calls, findings = lane
    config.post_merge.enabled = post_merge
    pr.merged_at = "2026-09-01T12:00:00Z"  # time-rot-safe: compared only against the seeded watermark below, never against wall clock
    forge.prs = [] if post_merge else [pr]
    forge.list_merged_prs = lambda *a: [pr]
    monkeypatch.setattr(engine, "adapter_for", lambda *a: forge)
    monkeypatch.setattr(engine, "_poll_fix_merges", lambda *a, **k: None)
    modeled = []
    def analyze(*a):
        modeled.append(1)
        clock[0] = 101
        return ReviewDoc(pr=pr, findings=findings)
    monkeypatch.setattr("fl4write.analyzer.analyze", analyze)
    path = tmp_path / "state.json"
    state.save_state(path, {"version": 1, "prs": {}, "merged_since": "2026-09-01T00:00:00Z"})  # time-rot-safe: compared only to pr.merged_at above, never to now()
    def cycle():
        return engine.run_cycle(config, path, get_diff=lambda p: ({"x.py"}, "diff"),
                                run_fixes=True, deadline=100)
    first = cycle()
    assert first.reviewed == 1
    assert calls == []
    if post_merge:
        assert state.load_state(path)["merged_since"] == "2026-09-01T00:00:00Z"  # time-rot-safe: pins the seeded value; no wall-clock compare
    clock[0] = 0
    second = cycle()
    assert second.reviewed == 0
    assert calls == [1, 2]
    assert modeled == [1]
    assert len(forge.posts) == 1
    if post_merge:
        assert state.load_state(path)["merged_since"] == pr.merged_at
    cycle()
    assert calls == [1, 2]

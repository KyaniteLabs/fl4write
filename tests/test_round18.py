"""Functional regressions from fresh whole-project review round 18."""
import json

import pytest

from fl4write import analyzer, engine, executor, metrics
from fl4write.forges import ForgeAdapter, ForgeError
from fl4write.models import Finding
from test_gauntlet_fixes import (
    _NO_EXTRA_LANES, _R4Forge, _r4_cycle, _r4_pr, _r4_seed, make_config,
)


def test_open_pr_new_file_is_checked_at_reviewed_revision(monkeypatch):
    pr = _r4_pr()
    requests = []
    attempts = []

    class Forge(_R4Forge):
        path_exists = ForgeAdapter.path_exists

        def _call(self, method, path, **kwargs):
            requests.append(path)
            if path.endswith("?ref=" + pr.head_sha):
                return {"type": "file"}
            raise ForgeError("HTTP 404: new file does not exist on default branch")

    finding = Finding(path="new.py", line=1, severity="Major", rule_id="general",
                      message="The new module raises on import.")
    monkeypatch.setattr(executor, "attempt_fix", lambda *args:
                        attempts.append(args) or {"status": "pr_opened"})
    monkeypatch.setattr(executor, "check_and_merge_own_prs", lambda *args: [])
    report = engine.CycleReport(repo=pr.repo)
    engine._fix_lane(pr, [finding], make_config(), Forge(), {"prs": {}}, report)
    assert report.fix_attempts == 1 and report.fix_prs_opened == 1
    assert len(attempts) == 1
    assert requests == [f"/repos/{pr.repo}/contents/new.py?ref={pr.head_sha}"]


@pytest.mark.parametrize("byte_count,character", [(60001, "x"), (200000, "x"), (199999, "é")])
def test_whole_file_review_includes_tail_and_retains_its_finding(monkeypatch, byte_count, character):
    tail = "\nresult = 1 / 0\n"
    remaining = byte_count - len(("#" + tail).encode())
    width = len(character.encode())
    source = "#" + character * (remaining // width) + "x" * (remaining % width) + tail
    prompts = []
    finding = {"path": "long.py", "line": 2, "severity": "Major", "rule_id": "general",
               "message": "`result = 1 / 0` raises ZeroDivisionError whenever the module is imported."}

    def model(route, prompt, *args, **kwargs):
        prompts.append(prompt)
        return json.dumps({"findings": [finding]})

    monkeypatch.setattr(analyzer, "_call_model", model)
    doc = analyzer.analyze(_r4_pr(), {"long.py"}, source, make_config(), mode="file")
    assert len(source.encode()) == byte_count
    assert source in prompts[0]
    assert [f.path for f in doc.findings] == ["long.py"]
    assert doc.digest["_diff_truncated"] == 0


@pytest.mark.parametrize("source", ["x" * 200001, "é" * 100001], ids=["ascii", "utf8"])
def test_oversize_whole_file_defers_before_model_call(monkeypatch, source):
    calls = []
    monkeypatch.setattr(analyzer, "_call_model", lambda *args, **kwargs:
                        calls.append(args) or '{"findings": []}')
    with pytest.raises(analyzer.ModelUnavailable, match="byte bound"):
        analyzer.analyze(_r4_pr(), {"long.py"}, source, make_config(), mode="file")
    assert not calls


def test_owned_fix_ci_is_revisited_without_new_source_review(tmp_path, monkeypatch):
    pr = _r4_pr()
    state_path = tmp_path / "state.json"
    _r4_seed(state_path, prs={str(pr.number): {"head_sha": pr.head_sha, "outcome": "reviewed"}})
    forge = _R4Forge(open_prs=[pr])
    polls = []

    def poll(config, identity):
        polls.append(identity)
        return [] if len(polls) == 1 else [7]

    monkeypatch.setattr(executor, "check_and_merge_own_prs", poll)
    monkeypatch.setattr(metrics, "acceptance_snapshot", lambda *args: None)
    config = {**_NO_EXTRA_LANES, "fix": {"enabled": True, "merge_own_prs": True}}
    first = _r4_cycle(forge, monkeypatch, state_path, config, run_fixes=True)
    second = _r4_cycle(forge, monkeypatch, state_path, config, run_fixes=True)
    assert first.reviewed == second.reviewed == 0
    assert len(polls) == 2
    assert first.fix_prs_merged == 0 and second.fix_prs_merged == 1

"""Desk decisions cannot substitute for recon identity, complete tests, or valid fixes."""
import copy
import hashlib
import json

import pytest

from fl4write import exhaustive
from fl4write.exhaustive_adjudication import AdjudicationError, actionable, fingerprint, read_decision
from fl4write.exhaustive_publication import PublicationError, ledger_body
from test_exhaustive import _args, _git, _repo, _responses, _state


FINDING = {"path": "value.py", "line": 1, "evidence": "VALUE = 1", "severity": "Major", "message": "bad value"}


def _setup(tmp_path):
    repo = _repo(tmp_path)
    responses = _responses(tmp_path, [{"findings": []}, {"findings": []}, {"findings": [FINDING]}])
    args = _args(repo, tmp_path / "state", responses)
    args.ledger_issue = None
    args.desk_adjudications = tmp_path / "desk"
    args.desk_adjudications.mkdir()
    return repo, args


def _decision(pending, verdict="invalid"):
    recon = pending["recon_evidence_bundle"]
    return {"version": 1, "reviewed_head": pending["reviewed_head"], "recon_sha256": recon["sha256"],
            "reviewer": "independent fixture reviewer", "decisions": [
                {"finding_id": ident, "verdict": verdict, "rationale": "PRIVATE_DESK_EVIDENCE"}
                for ident in sorted({fingerprint(row) for row in pending["findings"]})]}


def _accept(args, pending, verdict="invalid"):
    path = args.desk_adjudications / (pending["recon_evidence_bundle"]["sha256"] + ".json")
    path.write_text(json.dumps(_decision(pending, verdict)))
    return path


@pytest.mark.parametrize("ledger_issue", [None, 13], ids=["local", "fake-owned-forge"])
def test_three_desk_verified_rounds_preserve_raw_findings_and_streak(tmp_path, monkeypatch, ledger_issue):
    repo, args = _setup(tmp_path)
    args.ledger_issue = ledger_issue
    recon = exhaustive._recon
    calls = []

    def counted(*a, **kw):
        calls.append(1)
        return recon(*a, **kw)

    monkeypatch.setattr(exhaustive, "_recon", counted)
    for previous in range(3):
        assert exhaustive.run(args) == 2
        state = _state(args.state_dir)
        assert state["consecutive_green"] == previous
        assert len(calls) == previous + 1
        assert state["round"] == previous
        _accept(args, state["pending_round"])
        assert exhaustive.run(args) == (0 if previous == 2 and ledger_issue else 2)
        state = _state(args.state_dir)
        assert len(calls) == previous + 1  # Resume reuses sealed recon.
        assert state["consecutive_green"] == previous + 1
        row = state["ledger"][-1]
        assert row["findings"] == [FINDING]
        assert row["finding_count"] == 1 and row["valid_finding_count"] == 0
        assert row["test_ids"] and row["tested_head"] == _git(repo, "rev-parse", "HEAD")
        assert row["recon_evidence_bundle"] != row["evidence_bundle"]
        exhaustive._load_state(next(args.state_dir.glob("*/state.json")), exhaustive._identity(repo)[1])
    assert state["certified_sha"] == (_git(repo, "rev-parse", "HEAD") if ledger_issue else None)
    public = ledger_body(state, "fixture/repo")
    assert '"valid_finding_count": 0' in public and '"finding_count": 1' in public
    assert "PRIVATE_DESK_EVIDENCE" not in public and "value.py" not in public


@pytest.mark.parametrize("verdict", ["valid", "duplicate"])
def test_actionable_desk_verdict_cannot_bypass_fix_gate(tmp_path, verdict):
    _, args = _setup(tmp_path)
    assert exhaustive.run(args) == 2
    _accept(args, _state(args.state_dir)["pending_round"], verdict)
    assert exhaustive.run(args) == 2
    state = _state(args.state_dir)
    assert state["consecutive_green"] == 0 and state["pending_round"] is None
    assert state["ledger"][-1]["valid_finding_count"] == 1
    assert "owned-PR" in state["ledger"][-1]["reason"]


def test_accepted_decisions_are_pinned_across_unavailable_suite(tmp_path, monkeypatch):
    _, args = _setup(tmp_path)
    assert exhaustive.run(args) == 2
    path = _accept(args, _state(args.state_dir)["pending_round"])
    test = exhaustive._test
    def unavailable(*a):
        raise exhaustive.Deferred("runner unavailable")
    monkeypatch.setattr(exhaustive, "_test", unavailable)
    assert exhaustive.run(args) == 2
    pending = _state(args.state_dir)["pending_round"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert pending["adjudication_sha256"] == digest
    path.write_text("{}")
    monkeypatch.setattr(exhaustive, "_test", test)
    assert exhaustive.run(args) == 2
    assert _state(args.state_dir)["ledger"][-1]["adjudication_sha256"] == digest


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(reviewed_head="b" * 40),
    lambda d: d.update(recon_sha256="b" * 64),
    lambda d: d.update(version=True),
    lambda d: d.update(reviewer=" "),
    lambda d: d.update(decisions=[]),
    lambda d: d["decisions"].append(copy.deepcopy(d["decisions"][0])),
    lambda d: d["decisions"][0].update(finding_id="c" * 64),
    lambda d: d["decisions"][0].update(verdict="dismiss"),
    lambda d: d["decisions"][0].update(rationale=""),
], ids=["head", "bundle", "version", "reviewer", "missing", "duplicate-id", "unknown-id", "verdict", "rationale"])
def test_decisions_require_exact_complete_current_identity(mutate):
    pending = {"reviewed_head": "a" * 40, "recon_evidence_bundle": {"sha256": "a" * 64}, "findings": [FINDING]}
    value = _decision(pending)
    mutate(value)
    with pytest.raises(AdjudicationError):
        actionable(value, pending["reviewed_head"], pending["recon_evidence_bundle"], [FINDING])


def test_repeated_raw_occurrences_share_one_decision_but_keep_occurrence_count():
    pending = {"reviewed_head": "a" * 40, "recon_evidence_bundle": {"sha256": "a" * 64},
               "findings": [FINDING, FINDING]}
    value = _decision(pending, "valid")
    assert len(value["decisions"]) == 1
    assert len(actionable(value, pending["reviewed_head"], pending["recon_evidence_bundle"], pending["findings"])) == 2


def test_duplicate_json_keys_and_symlinks_are_rejected(tmp_path):
    path = tmp_path / "decision.json"
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(AdjudicationError):
        read_decision(path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(AdjudicationError):
        read_decision(link)


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(valid_finding_count=1),
    lambda row: row.update(valid_finding_count=False),
    lambda row: row.pop("adjudication_sha256"),
    lambda row: row.update(adjudication_sha256="0" * 64),
    lambda row: row.update(recon_evidence_bundle=row["evidence_bundle"]),
    lambda row: row.update(finding_count=0, findings=[]),
], ids=["count", "bool-count", "missing-decision", "digest", "replayed-bundle", "erased-raw"])
def test_state_and_publication_reject_forged_desk_count(tmp_path, mutate):
    repo, args = _setup(tmp_path)
    assert exhaustive.run(args) == 2
    _accept(args, _state(args.state_dir)["pending_round"])
    assert exhaustive.run(args) == 2
    state = _state(args.state_dir)
    mutate(state["ledger"][0])
    path = next(args.state_dir.glob("*/state.json"))
    path.write_text(json.dumps(state))
    with pytest.raises(exhaustive.Deferred):
        exhaustive._load_state(path, exhaustive._identity(repo)[1])
    with pytest.raises(PublicationError):
        ledger_body(state, "fixture/repo")


def test_empty_recon_does_not_require_desk_file(tmp_path):
    _, args = _setup(tmp_path)
    args._fake_responses = _responses(tmp_path)
    assert exhaustive.run(args) == 2
    assert _state(args.state_dir)["consecutive_green"] == 1
    assert not list(args.desk_adjudications.iterdir())


def test_selected_desk_directory_cannot_change_during_pending_round(tmp_path, monkeypatch):
    _, args = _setup(tmp_path)
    assert exhaustive.run(args) == 2
    before = _state(args.state_dir)
    args.desk_adjudications = tmp_path / "different-desk"
    monkeypatch.setattr(exhaustive, "_recon", lambda *a: pytest.fail("recon repeated after request changed"))
    assert exhaustive.run(args) == 2
    assert _state(args.state_dir) == before


def test_repository_cannot_supply_its_own_desk_directory(tmp_path):
    repo, args = _setup(tmp_path)
    args.desk_adjudications = repo / "desk"
    assert exhaustive.run(args) == 2
    assert not list(args.state_dir.glob("*/state.json"))


def test_real_budget_survives_missing_decisions_and_unavailable_live_suite(tmp_path, monkeypatch):
    from fl4write.analyzer import _call_model
    from fl4write.config import load_config
    from fl4write.model_proxy import ModelProxy

    repo, args = _setup(tmp_path)
    args._fake_responses = None
    args.max_model_calls, args.max_output_tokens = 5, 50
    config = load_config(repo / ".fl4write.yaml")
    forwarded = []

    def upstream(self, payload):
        prompt = payload["messages"][1]["content"]
        forwarded.append(prompt)
        findings = []
        if prompt != "live suite fixture" and json.loads(prompt)["path"] == "value.py":
            findings = [FINDING]
        return {"choices": [{"message": {"content": json.dumps({"findings": findings})}, "finish_reason": "stop"}]}

    monkeypatch.setattr(ModelProxy, "_forward", upstream)
    attempts = []

    def suite(command, tree, evidence, timeout):
        assert _call_model(config.model, "live suite fixture") == '{"findings": []}'
        attempts.append(1)
        if len(attempts) == 1:
            raise exhaustive.Deferred("suite unavailable after reserved inference")
        evidence.write_text('<testsuite tests="1"><testcase classname="fixture" name="live"/></testsuite>')
        ids, green, digest = exhaustive._junit(evidence)
        assert green
        return ids, digest

    monkeypatch.setattr(exhaustive, "_test", suite)
    assert exhaustive.run(args) == 2
    pending = _state(args.state_dir)["pending_round"]
    budget = next(args.state_dir.glob("*/budgets/round-0001.json"))
    assert json.loads(budget.read_text())["usage"]["calls"] == 3
    assert exhaustive.run(args) == 2  # Missing-file retry spends nothing.
    assert _state(args.state_dir)["pending_round"]["recon_evidence_bundle"] == pending["recon_evidence_bundle"]
    assert len(forwarded) == 3
    _accept(args, pending)
    assert exhaustive.run(args) == 2  # First suite unavailable; keep its reservation.
    assert json.loads(budget.read_text())["usage"]["calls"] == 4
    assert exhaustive.run(args) == 2
    row = _state(args.state_dir)["ledger"][0]
    assert row["green"] and row["model_usage"] == {"calls": 5, "reserved_output_tokens": 50}
    assert json.loads(budget.read_text())["usage"] == row["model_usage"]
    assert len(forwarded) == 5 and len(attempts) == 2


@pytest.mark.parametrize("failure", ["failed-suite", "missing-baseline"])
def test_invalid_decisions_never_waive_failed_suite_or_regression(tmp_path, monkeypatch, failure):
    _, args = _setup(tmp_path)
    args._fake_responses = _responses(tmp_path)
    assert exhaustive.run(args) == 2  # Establish a real prior green test identity.
    args._fake_responses = _responses(tmp_path, [{"findings": []}, {"findings": []}, {"findings": [FINDING]}])
    assert exhaustive.run(args) == 2
    assert _state(args.state_dir)["consecutive_green"] == 1
    _accept(args, _state(args.state_dir)["pending_round"])

    def suite(command, tree, evidence, timeout):
        outcome = '<failure/>' if failure == "failed-suite" else ''
        evidence.write_text(f'<testsuite tests="1"><testcase classname="different" name="test">{outcome}</testcase></testsuite>')
        ids, green, digest = exhaustive._junit(evidence)
        if not green:
            raise exhaustive.NonGreen("full suite failed", {"test_ids": sorted(ids), "junit_sha256": digest})
        return ids, digest

    monkeypatch.setattr(exhaustive, "_test", suite)
    assert exhaustive.run(args) == 2
    state = _state(args.state_dir)
    assert state["consecutive_green"] == 0 and state["ledger"][-1]["green"] is False
    assert state["ledger"][-1]["valid_finding_count"] == 0

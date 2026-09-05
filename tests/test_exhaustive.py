from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
import pytest
from fl4write import exhaustive


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "value.py").write_text("VALUE = 1\n")
    (repo / "test_value.py").write_text("from value import VALUE\n\ndef test_value():\n    assert VALUE > 0\n")
    (repo / ".fl4write.yaml").write_text("""repo: fixture/repo
forges:
  local:
    role: primary
    api_base: http://127.0.0.1:9/api
    token_env: UNUSED_FORGE_TOKEN
model:
  endpoint: http://127.0.0.1:9/v1
  model: fake
  max_tokens: 10
review: {}
""")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _responses(tmp_path: Path, values: list[dict] | None = None) -> Path:
    path = tmp_path / f"responses-{len(list(tmp_path.glob('responses-*')))}.json"
    path.write_text(json.dumps(values or [{"findings": []}] * 3))
    return path


def _args(repo: Path, state: Path, fake: Path, rounds=1, test_command=None):
    return argparse.Namespace(
        repo=repo,
        state_dir=state,
        config=None,
        test_command=test_command or [sys.executable, "-m", "pytest", "-q", "--junitxml", "{junit}"],
        max_rounds=rounds,
        round_cap=12,
        max_model_calls=10,
        max_output_tokens=100,
        chunk_chars=48_000,
        process_timeout=30,
        test_timeout=60,
        _fake_responses=fake,
    )


def _state(state: Path):
    return json.loads(next(state.glob("*/state.json")).read_text())


def test_real_snapshot_fixed_subprocess_and_coverage_certify(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    fake = _responses(tmp_path)
    assert exhaustive.run(_args(repo, state_dir, fake, rounds=3)) == 0
    state = _state(state_dir)
    assert state["consecutive_green"] == 3
    assert state["certified_sha"] == _git(repo, "rev-parse", "HEAD")
    entries = json.loads(Path(state["ledger"][0]["coverage_manifest"]).read_text())["entries"]
    assert {e["path"] for e in entries} == {".fl4write.yaml", "test_value.py", "value.py"}
    assert state["ledger"][0]["model_usage"] == {"calls": 3, "reserved_output_tokens": 30}
    assert state["ledger"][0]["junit_sha256"] and state["ledger"][0]["test_ids"]


def test_budget_is_reserved_before_call(tmp_path: Path):
    repo = _repo(tmp_path)
    args = _args(repo, tmp_path / "state", _responses(tmp_path))
    args.max_output_tokens = 29
    assert exhaustive.run(args) == 2
    assert not list((tmp_path / "state").glob("*/state.json"))


@pytest.mark.parametrize("baseline", [["ok", 1], ["ok", "ok"], [""], [" "], [{}]])
def test_malformed_green_baseline_defers_at_run_boundary(tmp_path: Path, baseline):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    args = _args(repo, state_dir, _responses(tmp_path))
    assert exhaustive.run(args) == 2
    path = next(state_dir.glob("*/state.json"))
    state = json.loads(path.read_text())
    state["green_baseline"] = baseline
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    assert exhaustive.run(args) == 2
    assert path.read_bytes() == before


def test_finding_is_grounded_and_resets_before_escalation(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path))) == 2
    values = [
        {"findings": []},
        {"findings": []},
        {
            "findings": [
                {"path": "value.py", "line": 1, "evidence": "VALUE = 1", "severity": "Major", "message": "bad value"}
            ]
        },
    ]
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path, values))) == 2
    state = _state(state_dir)
    assert state["consecutive_green"] == 0
    assert state["ledger"][-1]["finding_count"] == 1 and state["pending_round"] is None
    assert "owned-PR" in state["ledger"][-1]["reason"]


def test_recovery_cannot_forget_pending_findings(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    _, identity = exhaustive._identity(repo)
    target = state_dir / identity / "state.json"
    state = exhaustive._fresh_state(identity)
    state["pending_round"] = {"reviewed_head": _git(repo, "rev-parse", "HEAD"), "finding_count": 1}
    exhaustive._atomic_json(target, state)
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path))) == 2
    assert _state(state_dir)["ledger"][0]["reason"] == "recovered interrupted pending round"


def test_failed_suite_after_two_green_resets(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path), rounds=2)) == 2
    bad = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path(r'{junit}').write_text('''<testsuite tests=\"1\" failures=\"1\"><testcase name=\"x\"/></testsuite>''')",
    ]
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path), test_command=bad)) == 2
    assert _state(state_dir)["consecutive_green"] == 0


@pytest.mark.parametrize("xml", ['<testsuite tests="x"><testcase name="x"/></testsuite>', "<broken>"])
def test_malformed_junit_is_contained(tmp_path: Path, xml: str):
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    with pytest.raises(exhaustive.NonGreen):
        exhaustive._junit(path)


@pytest.mark.parametrize("attribute", ["failures", "errors", "skipped"])
def test_aggregate_bad_junit_is_rejected_without_child_nodes(tmp_path: Path, attribute: str):
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuite tests="1" {attribute}="1"><testcase name="x"/></testsuite>')
    assert exhaustive._junit(path)[1] is False


def test_head_drift_during_test_resets(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    script = tmp_path / "drift.py"
    script.write_text(f"""import subprocess, sys
from pathlib import Path
repo=Path({str(repo)!r}); (repo/'drift').write_text('x'); subprocess.run(['git','add','.'],cwd=repo); subprocess.run(['git','commit','-qm','drift'],cwd=repo)
Path(sys.argv[1]).write_text('<testsuite tests="1"><testcase name="x"/></testsuite>')
""")
    assert (
        exhaustive.run(
            _args(repo, state_dir, _responses(tmp_path), test_command=[sys.executable, str(script), "{junit}"])
        )
        == 2
    )
    assert _state(state_dir)["ledger"][-1]["reason"] == "HEAD changed during tests"


def test_certificate_invalidates_when_head_moves(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path), rounds=3)) == 0
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "move")
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path))) == 2
    state = _state(state_dir)
    assert state["certified_sha"] is None
    cert = json.loads(next(state_dir.glob("*/certification.json")).read_text())
    assert cert["status"] == "invalidated"


def test_corrupt_impossible_state_refused(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    _, identity = exhaustive._identity(repo)
    state = exhaustive._fresh_state(identity)
    state["consecutive_green"] = 4
    target = state_dir / identity / "state.json"
    exhaustive._atomic_json(target, state)
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path))) == 2
    assert json.loads(target.read_text())["consecutive_green"] == 4


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state, head: state.update(round=1, ledger=[None]),
        lambda state, head: state.update(round=1, ledger=[{"round": 2, "reviewed_head": head, "green": False, "reason": "x"}]),
        lambda state, head: state.update(pending_round={"reviewed_head": 7, "finding_count": 1}),
        lambda state, head: state.update(pending_round={"reviewed_head": head, "finding_count": True}),
    ],
)
def test_malformed_ledger_and_pending_rows_fail_closed(tmp_path: Path, mutation):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    _, identity = exhaustive._identity(repo)
    state = exhaustive._fresh_state(identity)
    mutation(state, _git(repo, "rev-parse", "HEAD"))
    target = state_dir / identity / "state.json"
    exhaustive._atomic_json(target, state)
    before = target.read_bytes()
    assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path))) == 2
    assert target.read_bytes() == before


def test_worker_receives_only_selected_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    config = repo / ".fl4write.yaml"
    config.write_text(config.read_text().replace("  max_tokens: 10", "  max_tokens: 10\n  key_env: SELECTED_KEY"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "credential route")
    monkeypatch.setenv("SELECTED_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    assert exhaustive.run(_args(repo, tmp_path / "state", _responses(tmp_path))) == 2


def test_same_repository_lock_refuses_concurrent_loop(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    _, identity = exhaustive._identity(repo)
    with exhaustive.CycleLock(state_dir / identity / "loop.lock"):
        assert exhaustive.run(_args(repo, state_dir, _responses(tmp_path))) == 3


class _Forge:
    name = "github"

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.bodies = []

    def update_issue(self, repo, number, body):
        self.bodies.append((repo, number, body))
        return self.outcomes.pop(0) if self.outcomes else True


def test_quarantined_publication_never_calls_forge_or_advances_state(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    forge = _Forge()
    args = _args(repo, state_dir, _responses(tmp_path), rounds=3)
    args.ledger_issue = 13
    args._forge_adapter = forge
    assert exhaustive.run(args) == 2
    assert forge.bodies == []
    assert not list(state_dir.glob("*/state.json"))


def test_quarantined_publication_retry_stays_disabled(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    forge = _Forge([True, True, False])
    args = _args(repo, state_dir, _responses(tmp_path), rounds=3)
    args.ledger_issue = 13
    args._forge_adapter = forge
    assert exhaustive.run(args) == 2
    retry = _args(repo, state_dir, _responses(tmp_path), rounds=1)
    retry.ledger_issue = 13
    retry._forge_adapter = forge
    assert exhaustive.run(retry) == 2
    assert forge.bodies == []
    with pytest.raises(exhaustive.Deferred, match="quarantined"):
        exhaustive._publish(forge, "fixture/repo", 13, exhaustive._fresh_state("repo"))
    assert forge.bodies == []


def test_public_ledger_scrubs_credentials():
    state = exhaustive._fresh_state("repo")
    state["round"] = 1
    state["ledger"] = [{"round": 1, "reason": "token ghp_" + "abcdefghijklmnopqrstuvwxyz123456"}]
    body = exhaustive._ledger_body(state)
    assert "ghp_" not in body and "[redacted]" in body


def test_public_ledger_drops_private_artifact_paths():
    state = exhaustive._fresh_state("repo")
    state["round"] = 1
    state["ledger"] = [{
        "round": 1, "reviewed_head": "a" * 40, "green": False, "reason": "x",
        "pack_manifest": "/Users/operator/private/artifacts/manifest.json",
    }]
    body = exhaustive._ledger_body(state)
    assert "/Users/" not in body and '"pack_manifest": "manifest.json"' in body


def test_forgejo_fix_is_explicitly_deferred(tmp_path: Path):
    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    values = [{"findings": []}, {"findings": []}, {"findings": [
        {"path": "value.py", "line": 1, "evidence": "VALUE = 1", "severity": "Major", "message": "bad"}
    ]}]
    args = _args(repo, state_dir, _responses(tmp_path, values))
    args.enable_fixes = True
    assert exhaustive.run(args) == 2
    assert "Forgejo exhaustive fix adapter unsupported" in _state(state_dir)["ledger"][-1]["reason"]

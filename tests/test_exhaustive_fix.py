from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from fl4write.config import RepoConfig
from fl4write import exhaustive_fix as ef
from fl4write.exhaustive import NonGreen, _junit


def _run(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _run("git", "init", "-b", "main", cwd=repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("def test_old(): assert True\n")
    _run("git", "add", ".", cwd=repo)
    _run("git", "-c", "user.name=T", "-c", "user.email=t@invalid",
         "commit", "-m", "initial", cwd=repo)
    return repo, _run("git", "rev-parse", "HEAD", cwd=repo)


def _config(*, github: bool = True, enabled: bool = True) -> RepoConfig:
    return RepoConfig.model_validate({
        "repo": "acme/widget",
        "forges": {"origin": {"role": "primary",
            "api_base": "https://api.github.com" if github else "https://forge.example/api/v1",
            "token_env": "TEST_FORGE_TOKEN"}},
        "model": {"endpoint": "https://model.invalid/v1", "model": "test"},
        "fix": {"enabled": enabled, "merge_own_prs": True},
        "bot_login": "fl4write[bot]",
    })


def _verify(command, tree: Path, junit: Path, timeout):
    del command, timeout
    source = (tree / "calc.py").read_text()
    pin = tree / "tests" / "test_regression.py"
    junit.parent.mkdir(parents=True, exist_ok=True)
    red = pin.exists() and "return a - b" in source
    extra = ("<testcase classname='tests/test_regression.py' name='test_add'>"
             + ("<failure/>" if red else "") + "</testcase>") if pin.exists() else ""
    junit.write_text("<testsuite><testcase classname='tests/test_old.py' name='test_old'/>"
                     + extra + "</testsuite>")
    ids, _, digest = _junit(junit)
    if red:
        raise NonGreen("regression failed as expected")
    return ids, digest


def _patch():
    return ({
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_regression.py": "from calc import add\n\ndef test_add(): assert add(2, 1) == 3\n",
    }, {"tests/test_regression.py"})


def test_structured_patch_requires_multiple_files_and_pin():
    with pytest.raises(ef.FixError, match="2-24"):
        ef._parse_patch(json.dumps({"files": [
            {"path": "a.py", "content": "x", "regression": True}]}))
    with pytest.raises(ef.FixError, match="regression and implementation"):
        ef._parse_patch(json.dumps({"files": [
            {"path": "a.py", "content": "x", "regression": False},
            {"path": "b.py", "content": "y", "regression": False}]}))


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a\\b", "a\x00b"])
def test_structured_patch_rejects_unsafe_paths(path):
    with pytest.raises(ef.FixError, match="unsafe"):
        ef._parse_patch(json.dumps({"files": [
            {"path": path, "content": "x", "regression": True},
            {"path": "good.py", "content": "y", "regression": False}]}))


def test_real_git_patch_proves_red_pin_green_fix_and_preserves_baseline(tmp_path):
    repo, head = _repo(tmp_path)
    fixed, ids, digest, baseline = ef._prove_patch(
        repo, head, *_patch(), ["pytest"], tmp_path / "evidence", _verify)
    assert (fixed / "calc.py").read_text().endswith("return a + b\n")
    assert baseline == {"tests/test_old.py::test_old"}
    assert baseline < ids
    assert len(digest) == 64


def test_pin_that_does_not_fail_original_is_rejected(tmp_path):
    repo, head = _repo(tmp_path)
    def always_green(command, tree, junit, timeout):
        return {"old"}, "a" * 64
    with pytest.raises(ef.FixError, match="did not fail"):
        ef._prove_patch(repo, head, *_patch(), ["pytest"], tmp_path / "e", always_green)


def test_runner_outage_cannot_prove_regression(tmp_path):
    repo, head = _repo(tmp_path)
    def verifier(command, tree, junit, timeout):
        if tree.name == "pin-only":
            raise OSError("runner unavailable")
        return _verify(command, tree, junit, timeout)
    with pytest.raises(ef.FixError, match="runner failure"):
        ef._prove_patch(repo, head, *_patch(), ["pytest"], tmp_path / "e", verifier)


def test_mutating_test_cannot_change_the_committed_fix(tmp_path):
    repo, head = _repo(tmp_path)
    def verifier(command, tree, junit, timeout):
        result = _verify(command, tree, junit, timeout)
        if tree.name == "fixed":
            (tree / "calc.py").write_text("unreviewed mutation")
        return result
    with pytest.raises(ef.FixError, match="mutated"):
        ef._prove_patch(repo, head, *_patch(), ["pytest"], tmp_path / "e", verifier)


def test_prepared_patch_retry_does_not_call_model(tmp_path, monkeypatch):
    repo, head = _repo(tmp_path)
    evidence = tmp_path / "e"
    evidence.mkdir()
    monkeypatch.setattr(ef, "_model_patch", lambda *a: _patch())
    first = ef._prepared_patch(_config(), head, [], repo, evidence)
    monkeypatch.setattr(ef, "_model_patch", lambda *a: pytest.fail("repeated model call"))
    assert ef._prepared_patch(_config(), head, [], repo, evidence) == first


def test_forgejo_merge_uses_live_contract_and_reads_empty_response():
    forge = ef._Forge(_config(github=False).forges["origin"], "unused")
    calls = []
    def call(method, path, data=None):
        calls.append((method, path, data))
        if method == "POST":
            assert data == {"Do": "squash", "head_commit_id": "a" * 40}
            return {}
        return {"merged": True, "head": {"sha": "a" * 40}, "merge_commit_sha": "b" * 40}
    forge.call = call
    assert forge.merge("acme/widget", 7, "a" * 40) == {"merged": True, "sha": "b" * 40}
    assert [c[0] for c in calls] == ["POST", "GET"]


def test_baseline_loss_is_rejected(tmp_path):
    repo, head = _repo(tmp_path)
    calls = 0
    def verifier(command, tree, junit, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _verify(command, tree, junit, timeout)
        if calls == 2:
            return _verify(command, tree, junit, timeout)
        return {"new"}, "b" * 64
    with pytest.raises(ef.FixError, match="baseline"):
        ef._prove_patch(repo, head, *_patch(), ["pytest"], tmp_path / "e", verifier)


def test_capability_gate_precedes_model_and_credentials(tmp_path, monkeypatch):
    repo, head = _repo(tmp_path)
    monkeypatch.setattr(ef, "_model_patch", lambda *a: pytest.fail("model called"))
    monkeypatch.setattr(ef, "_credential", lambda *a: pytest.fail("credential acquired"))
    result = ef.attempt_fix_with_regression_pin(
        repo, _config(enabled=False), head, [{"id": "F1"}], ["pytest"],
        tmp_path / "e", verify_suite=_verify)
    assert result["status"] == "blocked"


def test_local_head_drift_defers_before_model_or_auth(tmp_path, monkeypatch):
    repo, head = _repo(tmp_path)
    (repo / "later").write_text("x")
    _run("git", "add", ".", cwd=repo)
    _run("git", "-c", "user.name=T", "-c", "user.email=t@invalid",
         "commit", "-m", "later", cwd=repo)
    monkeypatch.setattr(ef, "_model_patch", lambda *a: pytest.fail("model called"))
    result = ef.attempt_fix_with_regression_pin(
        repo, _config(), head, [{"id": "F1"}], ["pytest"], tmp_path / "e",
        verify_suite=_verify)
    assert result["status"] == "pending"


class _FakeForge:
    commit = ""
    merged = "e" * 40
    existing = None
    ci = True
    identity = "fl4write[bot]"
    fork = False
    base = ""
    merge_response = {"merged": True, "sha": "e" * 40}
    created = 0
    merged_calls = 0
    has_existing = False

    def __init__(self, binding, token): pass
    def repo(self, repo):
        return {"full_name": repo, "default_branch": "main", "fork": self.fork}
    def user(self): return self.identity
    def head(self, repo, branch):
        return self.merged if self.merged_calls else self.base
    def find_pr(self, repo, branch):
        if self.existing is not None:
            return self.existing
        if not self.has_existing and not self.created:
            return None
        return {"number": 7, "html_url": "https://forge/pr/7",
            "user": {"login": self.identity}, "head": {"sha": self.commit, "ref": branch},
            "base": {"sha": self.base}}
    def create_pr(self, repo, branch, base, title, body):
        type(self).created += 1
        return self.find_pr(repo, branch)
    def checks_green(self, repo, sha): return self.ci
    def merge(self, repo, number, sha):
        type(self).merged_calls += 1
        return self.merge_response


def _flow(tmp_path, monkeypatch, forge=_FakeForge, github=True):
    repo, head = _repo(tmp_path)
    forge.base = head
    forge.merged_calls = forge.created = 0
    forge.merged = "e" * 40
    if forge is _FakeForge:
        forge.existing = None
        forge.ci = True
        forge.identity = "fl4write[bot]"
        forge.fork = False
        forge.has_existing = False
    monkeypatch.setattr(ef, "_model_patch", lambda *a: _patch())
    monkeypatch.setattr(ef, "_Forge", forge)
    @contextlib.contextmanager
    def credential(config, binding):
        yield "secret-token"
    monkeypatch.setattr(ef, "_credential", credential)
    real_git = ef._git
    def git(args, cwd=None, env=None, timeout=120):
        if args[0] == "push":
            forge.commit = args[-1].split(":", 1)[0]
            assert env["FL4WRITE_PUSH_TOKEN"] == "secret-token"
            return ""
        return real_git(args, cwd, env, timeout)
    monkeypatch.setattr(ef, "_git", git)
    result = ef.attempt_fix_with_regression_pin(
        repo, _config(github=github), head, [{"id": "F1", "path": "calc.py"}], ["pytest"],
        tmp_path / "evidence", verify_suite=_verify)
    return result, forge


@pytest.mark.parametrize("github", [True, False])
def test_verified_bot_owned_flow_supports_github_and_forgejo(tmp_path, monkeypatch, github):
    result, forge = _flow(tmp_path, monkeypatch, github=github)
    assert result["status"] == "merged", result["reason"]
    assert result["merged_head"] == "e" * 40
    assert result["regression_paths"] == ["tests/test_regression.py"]
    assert forge.merged_calls == 1
    assert json.loads((tmp_path / "evidence" / "exhaustive-fix.json").read_text())["status"] == "merged"


@pytest.mark.parametrize("failure,reason", [
    ({"fork": True}, "non-fork"),
    ({"identity": "human"}, "configured bot"),
])
def test_identity_and_fork_rails_block_before_push(tmp_path, monkeypatch, failure, reason):
    class Bad(_FakeForge):
        pass
    for key, value in failure.items():
        setattr(Bad, key, value)
    result, forge = _flow(tmp_path / "second", monkeypatch, Bad)
    assert result["status"] == "blocked"
    assert reason in result["reason"]
    assert forge.merged_calls == 0


def test_unknown_ci_is_durable_pending_and_never_merges(tmp_path, monkeypatch):
    class Pending(_FakeForge):
        ci = None
    result, forge = _flow(tmp_path, monkeypatch, Pending)
    assert result["status"] == "pending"
    assert "unqueryable" in result["reason"]
    assert forge.merged_calls == 0
    assert (tmp_path / "evidence" / "exhaustive-fix.json").exists()


def test_remote_base_drift_defers_without_push_or_merge(tmp_path, monkeypatch):
    class Drift(_FakeForge):
        def head(self, repo, branch):
            return "d" * 40
    result, forge = _flow(tmp_path, monkeypatch, Drift)
    assert result["status"] == "pending"
    assert "drifted" in result["reason"]
    assert forge.merged_calls == 0


def test_unproven_merge_is_pending(tmp_path, monkeypatch):
    class Unknown(_FakeForge):
        merge_response = {"message": "accepted"}
    result, forge = _flow(tmp_path, monkeypatch, Unknown)
    assert result["status"] == "pending"
    assert "not proven" in result["reason"]


def test_scoped_credential_restores_existing_env_on_error(monkeypatch):
    config = _config(github=False)
    binding = config.forges["origin"]
    monkeypatch.setenv("TEST_FORGE_TOKEN", "dedicated-token")
    with pytest.raises(RuntimeError):
        with ef._credential(config, binding) as token:
            assert token == "dedicated-token"
            assert "TEST_FORGE_TOKEN" not in os.environ
            raise RuntimeError("boom")
    assert os.environ["TEST_FORGE_TOKEN"] == "dedicated-token"


def test_askpass_secret_is_removed_on_exception():
    helper = None
    with pytest.raises(RuntimeError):
        with ef._askpass("one-shot-token") as env:
            helper = Path(env["GIT_ASKPASS"])
            assert helper.exists()
            assert "one-shot-token" not in helper.read_text()
            raise RuntimeError("boom")
    assert helper is not None and not helper.exists()
    assert "FL4WRITE_PUSH_TOKEN" not in os.environ


def test_existing_foreign_pr_is_never_mutated(tmp_path, monkeypatch):
    class Foreign(_FakeForge):
        existing = {"number": 8, "html_url": "https://forge/pr/8",
            "user": {"login": "human"}, "head": {"sha": "0" * 40, "ref": "x"},
            "base": {"sha": "1" * 40}}
    result, forge = _flow(tmp_path, monkeypatch, Foreign)
    assert result["status"] == "blocked"
    assert forge.merged_calls == 0


def test_idempotent_existing_owned_pr_is_reused(tmp_path, monkeypatch):
    class Existing(_FakeForge):
        has_existing = True
    result, forge = _flow(tmp_path, monkeypatch, Existing)
    assert result["status"] == "merged"
    assert forge.created == 0

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


@pytest.mark.parametrize("branch", ["main", "feature/exhaustive-loop-draft"])
def test_forgejo_head_uses_branch_endpoint_and_commit_id(monkeypatch, branch):
    forge = ef._Forge(_config(github=False).forges["origin"], "synthetic-token")
    calls = []
    monkeypatch.setattr(forge, "call", lambda method, path:
                        calls.append((method, path)) or {"name": branch, "commit": {"id": "a" * 40}})
    assert forge.head("acme/widget", branch) == "a" * 40
    assert calls == [("GET", "/repos/acme/widget/branches/" + branch.replace("/", "%2F"))]


@pytest.mark.parametrize("row", [{"name": "other", "commit": {"id": "a" * 40}},
                                  {"name": "main", "commit": {"id": "invalid"}}])
def test_forgejo_head_rejects_wrong_branch_or_invalid_commit(monkeypatch, row):
    forge = ef._Forge(_config(github=False).forges["origin"], "synthetic-token")
    monkeypatch.setattr(forge, "call", lambda *args: row)
    with pytest.raises(ef.FixError, match="branch head"):
        forge.head("acme/widget", "main")


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


def test_compact_patch_expands_only_unique_supplied_source_fragments():
    files, pins = ef._parse_patch(json.dumps({"files": [
        {"path": "calc.py", "edits": [{"old": "return a - b", "new": "return a + b"}],
         "regression": False},
        {"path": "test_calc.py", "content": "assert add(2, 1) == 3\n", "regression": True},
    ]}), {"calc.py": "def add(a, b):\n    return a - b\n"})
    assert files["calc.py"] == "def add(a, b):\n    return a + b\n"
    assert pins == {"test_calc.py"}


@pytest.mark.parametrize("source,old", [("return 1\nreturn 1\n", "return 1"),
                                        ("aaaa", "aaa"),
                                        ("return 1\n", "missing"), ("return 1\n", "")])
def test_compact_patch_rejects_missing_empty_or_ambiguous_context(source, old):
    with pytest.raises(ef.FixError, match="exactly one"):
        ef._parse_patch(json.dumps({"files": [
            {"path": "calc.py", "edits": [{"old": old, "new": "return 2"}], "regression": False},
            {"path": "test_calc.py", "content": "assert True", "regression": True},
        ]}), {"calc.py": source})


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a\\b", "a\x00b"])
def test_structured_patch_rejects_unsafe_paths(path):
    with pytest.raises(ef.FixError, match="unsafe"):
        ef._parse_patch(json.dumps({"files": [
            {"path": path, "content": "x", "regression": True},
            {"path": "good.py", "content": "y", "regression": False}]}))


@pytest.mark.parametrize("path", [".git/config", ".GIT/config", ".git /config"])
def test_model_patch_excludes_repository_metadata(path):
    with pytest.raises(ef.FixError, match="metadata"):
        ef._safe_path(path)


def test_git_normalization_cannot_change_tested_bytes_before_commit(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / ".gitattributes").write_text("calc.py text eol=lf\n")
    (repo / "calc.py").write_bytes(b"def add(a, b):\r\n    return a + b\r\n")
    _run("git", "add", ".gitattributes", "calc.py", cwd=repo)
    with pytest.raises(ef.FixError, match="staged source bytes"):
        ef._assert_index_matches_worktree(repo, {"calc.py"})


def test_real_git_patch_proves_red_pin_green_fix_and_preserves_baseline(tmp_path):
    repo, head = _repo(tmp_path)
    timeouts = []
    def verify(command, tree, junit, timeout):
        timeouts.append(timeout)
        return _verify(command, tree, junit, timeout)
    fixed, ids, digest, baseline = ef._prove_patch(
        repo, head, *_patch(), ["pytest"], tmp_path / "evidence", verify, test_timeout=17)
    assert timeouts == [17, 17, 17]
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
    with pytest.raises(ef.FixError, match="another repair request"):
        ef._prepared_patch(_config(), head, [{"id": "new finding"}], repo, evidence)


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


def test_actions_only_ci_can_be_green_without_legacy_status_contexts():
    forge = ef._Forge(_config().forges["origin"], "synthetic-token")
    def call(method, path, data=None):
        if "/actions/runs" in path:
            return {"total_count": 1, "workflow_runs": [
                {"head_sha": "a" * 40, "status": "completed", "conclusion": "success"}]}
        return {"total_count": 0, "statuses": [], "state": "pending"}
    forge.call = call
    assert forge.checks_green("acme/widget", "a" * 40) is True


def test_forgejo_ci_uses_latest_status_for_each_context():
    forge = ef._Forge(_config(github=False).forges["origin"], "synthetic-token")
    forge.call = lambda *a: [
        {"id": 2, "context": "tests", "status": "success"},
        {"id": 1, "context": "tests", "status": "pending"},
    ]
    assert forge.checks_green("acme/widget", "a" * 40) is True


def test_actions_enumeration_cannot_silently_drop_runs():
    forge = ef._Forge(_config().forges["origin"], "synthetic-token")
    forge.call = lambda *a: {"total_count": 2, "workflow_runs": [
        {"head_sha": "a" * 40, "status": "completed", "conclusion": "success"}]}
    with pytest.raises(ef.FixError, match="incomplete"):
        forge.checks_green("acme/widget", "a" * 40)


def test_required_forgejo_context_cannot_be_replaced_by_unrelated_green_status():
    forge = ef._Forge(_config(github=False).forges["origin"], "synthetic-token")
    def call(method, path, data=None):
        if "/branches/" in path:
            return {"name": "main", "protected": True, "enable_status_check": True,
                    "status_check_contexts": ["required-suite"]}
        return [{"id": 1, "context": "unrelated", "status": "success"}]
    forge.call = call
    required = forge.required_contexts("acme/widget", "main")
    assert required == {"required-suite"}
    assert forge.checks_green("acme/widget", "a" * 40, required) is None


def test_unqueryable_app_bound_requirement_cannot_approve_merge():
    forge = ef._Forge(_config().forges["origin"], "synthetic-token")
    def call(method, path, data=None):
        if path.endswith("/branches/main"):
            return {"name": "main", "protected": True}
        return {"contexts": ["tests"], "checks": [{"context": "tests", "app_id": 17}]}
    forge.call = call
    with pytest.raises(ef.FixError, match="App-bound"):
        forge.required_contexts("acme/widget", "main")


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
    target = "main"

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
            "user": {"login": self.identity},
            "head": {"sha": self.commit, "ref": branch, "repo": {"full_name": repo}},
            "base": {"sha": self.base, "ref": self.target, "repo": {"full_name": repo}}}
    def create_pr(self, repo, branch, base, title, body):
        type(self).created += 1
        type(self).target = base
        return self.find_pr(repo, branch)
    def required_contexts(self, repo, branch): return set()
    def checks_green(self, repo, sha, required=None): return self.ci
    def merge(self, repo, number, sha):
        type(self).merged_calls += 1
        return self.merge_response


def _flow(tmp_path, monkeypatch, forge=_FakeForge, github=True, base_branch=None):
    repo, head = _repo(tmp_path)
    forge.base = head
    forge.merged_calls = forge.created = 0
    forge.merged = "e" * 40
    forge.target = "main"
    if forge is _FakeForge:
        forge.existing = None
        forge.ci = True
        forge.identity = "fl4write[bot]"
        forge.fork = False
        forge.has_existing = False
        forge.target = "main"
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
        tmp_path / "evidence", verify_suite=_verify, base_branch=base_branch)
    return result, forge


@pytest.mark.parametrize("github", [True, False])
def test_verified_bot_owned_flow_supports_github_and_forgejo(tmp_path, monkeypatch, github):
    result, forge = _flow(tmp_path, monkeypatch, github=github)
    assert result["status"] == "merged", result["reason"]
    assert result["merged_head"] == "e" * 40
    assert result["regression_paths"] == ["tests/test_regression.py"]
    assert forge.merged_calls == 1
    assert json.loads((tmp_path / "evidence" / "exhaustive-fix.json").read_text())["status"] == "merged"


def test_explicit_base_uses_its_head_and_required_checks(tmp_path, monkeypatch):
    selected = []
    class Candidate(_FakeForge):
        def head(self, repo, branch):
            selected.append(branch)
            return super().head(repo, branch)
        def required_contexts(self, repo, branch):
            selected.append(branch)
            return set()
    result, forge = _flow(tmp_path, monkeypatch, forge=Candidate,
                          base_branch="feature/review-candidate")
    assert result["status"] == "merged", result["reason"]
    assert selected and set(selected) == {"feature/review-candidate"}
    assert forge.target == "feature/review-candidate"
    assert result["default_branch"] == "main"
    assert result["base_branch"] == "feature/review-candidate"


@pytest.mark.parametrize("field", ["branch", "base_repo", "head_repo"])
def test_same_sha_on_wrong_base_or_repository_never_merges(tmp_path, monkeypatch, field):
    class ChangedBase(_FakeForge):
        def find_pr(self, repo, branch):
            value = super().find_pr(repo, branch)
            if value:
                if field == "branch":
                    value["base"]["ref"] = "other-branch"
                elif field == "base_repo":
                    value["base"]["repo"]["full_name"] = "other/repo"
                else:
                    value["head"].pop("repo")
            return value
    result, forge = _flow(tmp_path, monkeypatch, forge=ChangedBase)
    assert result["status"] == "blocked"
    assert "ownership, head, or base" in result["reason"]
    assert forge.merged_calls == 0


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


def test_lost_merge_response_is_recovered_without_another_push_or_merge(tmp_path, monkeypatch):
    class Lost(_FakeForge):
        merge_response = {"message": "accepted"}
        def call(self, method, path, data=None):
            assert method == "GET" and path.endswith("/pulls/7")
            return {"number": 7, "merged": True, "state": "closed",
                    "user": {"login": self.identity},
                    "head": {"sha": self.commit, "repo": {"full_name": "acme/widget"}},
                    "base": {"ref": "main", "repo": {"full_name": "acme/widget"}},
                    "merge_commit_sha": self.merged}
    first, forge = _flow(tmp_path, monkeypatch, Lost)
    assert first["status"] == "pending" and first["phase"] == "merging"
    before = forge.merged_calls
    monkeypatch.setattr(ef, "_model_patch", lambda *a: pytest.fail("model repeated during merge recovery"))
    real_git = ef._git
    def no_push(args, *a, **kw):
        assert args[0] != "push", "recovery pushed an already merged branch"
        return real_git(args, *a, **kw)
    monkeypatch.setattr(ef, "_git", no_push)
    second = ef.attempt_fix_with_regression_pin(
        tmp_path / "repo", _config(), forge.base, [{"id": "F1", "path": "calc.py"}], ["pytest"],
        tmp_path / "evidence", verify_suite=_verify)
    assert second["status"] == "merged", second
    assert second["merged_head"] == forge.merged
    assert forge.merged_calls == before


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

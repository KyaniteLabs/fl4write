"""E2 (t_5a7e1b77): guard/auth/merge changes never auto-merge.

The classifier's only input is the PR's changed-file list. These tests pin the
four card behaviours at the real merge path (`check_and_merge_own_prs`):
green protected PR refused, docs-only unaffected, missing/malformed policy
fails closed, and exactly one `merge_blocked` telemetry event per refusal —
with zero writes, so a refusal can never open a fix PR.
"""

from __future__ import annotations

import json
import stat

import pytest

from fl4write import executor, fixlane, mergepolicy, telemetry
from test_fl4write import make_config

BOT = "fl4write[bot]"
HEAD = "h" * 40
PROTECTED_PATH_PATTERNS = [
    "fl4write/appauth.py",
    "fl4write/executor.py",
    "fl4write/exhaustive_fix.py",
    "fl4write/fixlane.py",
    "fl4write/forges.py",
    "fl4write/gatekeeper.py",
    "fl4write/mergepolicy.py",
    "fl4write/scrub.py",
    "fl4write/telemetry.py",
    "fl4write/writeguard.py",
    ".github/workflows/**",
]


def _config():
    return make_config(fix={"enabled": True, "merge_own_prs": True})


@pytest.fixture
def tel(tmp_path, monkeypatch):
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setattr(telemetry, "_STREAM", path)
    return path


def _policy(tmp_path, monkeypatch, doc=None, raw=None):
    path = tmp_path / "protected-paths.json"
    text = raw if raw is not None else json.dumps(
        doc if doc is not None else {"protected_paths": PROTECTED_PATH_PATTERNS})
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FL4WRITE_PROTECTED_PATHS", str(path))
    return path


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeGitHub:
    """Routes _gh_api for one green own-fix PR and records every call."""

    def __init__(self, *, files=None, file_pages=None, files_reply=None,
                 checks=None, pr_number=5, author=BOT, title="t"):
        self.files = files if files is not None else [
            {"filename": "docs/notes.md", "status": "modified",
             "patch": "@@ -1 +1 @@\n-old\n+new"}]
        self.file_pages = file_pages
        self.files_reply = files_reply
        self.checks = checks if checks is not None else [
            {"status": "completed", "conclusion": "success"}]
        self.pr = {"number": pr_number, "title": title,
                   "head": {"ref": "fl4write/fix-5-abc", "sha": HEAD},
                   "user": {"login": author}}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.puts: list[tuple[str, dict | None]] = []

    def __call__(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "PUT":
            self.puts.append((path, data))
            return {"merged": True, "sha": "m" * 40}
        assert method == "GET", (method, path)
        if "/pulls?state=open" in path:
            return [self.pr]
        if "/files?" in path:
            if isinstance(self.files_reply, Exception):
                raise self.files_reply
            if self.files_reply is not None:
                return self.files_reply
            if self.file_pages is not None:
                page = int(path.rsplit("page=", 1)[1])
                return self.file_pages[page - 1] if page <= len(self.file_pages) else []
            return self.files
        if "/check-runs" in path:
            page = int(path.rsplit("page=", 1)[1])
            return {"check_runs": self.checks if page == 1 else []}
        if path.endswith("/status"):
            return {"state": "success"}
        raise AssertionError(f"unexpected {method} {path}")


def _scan(monkeypatch, api, config=None):
    monkeypatch.setattr(executor, "_gh_api", api)
    return executor.check_and_merge_own_prs(config or _config(), BOT)


# ---------------------------------------------------------------- AC1 + AC4
def test_executor_change_with_all_checks_green_is_refused_and_stays_open(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[{"filename": "fl4write/executor.py", "status": "modified",
                             "patch": "@@ -1 +1 @@\n-x\n+y"}])
    assert _scan(monkeypatch, api) == []
    assert api.puts == []  # stays open: the merge endpoint was never called
    assert any("/check-runs" in path for _, path, _ in api.calls)  # CI was green
    events = _events(tel)
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "merge_blocked"
    assert event["repo"] == "KyaniteLabs/kinocut"
    assert event["pr"] == 5 and event["head"] == HEAD
    assert event["matched_paths"] == ["fl4write/executor.py"]
    assert event["reason"] == "protected_path"
    # AC4: a refusal is telemetry, never a target — no POST/PUT of any kind.
    assert {method for method, _, _ in api.calls} == {"GET"}


@pytest.mark.parametrize("path", [
    "fl4write/appauth.py", "fl4write/exhaustive_fix.py", "fl4write/forges.py",
    "fl4write/gatekeeper.py", "fl4write/telemetry.py", "fl4write/scrub.py",
    "fl4write/writeguard.py", "fl4write/mergepolicy.py", "fl4write/fixlane.py",
])
def test_every_named_protected_module_is_refused(tmp_path, monkeypatch, tel, path):
    _policy(tmp_path, monkeypatch, {"protected_paths": PROTECTED_PATH_PATTERNS})
    api = FakeGitHub(files=[{"filename": path, "status": "modified", "patch": "@@\n-x\n+y"}])
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    assert [e["matched_paths"] for e in _events(tel)] == [[path]]


# ---------------------------------------------------------------- AC2
def test_docs_only_pr_merges_through_the_same_code_path(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[{"filename": "README.md", "status": "modified",
                             "patch": "@@ -1 +1 @@\n-a\n+b"}])
    assert _scan(monkeypatch, api) == [{"pr": 5, "status": "merged"}]
    assert len(api.puts) == 1 and api.puts[0][0].endswith("/pulls/5/merge")
    assert _events(tel) == []  # no refusal, no event


def test_title_and_author_never_decide(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    protected = FakeGitHub(title="docs: fix a typo",
                           files=[{"filename": "fl4write/executor.py", "status": "modified",
                                   "patch": "@@\n-x\n+y"}])
    assert _scan(monkeypatch, protected) == []
    harmless = FakeGitHub(title="harden the executor guard",
                          files=[{"filename": "docs/notes.md", "status": "modified",
                                  "patch": "@@\n-x\n+y"}])
    assert _scan(monkeypatch, harmless) == [{"pr": 5, "status": "merged"}]


# ---------------------------------------------------------------- AC3
def test_missing_policy_file_refuses_every_merge(tmp_path, monkeypatch, tel):
    monkeypatch.setenv("FL4WRITE_PROTECTED_PATHS", str(tmp_path / "does-not-exist.json"))
    api = FakeGitHub()  # docs-only PR: without the policy it must STILL be refused
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    events = _events(tel)
    assert len(events) == 1 and events[0]["reason"] == "policy_unavailable"
    assert events[0]["matched_paths"] == [] and events[0]["pr"] == 5


@pytest.mark.parametrize("raw", [
    "not json at all",
    "[]",
    "{}",
    json.dumps({"protected_paths": []}),
    json.dumps({"protected_paths": "fl4write/executor.py"}),
    json.dumps({"protected_paths": [1]}),
    json.dumps({"protected_paths": ["  "]}),
])
def test_malformed_policy_refuses_every_merge(tmp_path, monkeypatch, tel, raw):
    _policy(tmp_path, monkeypatch, raw=raw)
    api = FakeGitHub()
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    assert [e["reason"] for e in _events(tel)] == ["policy_unavailable"]


def test_unreadable_policy_refuses_every_merge(tmp_path, monkeypatch, tel):
    directory = tmp_path / "policy-dir"
    directory.mkdir()
    monkeypatch.setenv("FL4WRITE_PROTECTED_PATHS", str(directory))
    api = FakeGitHub()
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    assert [e["reason"] for e in _events(tel)] == ["policy_unavailable"]


def test_empty_changed_file_list_fails_closed(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[])
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    assert [e["reason"] for e in _events(tel)] == ["files_unavailable"]


@pytest.mark.parametrize("reply", [RuntimeError("files endpoint down"), {"message": "no"}])
def test_unreadable_changed_file_list_fails_closed(tmp_path, monkeypatch, tel, reply):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files_reply=reply)
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    assert [e["reason"] for e in _events(tel)] == ["files_unavailable"]


def test_capped_changed_file_list_fails_closed(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    full_page = [{"filename": f"docs/{i}.md", "status": "modified", "patch": "@@\n-x\n+y"}
                 for i in range(100)]
    api = FakeGitHub(file_pages=[full_page] * 21)
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    assert [e["reason"] for e in _events(tel)] == ["files_unavailable"]


# ---------------------------------------------------------------- target-repo rule
def test_workflow_change_is_protected(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[{"filename": ".github/workflows/ci.yml", "status": "modified",
                             "patch": "@@\n- old\n+ new"}])
    assert _scan(monkeypatch, api) == []
    assert [e["matched_paths"] for e in _events(tel)] == [[".github/workflows/ci.yml"]]


def test_repo_config_that_changes_a_target_repo_is_protected(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[{"filename": "x.fl4write.yaml", "status": "modified",
                             "patch": "@@ -1 +1 @@\n-repo: KyaniteLabs/a\n+repo: KyaniteLabs/b"}])
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    event = _events(tel)[0]
    assert event["matched_paths"] == ["x.fl4write.yaml"]
    assert event["reason"] == "protected_path"


def test_repo_config_edit_without_a_target_change_is_unaffected(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[{"filename": "fl4write.fl4write.yaml", "status": "modified",
                             "patch": "@@ -4 +4 @@\n-  schedule: daily\n+  schedule: hourly"}])
    assert _scan(monkeypatch, api) == [{"pr": 5, "status": "merged"}]
    assert _events(tel) == []


def test_repo_config_change_without_a_patch_fails_closed(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[{"filename": "new.fl4write.yaml", "status": "modified"}])
    assert _scan(monkeypatch, api) == []
    assert [e["matched_paths"] for e in _events(tel)] == [["new.fl4write.yaml"]]


def test_renaming_a_protected_module_out_of_its_path_is_still_protected(tmp_path, monkeypatch, tel):
    _policy(tmp_path, monkeypatch)
    api = FakeGitHub(files=[{"filename": "fl4write/executor_v2.py", "status": "renamed",
                             "previous_filename": "fl4write/executor.py"}])
    assert _scan(monkeypatch, api) == []
    assert api.puts == []
    assert [e["matched_paths"] for e in _events(tel)] == [["fl4write/executor.py"]]


# ---------------------------------------------------------------- AC5: read-only
def test_policy_file_is_only_read_never_written(tmp_path, monkeypatch, tel):
    path = _policy(tmp_path, monkeypatch)
    path.chmod(0o444)
    before_mode = stat.S_IMODE(path.stat().st_mode)
    before_bytes = path.read_bytes()
    api = FakeGitHub(files=[{"filename": "fl4write/executor.py", "status": "modified",
                             "patch": "@@\n-x\n+y"}])
    assert _scan(monkeypatch, api) == []
    assert stat.S_IMODE(path.stat().st_mode) == before_mode == 0o444
    assert path.read_bytes() == before_bytes


# ---------------------------------------------------------------- gate itself
def test_gate_refuses_a_merge_without_a_decision():
    c = _config()
    with pytest.raises(fixlane.FixLaneBlocked, match="no protected-merge decision"):
        fixlane.merge_own_pr(author=BOT, bot_identity=BOT, ci_green=True, config=c)


def test_gate_refuses_a_refused_decision_and_emits_exactly_one_event(tel):
    c = _config()
    decision = mergepolicy.MergeDecision(False, "protected_path", c.repo, 9, "a" * 40,
                                         ("fl4write/executor.py",))
    with pytest.raises(fixlane.FixLaneBlocked, match="protected_path"):
        fixlane.merge_own_pr(author=BOT, bot_identity=BOT, ci_green=True, config=c,
                             merge_decision=decision)
    events = _events(tel)
    assert len(events) == 1
    assert events[0]["kind"] == "merge_blocked"
    assert events[0]["repo"] == c.repo and events[0]["pr"] == 9
    assert events[0]["head"] == "a" * 40
    assert events[0]["matched_paths"] == ["fl4write/executor.py"]

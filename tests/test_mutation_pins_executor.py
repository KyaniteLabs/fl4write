"""Mutation-testing pins for fl4write/executor.py security helpers.

A mutmut run over executor.py showed the token and containment rails had
no direct tests: dropping the token helper, breaking its permissions or
removing git hardening all went unnoticed. These rails stand between
untrusted test code and the bot's credentials.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from fl4write import executor


# ---------------------------------------------------------------- token helper
def test_push_token_env_builds_a_private_askpass_outside_the_workdir(tmp_path):
    env = executor._push_token_env(tmp_path, "tok'en")
    try:
        helper = Path(env["GIT_ASKPASS"])
        assert helper.is_file()
        assert tmp_path not in helper.parents            # never inside the tested tree
        assert Path(env["_FL4WRITE_ASKPASS_DIR"]) == helper.parent
        assert stat.S_IMODE(helper.stat().st_mode) == stat.S_IRUSR | stat.S_IXUSR
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # the helper really prints the token (shell-quoted: odd chars survive)
        out = subprocess.run([str(helper)], capture_output=True, text=True, check=True)
        assert out.stdout == "tok'en\n"
    finally:
        executor._drop_askpass(env)


def test_drop_askpass_removes_the_token_before_untrusted_code_runs(tmp_path):
    env = executor._push_token_env(tmp_path, "secret-token")
    askpass_dir = Path(env["_FL4WRITE_ASKPASS_DIR"])
    assert askpass_dir.exists()
    executor._drop_askpass(env)
    assert not askpass_dir.exists()
    assert "_FL4WRITE_ASKPASS_DIR" not in env
    executor._drop_askpass(env)  # idempotent: a second drop is harmless


# ---------------------------------------------------------------- git hardening
def test_git_hardened_env_blinds_privileged_git_to_planted_config():
    base = {"PATH": "/usr/bin", "HOME": "/tmp/sandbox-home"}
    env = executor._git_hardened_env(base)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["HOME"] == "/tmp/sandbox-home"
    assert env["PATH"] == "/usr/bin"
    assert base == {"PATH": "/usr/bin", "HOME": "/tmp/sandbox-home"}  # input untouched


def test_git_hardened_env_without_home_uses_the_sandbox_home():
    env = executor._git_hardened_env({"PATH": "/usr/bin"})
    assert env["HOME"] == executor._sandbox_home()
    assert env["HOME"] != os.path.expanduser("~")


# ---------------------------------------------------------------- contained writes
@pytest.mark.parametrize("rel", ["", "/etc/passwd", "\\evil", "a\\b.py", "../out.py", "a/../../x"])
def test_write_contained_refuses_unsafe_paths(tmp_path, rel):
    assert executor._write_contained(tmp_path, rel, "x") is not None
    assert not (tmp_path.parent / "out.py").exists()


def test_write_contained_refuses_symlinks(tmp_path):
    (tmp_path / "link.py").symlink_to(tmp_path.parent / "target.py")
    assert "symlink" in executor._write_contained(tmp_path, "link.py", "x")
    assert not (tmp_path.parent / "target.py").exists()


def test_write_contained_creates_nested_dirs_and_writes_utf8(tmp_path):
    assert executor._write_contained(tmp_path, "pkg/sub/mod.py", "café = 1\n") is None
    assert (tmp_path / "pkg/sub/mod.py").read_bytes() == "café = 1\n".encode("utf-8")


# ---------------------------------------------------------------- sandbox env
def test_sandbox_env_is_an_allowlist_with_a_throwaway_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODESITTER_GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    monkeypatch.setenv("PYTHONPATH", "/prev")
    env = executor._sandbox_env_for(str(tmp_path))
    assert env["HOME"] == str(tmp_path)
    assert not any("secret" in v for v in env.values())
    assert set(env) <= set(executor._TEST_ENV_ALLOW) | {"HOME"}
    # an existing PYTHONPATH survives behind the user site-packages dir
    assert env["PYTHONPATH"].split(os.pathsep)[-1] == "/prev"


# ---------------------------------------------------------------- auto-merge gate
from test_fl4write import make_config  # noqa: E402

BOT = "fl4write[bot]"
GREEN = [{"status": "completed", "conclusion": "success"}]


def _pr(number=5, ref="fl4write/fix-5-abc", author=BOT, sha="h" * 40):
    return {"number": number, "head": {"ref": ref, "sha": sha}, "user": {"login": author}}


class FakeForge:
    """Routes _gh_api calls; records every merge PUT."""

    def __init__(self, pages, runs=None, status="success", merge_reply=None):
        self.pages, self.runs, self.status = pages, runs or {}, status
        self.merge_reply = merge_reply if merge_reply is not None else {"merged": True, "sha": "m" * 40}
        self.puts: list = []

    def __call__(self, method, path, data=None):
        if method == "PUT":
            self.puts.append((path, data))
            return self.merge_reply
        if "/pulls?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return self.pages[page - 1] if page <= len(self.pages) else []
        if path.endswith("/status"):
            if isinstance(self.status, Exception):
                raise self.status
            return {"state": self.status}
        if "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            page = int(path.rsplit("page=", 1)[1])
            pages = self.runs.get(sha, [GREEN])
            return {"check_runs": pages[page - 1] if page <= len(pages) else []}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def merge_config():
    return make_config(fix={"enabled": True, "merge_own_prs": True})


def _scan(monkeypatch, forge, config):
    monkeypatch.setattr(executor, "_gh_api", forge)
    return executor.check_and_merge_own_prs(config, BOT)


def test_merge_gate_merges_a_green_own_fix_pr(monkeypatch, merge_config):
    forge = FakeForge([[_pr()]])
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 5, "status": "merged"}]
    path, body = forge.puts[0]
    assert path.endswith("/pulls/5/merge")
    assert body == {"merge_method": "squash", "sha": "h" * 40}   # head-sha precondition


@pytest.mark.parametrize("pr,runs,status", [
    (_pr(ref="feature/x"), None, "success"),                                  # not our fix branch
    (_pr(author="mallory"), None, "success"),                                 # not our bot
    (_pr(), {"h" * 40: [[{"status": "completed", "conclusion": "failure"}]]}, "success"),
    (_pr(), {"h" * 40: [[{"status": "in_progress"}]]}, "success"),             # pending
    (_pr(), {"h" * 40: [[]]}, "success"),                                      # no checks at all
    (_pr(), None, "failure"),                                                  # legacy status red
    (_pr(), None, RuntimeError("status probe down")),                          # status unknown
])
def test_merge_gate_refuses(monkeypatch, merge_config, pr, runs, status):
    forge = FakeForge([[pr]], runs=runs, status=status)
    assert _scan(monkeypatch, forge, merge_config) == []
    assert forge.puts == []


def test_merge_gate_off_in_config_never_merges(monkeypatch):
    forge = FakeForge([[_pr()]])
    assert _scan(monkeypatch, forge, make_config(fix={"enabled": True, "merge_own_prs": False})) == []
    assert forge.puts == []


@pytest.mark.parametrize("reply", [
    {"merged": False, "sha": "m" * 40}, {"merged": True, "sha": ""}, {"merged": True}, "ok", None,
])
def test_merge_counts_only_when_the_api_proves_it(monkeypatch, merge_config, reply):
    forge = FakeForge([[_pr()]])
    forge.merge_reply = reply
    assert _scan(monkeypatch, forge, merge_config) == []
    assert len(forge.puts) == 1


def test_malformed_rows_skip_without_killing_the_scan(monkeypatch, merge_config):
    rows = ["junk", {"number": True, "head": {"ref": "fl4write/x", "sha": "h" * 40}},
            {"number": 0}, _pr(number=9)]
    forge = FakeForge([rows])
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 9, "status": "merged"}]


def test_pr_listing_is_paginated(monkeypatch, merge_config):
    filler = [_pr(number=100 + i, ref="feature/x") for i in range(100)]
    forge = FakeForge([filler, [_pr(number=7)]])
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 7, "status": "merged"}]


def test_a_failing_check_on_page_two_blocks_the_merge(monkeypatch, merge_config):
    page1 = GREEN * 100
    page2 = [{"status": "completed", "conclusion": "failure"}]
    forge = FakeForge([[_pr()]], runs={"h" * 40: [page1, page2]})
    assert _scan(monkeypatch, forge, merge_config) == []
    assert forge.puts == []


def test_capped_check_runs_never_merge(monkeypatch, merge_config):
    forge = FakeForge([[_pr()]], runs={"h" * 40: [GREEN * 100] * 25})
    assert _scan(monkeypatch, forge, merge_config) == []
    assert forge.puts == []

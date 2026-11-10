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

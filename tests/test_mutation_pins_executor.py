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
            if isinstance(self.merge_reply, list):
                return self.merge_reply.pop(0)
            return self.merge_reply
        assert method == "GET", (method, path)
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


def _scan(monkeypatch, forge, config, bot=BOT):
    monkeypatch.setattr(executor, "_gh_api", forge)
    return executor.check_and_merge_own_prs(config, bot)


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


# ---------------------------------------------------------------- pytest argv model
_VA = executor._pytest_verify_argv
_LONG_FLAGS = ["--verbose", "--quiet", "--exitfirst", "--lf", "--ff", "--collect-only",
               "--no-header", "--disable-warnings", "--strict", "--strict-markers",
               "--continue-on-collection-errors", "--no-summary", "--keep-duplicates",
               "--no-cov", "--pdb", "--tb-native", "--pyargs"]
_LONG_VALUE = ["-c", "-k", "-m", "-o", "-p", "-r", "-W", "--maxfail", "--tb", "--rootdir",
               "--basetemp", "--deselect", "--ignore", "--ignore-glob", "--confcutdir",
               "--override-ini", "--junitxml", "--junit-prefix", "--color", "--log-level",
               "--timeout", "--durations", "--durations-min", "--cov", "--cov-report"]


@pytest.mark.parametrize("flag", _LONG_FLAGS)
def test_argv_every_modeled_long_flag_rides_through(flag):
    assert _VA(["pytest", flag, "tests/"]) == ["pytest", flag]
    assert _VA(["pytest", f"{flag}=x", "tests/"]) == ["pytest", f"{flag}=x"]


@pytest.mark.parametrize("opt", _LONG_VALUE)
def test_argv_every_modeled_value_option_keeps_its_value(opt):
    # the value is consumed even when it looks like a path
    assert _VA(["pytest", opt, "tests/v", "tests/"]) == ["pytest", opt, "tests/v"]
    if opt.startswith("--"):
        assert _VA(["pytest", f"{opt}=v"]) == ["pytest", f"{opt}=v"]
        assert _VA(["pytest", opt]) is None             # value missing: fail closed


@pytest.mark.parametrize("flag", list("qvxsl"))
def test_argv_every_short_flag_alone_and_bundled(flag):
    assert _VA(["pytest", f"-{flag}"]) == ["pytest", f"-{flag}"]
    assert _VA(["pytest", f"-q{flag}"]) == ["pytest", f"-q{flag}"]


@pytest.mark.parametrize("c", list("ckmoprW"))
def test_argv_every_short_value_option_attached(c):
    assert _VA(["pytest", f"-{c}expr", "tests/"]) == ["pytest", f"-{c}expr"]


@pytest.mark.parametrize("parts", [
    ["python", "-m", "unittest"],                 # no pytest at all
    ["pytest", "-n", "auto"],                     # xdist: unmodeled short
    ["pytest", "-qn"],                            # bundle with unmodeled letter
    ["pytest", "-zz"],                            # unknown body
    ["pytest", "--unknown=1"],                    # unknown attached long
    ["pytest", "--unknown"],                      # unknown long
    ["pytest", "-q", "-k"],                       # trailing short value missing
])
def test_argv_unmodeled_grammar_fails_closed(parts):
    assert _VA(parts) is None


def test_argv_prefix_kept_and_double_dash_ends_options():
    assert _VA(["env", "X=1", "python", "-m", "pytest", "-q", "--", "-n", "tests/"]) == \
        ["env", "X=1", "python", "-m", "pytest", "-q"]
    assert _VA(["pytest"]) == ["pytest"]
    assert _VA(["pytest", "a.py", "b.py", "-x"]) == ["pytest", "-x"]


# ---------------------------------------------------------------- file fetch
import base64 as _b64  # noqa: E402
import urllib.error  # noqa: E402


def _fetch(monkeypatch, reply, path="src/a b.py", ref="feat/x#1"):
    seen = []

    def api(method, p, data=None):
        seen.append((method, p))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(executor, "_gh_api", api)
    return executor._get_file_content("o/r", path, ref), seen


def test_fetch_quotes_path_and_ref_into_one_get(monkeypatch):
    body = {"encoding": "base64", "content": _b64.b64encode(b"x = 1\n").decode()}
    out, seen = _fetch(monkeypatch, body)
    assert out == "x = 1\n"
    assert seen == [("GET", "/repos/o/r/contents/src%2Fa%20b.py?ref=feat%2Fx%231")]


def test_fetch_accepts_line_wrapped_base64_and_utf8(monkeypatch):
    enc = _b64.b64encode("café = 'ü'\n".encode("utf-8") * 20).decode()
    wrapped = "\n".join(enc[i:i + 60] for i in range(0, len(enc), 60)) + "\n"
    out, _ = _fetch(monkeypatch, {"encoding": "base64", "content": wrapped})
    assert out == "café = 'ü'\n" * 20


@pytest.mark.parametrize("reply", [
    ["a", "dir", "listing"],                                        # directory match
    {"encoding": "base64"},                                         # no content
    {"encoding": "utf-8", "content": "eA=="},                       # wrong encoding
    {"encoding": "base64", "content": _b64.b64encode(b"\xff\xfe").decode()},  # not utf-8
    {"encoding": "base64", "content": 123},                         # wrong shape
    urllib.error.URLError("down"),
    ValueError("bad json"),
])
def test_fetch_never_returns_a_fabricated_premise(monkeypatch, reply):
    out, _ = _fetch(monkeypatch, reply)
    assert out is None


def test_merge_gate_honours_a_custom_bot_identity(monkeypatch, merge_config):
    forge = FakeForge([[_pr(author="acme-reviewer[bot]")]])
    assert _scan(monkeypatch, forge, merge_config, bot="acme-reviewer[bot]") == \
        [{"pr": 5, "status": "merged"}]


def test_pr_number_one_is_a_real_pr_and_zero_is_not(monkeypatch, merge_config):
    forge = FakeForge([[_pr(number=0), _pr(number=1)]])
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 1, "status": "merged"}]
    assert [p.endswith("/pulls/1/merge") for p, _ in forge.puts] == [True]


def test_pr_listing_keeps_page_one_rows(monkeypatch, merge_config):
    page1 = [_pr(number=7)] + [_pr(number=100 + i, ref="feature/x") for i in range(99)]
    forge = FakeForge([page1, [_pr(number=8, ref="feature/y")]])
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 7, "status": "merged"}]


def test_pr_listing_reads_exactly_twenty_pages(monkeypatch, merge_config):
    def full(n, tail):
        return [_pr(number=n, ref=tail)] + [_pr(number=10_000 * n + i, ref="feature/x")
                                             for i in range(99)]
    pages = [full(p, "feature/x") for p in range(1, 20)]
    pages.append(full(20, "fl4write/fix-20"))     # last page inside the cap: merged
    pages.append(full(21, "fl4write/fix-21"))     # past the cap: invisible
    forge = FakeForge(pages)
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 20, "status": "merged"}]


def test_a_failing_check_on_page_one_blocks_the_merge(monkeypatch, merge_config):
    page1 = [{"status": "completed", "conclusion": "failure"}] + GREEN * 99
    forge = FakeForge([[_pr()]], runs={"h" * 40: [page1, GREEN]})
    assert _scan(monkeypatch, forge, merge_config) == []


def test_check_runs_cap_is_exactly_twenty_pages(monkeypatch, merge_config):
    inside = FakeForge([[_pr()]], runs={"h" * 40: [GREEN * 100] * 19 + [GREEN]})
    assert _scan(monkeypatch, inside, merge_config) == [{"pr": 5, "status": "merged"}]
    at_cap = FakeForge([[_pr()]], runs={"h" * 40: [GREEN * 100] * 20})
    assert _scan(monkeypatch, at_cap, merge_config) == []


def test_an_unproven_merge_does_not_stop_later_prs(monkeypatch, merge_config):
    forge = FakeForge([[_pr(number=5, sha="a" * 40), _pr(number=6, sha="b" * 40)]])
    forge.merge_reply = [{"merged": False}, {"merged": True, "sha": "m" * 40}]
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 6, "status": "merged"}]
    assert len(forge.puts) == 2


def test_a_blocked_pr_does_not_stop_later_prs(monkeypatch, merge_config):
    forge = FakeForge([[_pr(number=5, author="mallory"), _pr(number=6)]])
    assert _scan(monkeypatch, forge, merge_config) == [{"pr": 6, "status": "merged"}]

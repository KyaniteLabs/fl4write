"""Mutation-testing pins for fl4write/executor.py security helpers.

A mutmut run over executor.py showed the token and containment rails had
no direct tests: dropping the token helper, breaking its permissions or
removing git hardening all went unnoticed. These rails stand between
untrusted test code and the bot's credentials.
"""

from __future__ import annotations

import json
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

    def __init__(self, pages, runs=None, status="success", merge_reply=None, files=None):
        self.pages, self.runs, self.status = pages, runs or {}, status
        self.merge_reply = merge_reply if merge_reply is not None else {"merged": True, "sha": "m" * 40}
        # E2 changed-file evidence: docs-only by default, so every pre-existing
        # merge test keeps exercising the allowed path of the classifier.
        self.files = files if files is not None else [
            {"filename": "docs/notes.md", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"}]
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
        if "/files?" in path:
            return self.files
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
def merge_config(tmp_path, monkeypatch):
    # E2: the merge path reads a PLATFORM-OWNED policy (fail closed if absent).
    policy = tmp_path / "protected-paths.json"
    policy.write_text(json.dumps({"protected_paths": [
        "fl4write/appauth.py", "fl4write/executor.py", "fl4write/forges.py",
        "fl4write/gatekeeper.py", "fl4write/mergepolicy.py", "fl4write/scrub.py",
        "fl4write/telemetry.py", "fl4write/writeguard.py", ".github/workflows/**",
    ]}), encoding="utf-8")
    monkeypatch.setenv("FL4WRITE_PROTECTED_PATHS", str(policy))
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


# ---------------------------------------------------------------- verify_diff_tests
from test_fl4write import make_pr  # noqa: E402

_JUNIT_FAIL = '<testsuite tests="3" failures="1" errors="0"/>'


class VerifyRig:
    """Fakes git (_run) and the evidence runner; records what each one got."""

    def __init__(self, monkeypatch, green=False, rc=1, junit=_JUNIT_FAIL,
                 stdout="E   assert 1 == 2\n1 failed, 2 passed"):
        self.git: list = []
        self.evidence: list = []
        self.askpass_token = None
        self.green, self.rc, self.junit, self.stdout = green, rc, junit, stdout
        monkeypatch.setattr(executor, "_test_isolation", lambda: ("host", ""))
        monkeypatch.setattr(executor, "_run", self._run)
        monkeypatch.setattr(executor, "_pytest_evidence", self._evidence)

    def _run(self, cmd, cwd=None, timeout=120, env=None):
        self.git.append({"cmd": cmd, "cwd": cwd, "timeout": timeout, "env": env})
        if env and env.get("GIT_ASKPASS"):
            self.askpass_token = subprocess.run(
                [env["GIT_ASKPASS"]], capture_output=True, text=True).stdout
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _evidence(self, argv, cwd, timeout, env, junit):
        self.evidence.append({"argv": list(argv), "cwd": cwd, "timeout": timeout, "env": env})
        if self.junit is not None:
            junit.write_text(self.junit)
        return self.green, subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


def _verify(cfg=None, files=("tests/test_x.py",)):
    return executor.verify_diff_tests(make_pr(head_sha="c" * 40), cfg or make_config(),
                                      list(files))


def test_verify_fetches_with_the_bot_token_and_runs_in_the_checkout(monkeypatch):
    monkeypatch.setenv("CODESITTER_GITHUB_TOKEN", "tok-123")
    rig = VerifyRig(monkeypatch)
    finding = _verify(make_config(test_timeout=77))
    fetch = next(g for g in rig.git if g["cmd"][:2] == ["git", "fetch"])
    assert fetch["cmd"][-2:] == ["https://github.com/KyaniteLabs/kinocut.git", "c" * 40]
    assert fetch["timeout"] == 180
    assert rig.askpass_token == "tok-123\n"
    workdir = rig.git[0]["cwd"]
    assert workdir is not None and all(g["cwd"] == workdir for g in rig.git)
    (ev,) = rig.evidence
    assert ev["cwd"] == workdir and ev["timeout"] == 77
    # tests run with a throwaway HOME and the sandbox allowlist — never the token
    assert ev["env"]["HOME"] != os.path.expanduser("~")
    assert "tok-123" not in "".join(ev["env"].values())
    assert not Path(workdir).exists() and not Path(ev["env"]["HOME"]).exists()
    assert finding is not None


def test_verify_finding_is_critical_on_the_changed_file_with_its_evidence(monkeypatch):
    VerifyRig(monkeypatch)
    f = _verify()
    assert (f.rule_id, f.severity, f.path, f.line, f.category) == \
        ("tests", "Critical", "tests/test_x.py", 1, "CI")
    assert "head=cccccccccc" in f.message and "1 failed, 2 passed" in f.message


def test_verify_default_argv_targets_only_the_diffs_python_tests(monkeypatch):
    rig = VerifyRig(monkeypatch)
    _verify(make_config(known_env_failures=["tests/t.py::flaky"]),
            files=("tests/test_a.py", "tests/data.json", "tests/test_b.py"))
    assert rig.evidence[0]["argv"] == [
        "python3", "-m", "pytest", "-q", "--tb=line", "--deselect", "tests/t.py::flaky",
        "--", "tests/test_a.py", "tests/test_b.py"]


def test_verify_explicit_pytest_cmd_keeps_options_before_the_separator(monkeypatch):
    rig = VerifyRig(monkeypatch)
    _verify(make_config(test_cmd="pytest -x tests/", known_env_failures=["a::b"]))
    assert rig.evidence[0]["argv"] == ["pytest", "-x", "--deselect", "a::b",
                                       "--", "tests/test_x.py"]


def test_verify_no_changed_tests_runs_the_default_tests_dir(monkeypatch):
    rig = VerifyRig(monkeypatch)
    _verify(files=())
    assert rig.evidence[0]["argv"] == ["python3", "-m", "pytest", "tests/", "-q", "--tb=line"]


@pytest.mark.parametrize("junit,rc,expect_finding", [
    ('<testsuite tests="3" failures="1" errors="2"/>', 1, True),   # real failures + errors
    ('<testsuites><testsuite failures="0" errors="0"/>'
     '<testsuite failures="1" errors="0"/></testsuites>', 1, True),  # failure in suite two
    ('<testsuite tests="3" failures="0" errors="2"/>', 1, False),  # errors-only = infra
    ("not xml", 1, False),                                          # unreadable = infra
    (None, 2, False),                                               # no junit, rc!=0 = infra
    (None, 0, True),                                                # no junit, rc 0 = killed suite
])
def test_verify_junit_decides_finding_vs_unverified(monkeypatch, junit, rc, expect_finding):
    VerifyRig(monkeypatch, junit=junit, rc=rc)
    assert (_verify() is not None) is expect_finding


def test_verify_green_run_is_no_finding(monkeypatch):
    VerifyRig(monkeypatch, green=True, rc=0, junit='<testsuite tests="1"/>')
    assert _verify() is None


@pytest.mark.parametrize("cmd", ["make test", "pytest x && echo", "npm test"])
def test_verify_non_pytest_or_chained_cmds_never_run(monkeypatch, cmd):
    rig = VerifyRig(monkeypatch)
    assert _verify(make_config(test_cmd=cmd)) is None
    assert rig.evidence == []


# ---------------------------------------------------------------- attempt_fix
import json as _json  # noqa: E402

from fl4write.models import Finding  # noqa: E402

HEAD = "d" * 40


def _finding(**over):
    base = dict(rule_id="loc-ceiling", severity="Major", path="pkg/mod.py", line=3,
                category="General", message="off by one", proposal="return 1")
    base.update(over)
    return Finding(**base)


class FixRig:
    """Fake git + forge + model for attempt_fix; every call is recorded."""

    def __init__(self, monkeypatch, *, content="x = 2\n", fixed="x = 1\n", tests=True,
                 rc=None, stdout=None, model=None, stderr=None):
        self.calls: list = []
        self.fetched: list = []
        self.tested: list = []
        self.branch_repo: list = []
        self.stderr = stderr or {}
        self.prompts: list = []
        self.api: list = []
        self.tokens: list = []
        self.rc = rc or {}
        self.stdout = {"rev-parse HEAD": HEAD, "write-tree": "T1", "rev-parse HEAD^{tree}": "T1"}
        self.stdout.update(stdout or {})
        monkeypatch.setenv("CODESITTER_GITHUB_TOKEN", "tok-fix")
        monkeypatch.setattr(executor, "_test_isolation", lambda: ("host", ""))
        monkeypatch.setattr(executor, "_get_file_content",
                            lambda r, p, ref: self.fetched.append((r, p, ref)) or content)
        monkeypatch.setattr(executor, "_run_tests",
                            lambda cwd, cfg: self.tested.append((cwd, cfg)) or tests)
        monkeypatch.setattr(executor, "_run", self._run)
        monkeypatch.setattr(executor, "_default_branch",
                            lambda repo: self.branch_repo.append(repo) or "trunk")
        monkeypatch.setattr(executor, "_gh_api", self._api)
        reply = model if model is not None else _json.dumps({"fixed_content": fixed})

        def call_model(route, prompt, system=None, **kw):
            self.prompts.append((route.model, prompt, system))
            if isinstance(reply, Exception):
                raise reply
            return reply
        monkeypatch.setattr(executor, "_call_model", call_model)

    def _key(self, cmd):
        return " ".join(cmd[1:3]) if cmd[0] == "git" else cmd[0]

    def _run(self, cmd, cwd=None, timeout=120, env=None):
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout, "env": env})
        if env and env.get("GIT_ASKPASS") and Path(env["GIT_ASKPASS"]).exists():
            self.tokens.append(subprocess.run([env["GIT_ASKPASS"]], capture_output=True,
                                              text=True).stdout)
        key = self._key(cmd)
        rc = self.rc.get(key, 0)
        if callable(rc):
            rc = rc()
        out = self.stdout.get(key, "")
        if cmd[:2] == ["git", "rev-parse"]:
            out = self.stdout.get(" ".join(cmd[1:3]), "")
        err = self.stderr.get(key, "err-tail" if rc else "")
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def _api(self, method, path, data=None):
        self.api.append((method, path, data))
        return {"number": 41, "html_url": "https://x/pr/41"}

    def cmds(self, name):
        return [c for c in self.calls if c["cmd"][:2] == ["git", name]]


def _fix(cfg=None, finding=None, pr=None):
    cfg = cfg or make_config(fix={"enabled": True})
    pr = pr or make_pr(head_sha=HEAD, number=12)
    return executor.attempt_fix(pr, finding or _finding(), cfg)


def test_fix_happy_path_opens_one_pr_from_a_hashed_bot_branch(monkeypatch):
    import hashlib
    rig = FixRig(monkeypatch)
    out = _fix()
    tag = hashlib.blake2b(f"{HEAD}:loc-ceiling:pkg/mod.py:3".encode(), digest_size=6).hexdigest()
    branch = f"fl4write/fix-12-{tag}"
    assert out == {"status": "pr_opened", "reason": "", "pr_number": 41,
                   "pr_url": "https://x/pr/41", "branch": branch}
    (push,) = rig.cmds("push")
    assert push["cmd"] == ["git", "push", "-q", "https://github.com/KyaniteLabs/kinocut.git",
                           f"HEAD:refs/heads/{branch}"]
    assert push["timeout"] == 120
    assert rig.tokens == ["tok-fix\n", "tok-fix\n"]           # fetch + push only
    (fetch,) = rig.cmds("fetch")
    assert fetch["cmd"][-2:] == ["https://github.com/KyaniteLabs/kinocut.git", HEAD]
    assert fetch["timeout"] == 180
    assert fetch["env"]["GIT_CONFIG_GLOBAL"] == os.devnull and \
        push["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    (add,) = rig.cmds("add")
    assert add["cmd"] == ["git", "add", "--", "pkg/mod.py"]
    (commit,) = rig.cmds("commit")
    env = commit["env"]
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "fl4write[bot]"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "fl4write@kyanitelabs.tech"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and "tok-fix" not in "".join(env.values())
    assert commit["cmd"][4].startswith("fix(loc-ceiling): off by one\n\nCo-authored-by: fl4write")
    workdir = rig.calls[0]["cwd"]
    assert all(c["cwd"] == workdir for c in rig.calls) and not Path(workdir).exists()
    ((method, path, body),) = rig.api
    assert (method, path) == ("POST", "/repos/KyaniteLabs/kinocut/pulls")
    assert body["head"] == branch and body["base"] == "trunk"
    assert body["title"] == "fix(loc-ceiling): off by one"
    assert "[Major] pkg/mod.py:3" in body["body"] and "return 1" in body["body"]


def test_fix_prompt_fences_the_exact_file_bytes(monkeypatch):
    rig = FixRig(monkeypatch, content="s = '```'\n")
    _fix()
    (_, prompt, system) = rig.prompts[0]
    assert "FINDING: [Major] pkg/mod.py:3 — off by one" in prompt
    assert "PROPOSAL: return 1" in prompt
    assert "````\ns = '```'\n\n````" in prompt                 # fence widened past the content
    assert system.startswith(executor.PATCH_SYSTEM)


def test_fix_falls_back_to_the_second_model_route(monkeypatch):
    calls = []
    rig = FixRig(monkeypatch)

    def flaky(route, prompt, system=None, **kw):
        calls.append(route.model)
        if route.model == "test-model":
            raise RuntimeError("402")
        return _json.dumps({"fixed_content": "x = 1\n"})
    monkeypatch.setattr(executor, "_call_model", flaky)
    cfg = make_config(fix={"enabled": True},
                      fallback_model={"endpoint": "http://m2/v1", "model": "fb", "key_env": "K2"})
    assert _fix(cfg)["status"] == "pr_opened"
    assert calls == ["test-model", "fb"]
    assert rig.prompts == []


def test_fix_same_route_fallback_is_not_called_twice(monkeypatch):
    rig = FixRig(monkeypatch, model=RuntimeError("down"))
    cfg = make_config(fix={"enabled": True}, fallback_model={
        "endpoint": "http://model/v1/chat/completions", "model": "test-model", "key_env": "MK"})
    out = _fix(cfg)
    assert out["status"] == "error" and out["reason"].startswith("model unavailable: down")
    assert len(rig.prompts) == 1


@pytest.mark.parametrize("over,status,reason", [
    (dict(model="no json at all"), "error", "model output unparseable"),
    (dict(model=_json.dumps({"fixed_content": None})), "nofix", "model returned no fix"),
    (dict(model=_json.dumps({"fixed_content": 7})), "nofix", "model returned no fix"),
    (dict(fixed="x = 2\n"), "nofix", "model patch identical to original (no-op)"),
    (dict(content=None), "error", "cannot fetch pkg/mod.py@dddddddd"),
    (dict(tests=False), "testfail", "tests failed with fix applied"),
    (dict(rc={"init -q": 1}), "error", "git init failed"),
    (dict(rc={"fetch -q": 1}), "error", "fetch of PR head failed: err-tail"),
    (dict(rc={"checkout -q": 1}), "error", "checkout of PR head failed"),
    (dict(stdout={"rev-parse HEAD": "e" * 40}), "error", "checked-out SHA does not match"),
    (dict(rc={"cp": 1}), "error", "test-tree copy failed"),
    (dict(rc={"add --": 1}), "error", "git add failed: err-tail"),
    (dict(rc={"write-tree": 1}), "error", "cannot capture pre-commit tree"),
    (dict(stdout={"write-tree": ""}), "error", "cannot capture pre-commit tree"),
    (dict(rc={"commit -q": 1}), "error", "git commit failed: err-tail"),
    (dict(stdout={"rev-parse HEAD^{tree}": "T2"}), "error", "committed tree differs"),
])
def test_fix_every_failure_is_a_terminal_outcome_with_no_pr(monkeypatch, over, status, reason):
    rig = FixRig(monkeypatch, **over)
    out = _fix()
    assert out["status"] == status and out["reason"].startswith(reason)
    assert rig.api == [] and rig.cmds("push") == []


def test_fix_whitespace_only_change_is_a_real_fix(monkeypatch):
    FixRig(monkeypatch, content="def f():\n  return 1\n", fixed="def f():\n    return 1\n")
    assert _fix()["status"] == "pr_opened"


def test_fix_rejected_push_retries_once_with_lease_and_a_fresh_token(monkeypatch):
    pushes = iter([1, 0])
    rig = FixRig(monkeypatch, rc={"push -q": lambda: next(pushes)})
    assert _fix()["status"] == "pr_opened"
    first, second = rig.cmds("push")
    assert "--force-with-lease" not in first["cmd"]
    assert second["cmd"][3] == "--force-with-lease" and second["timeout"] == 120
    assert rig.tokens == ["tok-fix\n"] * 3


def test_fix_push_failing_twice_is_an_error(monkeypatch):
    rig = FixRig(monkeypatch, rc={"push -q": 1})
    out = _fix()
    assert out == {"status": "error", "reason": "push failed: err-tail"}
    assert len(rig.cmds("push")) == 2 and rig.api == []


@pytest.mark.parametrize("pr_over,cfg,status", [
    (dict(is_fork=True), {"enabled": True}, "blocked"),
    ({}, {"enabled": False}, "blocked"),
    (dict(is_bot_author=True, title="bump lockfile"), {"enabled": True}, "blocked"),
])
def test_fix_rails_block_before_any_model_or_git(monkeypatch, pr_over, cfg, status):
    rig = FixRig(monkeypatch)
    out = _fix(make_config(fix=cfg), pr=make_pr(head_sha=HEAD, **pr_over))
    assert out["status"] == status and "escalation" in out
    assert rig.prompts == [] and rig.calls == []


def test_fix_isolation_unavailable_refuses_before_the_model(monkeypatch):
    rig = FixRig(monkeypatch)
    monkeypatch.setattr(executor, "_test_isolation", lambda: ("unavailable", "no docker"))
    assert _fix() == {"status": "error", "reason": "test isolation unavailable: no docker"}
    assert rig.prompts == []


# ---------------------------------------------------------------- _pytest_evidence
from fl4write import exhaustive_sandbox as _sbx  # noqa: E402


class EvidenceRun:
    """Host-mode fake: records argv and writes the junit the 'suite' produced."""

    def __init__(self, monkeypatch, junit, rc=0):
        self.argv = None
        self.kw = None
        self.junit, self.rc = junit, rc
        monkeypatch.setattr(executor, "_test_isolation", lambda: ("host", ""))
        monkeypatch.setattr(executor, "_run", self)

    def __call__(self, cmd, cwd=None, timeout=120, env=None):
        self.argv, self.kw = cmd, {"cwd": cwd, "timeout": timeout, "env": env}
        if self.junit is not None:
            Path(cmd[cmd.index("--junitxml") + 1]).write_text(self.junit)
        return subprocess.CompletedProcess(cmd, self.rc, "out", "err")


def _evidence(tmp_path, argv=("pytest", "-q")):
    return executor._pytest_evidence(list(argv), tmp_path, 33, {"HOME": "/h"},
                                     tmp_path / "ev" / "results.xml")


def test_evidence_strips_configured_junit_options_and_owns_the_path(monkeypatch, tmp_path):
    (tmp_path / "ev").mkdir()
    run = EvidenceRun(monkeypatch, '<testsuite tests="1"/>')
    green, _ = _evidence(tmp_path, ["pytest", "--junitxml", "mine.xml", "-q",
                                    "--junit-prefix", "p", "--junitxml=y.xml",
                                    "--junit-prefix=q", "-x", "--", "t.py"])
    assert green is True
    assert run.argv == ["pytest", "-q", "-x", "--junitxml",
                        str(tmp_path / "ev" / "results.xml"), "--", "t.py"]
    assert run.kw == {"cwd": tmp_path, "timeout": 33, "env": {"HOME": "/h"}}


@pytest.mark.parametrize("junit,green", [
    ('<testsuite tests="2" failures="0" errors="0"/>', True),
    ('<testsuites><testsuite tests="2"/><testsuite tests="1"/></testsuites>', True),
    ('<testsuite tests="0"/>', False),                                   # nothing ran
    ('<testsuite tests="2" failures="1"/>', False),                      # rc 0 but failures
    ('<testsuite tests="2" errors="1"/>', False),                        # rc 0 but errors
    ('<testsuites><testsuite tests="2"/><testsuite tests="1" failures="1"/></testsuites>',
     False),                                                             # hidden in suite two
    ('<testsuites><testsuite tests="2"/><testsuite tests="1" errors="1"/></testsuites>',
     False),
    ('<testsuites/>', False),                                            # no testsuite at all
    ('<testsuite tests="" failures="" errors=""/>', False),              # blank counts
    ("not xml", False),
    (None, False),                                                       # exit 0, no junit
])
def test_evidence_exit_zero_is_green_only_with_complete_junit(monkeypatch, tmp_path,
                                                              junit, green):
    (tmp_path / "ev").mkdir()
    EvidenceRun(monkeypatch, junit, rc=0)
    assert _evidence(tmp_path)[0] is green


def test_evidence_nonzero_exit_is_never_green(monkeypatch, tmp_path):
    (tmp_path / "ev").mkdir()
    EvidenceRun(monkeypatch, '<testsuite tests="2"/>', rc=1)
    green, result = _evidence(tmp_path)
    assert green is False and result.returncode == 1


def test_evidence_refuses_when_isolation_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "_test_isolation", lambda: ("unavailable", "no runtime"))
    green, result = _evidence(tmp_path)
    assert green is False and result.returncode == 125 and result.stderr == "no runtime"


def test_evidence_container_mode_hands_the_supervisor_a_placeholder(monkeypatch, tmp_path):
    seen = {}

    def run_isolated(argv, cwd, junit, timeout, image):
        seen.update(argv=argv, cwd=cwd, junit=junit, timeout=timeout, image=image)
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text('<testsuite tests="1"/>')
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(executor, "_test_isolation", lambda: ("container", "sha256:img"))
    monkeypatch.setattr(_sbx, "run_isolated", run_isolated)
    green, _ = _evidence(tmp_path, ["pytest", "--junitxml=evil", "-q", "--", "t.py"])
    assert green is True
    assert seen["argv"] == ["pytest", "-q", "--junitxml", "{junit}", "--", "t.py"]
    assert (seen["cwd"], seen["timeout"], seen["image"]) == (tmp_path, 33, "sha256:img")
    assert seen["junit"] == tmp_path / "ev" / "results.xml"


def test_evidence_container_unavailable_drops_any_stale_junit(monkeypatch, tmp_path):
    junit = tmp_path / "ev" / "results.xml"
    junit.parent.mkdir()
    junit.write_text('<testsuite tests="9"/>')                      # stale, must not count

    def boom(*a, **k):
        raise _sbx.SandboxUnavailable("docker gone")
    monkeypatch.setattr(executor, "_test_isolation", lambda: ("container", "sha256:img"))
    monkeypatch.setattr(_sbx, "run_isolated", boom)
    green, result = _evidence(tmp_path)
    assert green is False and not junit.exists()
    assert (result.returncode, result.stdout, result.stderr) == (125, "", "docker gone")
    assert result.args == ["pytest", "-q", "--junitxml", "{junit}"]


def test_evidence_cleanup_pending_keeps_the_runs_own_verdict(monkeypatch, tmp_path):
    def pending(argv, cwd, junit, timeout, image):
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text('<testsuite tests="1"/>')
        raise _sbx.SandboxCleanupPending("cleanup pending",
                                         subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(executor, "_test_isolation", lambda: ("container", "sha256:img"))
    monkeypatch.setattr(_sbx, "run_isolated", pending)
    assert _evidence(tmp_path)[0] is True


# ---------------------------------------------------------------- _run_tests (fix gate)
class GateRig:
    def __init__(self, monkeypatch, green=True):
        self.calls = []
        self.green = green
        monkeypatch.setattr(executor, "_pytest_evidence", self)

    def __call__(self, argv, cwd, timeout, env, junit):
        self.calls.append(dict(argv=argv, cwd=cwd, timeout=timeout, env=env, junit=junit))
        assert Path(env["HOME"]).is_dir() and junit.name == "results.xml"
        return self.green, None


def test_fix_gate_default_cmd_comes_from_the_env_with_deselects(monkeypatch, tmp_path):
    monkeypatch.delenv("CODESITTER_TEST_CMD", raising=False)
    rig = GateRig(monkeypatch)
    cfg = make_config(known_env_failures=["a::b", "c::d"], test_timeout=61)
    assert executor._run_tests(tmp_path, cfg) is True
    (c,) = rig.calls
    assert c["argv"] == ["python3", "-m", "pytest", "tests/", "-x", "-q", "--tb=line",
                         "--deselect", "a::b", "--deselect", "c::d"]
    assert c["cwd"] == tmp_path and c["timeout"] == 61
    assert c["env"]["HOME"] != os.path.expanduser("~")
    assert not Path(c["env"]["HOME"]).exists() and not c["junit"].parent.exists()

    monkeypatch.setenv("CODESITTER_TEST_CMD", "pytest -q 'my tests/'")
    assert executor._run_tests(tmp_path, make_config()) is True
    assert rig.calls[-1]["argv"] == ["pytest", "-q", "my tests/"]


def test_fix_gate_red_evidence_is_red(monkeypatch, tmp_path):
    GateRig(monkeypatch, green=False)
    assert executor._run_tests(tmp_path, make_config(test_cmd="pytest")) is False


@pytest.mark.parametrize("cmd", ["pytest && x", "pytest; x", "pytest | tee", "make test",
                                 "pytest 'unclosed"])
def test_fix_gate_fails_closed_without_running(monkeypatch, tmp_path, cmd):
    rig = GateRig(monkeypatch)
    assert executor._run_tests(tmp_path, make_config(test_cmd=cmd)) is False
    assert rig.calls == []


# ---------------------------------------------------------------- verify finding text
def test_verify_finding_names_the_default_cmd_and_last_three_lines(monkeypatch):
    VerifyRig(monkeypatch, stdout="l1\nl2\nl3\nl4\n" + "z" * 300)
    f = _verify(files=())
    assert f.path == "tests"
    assert "cmd='python3 -m pytest'" in f.message and "files=[]" in f.message
    tail = f.message.split("head=cccccccccc): ", 1)[1]
    assert tail == ("l3 | l4 | " + "z" * 300)[:240]


@pytest.mark.parametrize("junit", ['<testsuite tests="0" failures="0" errors="0"/>',
                                   '<testsuite tests="0"/>'])
def test_verify_empty_completed_suite_is_a_did_not_run_finding(monkeypatch, junit):
    VerifyRig(monkeypatch, junit=junit, rc=5)
    assert _verify() is not None


def test_fix_hands_the_right_inputs_to_every_helper(monkeypatch):
    cfg = make_config(fix={"enabled": True})
    rig = FixRig(monkeypatch)
    assert _fix(cfg)["status"] == "pr_opened"
    assert rig.fetched == [("KyaniteLabs/kinocut", "pkg/mod.py", HEAD)]
    (checkout,) = rig.cmds("checkout")
    assert checkout["cmd"] == ["git", "checkout", "-q", "--detach", "FETCH_HEAD"]
    ((testdir, tcfg),) = rig.tested
    workdir = rig.calls[0]["cwd"]
    assert tcfg is cfg and testdir != workdir and str(testdir).startswith(
        os.path.join(os.path.dirname(str(workdir)), "fl4write-fix-test-"))
    assert not Path(testdir).exists()
    (cp,) = [c for c in rig.calls if c["cmd"][0] == "cp"]
    assert cp["cmd"] == ["cp", "-a", f"{workdir}/.", f"{testdir}/"]
    assert rig.branch_repo == ["KyaniteLabs/kinocut"]


def test_fix_plain_content_uses_a_single_backtick_fence(monkeypatch):
    rig = FixRig(monkeypatch, content="x = 2\n")
    _fix()
    assert rig.prompts[0][1].endswith("FILE CONTENT (pkg/mod.py):\n`\nx = 2\n\n`")


def test_fix_empty_commit_is_a_nofix_not_an_error(monkeypatch):
    FixRig(monkeypatch, rc={"commit -q": 1}, stderr={"commit -q": "nothing to commit, clean"})
    assert _fix() == {"status": "nofix", "reason": "nothing to commit (patch was a no-op)"}


def test_fix_retry_push_runs_in_the_workdir(monkeypatch):
    pushes = iter([1, 0])
    rig = FixRig(monkeypatch, rc={"push -q": lambda: next(pushes)})
    _fix()
    workdir = rig.calls[0]["cwd"]
    assert [p["cwd"] for p in rig.cmds("push")] == [workdir, workdir]


def test_fix_depth_starts_at_zero(monkeypatch):
    FixRig(monkeypatch)
    out = _fix(make_config(fix={"enabled": True, "max_fix_depth": 1}))
    assert out["status"] == "pr_opened"


def test_fix_block_carries_an_escalation_naming_the_reason(monkeypatch):
    FixRig(monkeypatch)
    out = _fix(make_config(fix={"enabled": False}))
    assert out["reason"] == "fix lane disabled for this repo"
    assert "Blocked: fix lane disabled for this repo" in out["escalation"]
    assert "pkg/mod.py:3" in out["escalation"]


# ---------------------------------------------------------------- process + API plumbing
def test_run_is_text_captured_in_cwd_with_exact_env(tmp_path):
    out = executor._run(["sh", "-c", 'printf "%s|" "$FOO"; pwd'], cwd=tmp_path,
                        env={"FOO": "bar", "PATH": os.environ["PATH"]}, timeout=10)
    assert out.returncode == 0 and out.stdout == f"bar|{tmp_path}\n" and out.stderr == ""


def test_run_honours_its_timeout(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        executor._run(["sleep", "5"], timeout=0.2)


def test_gh_api_sends_an_authenticated_json_request(monkeypatch):
    import io
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(), data=req.data,
                    auth=req.get_header("Authorization"),
                    ctype=req.get_header("Content-type"), timeout=timeout)
        return Resp(b'{"ok": 1}')
    monkeypatch.setenv("CODESITTER_GITHUB_TOKEN", "t0k")
    monkeypatch.setattr(executor.urllib.request, "urlopen", urlopen)
    assert executor._gh_api("PUT", "/repos/o/r/x", {"a": 1}) == {"ok": 1}
    assert seen == {"url": "https://api.github.com/repos/o/r/x", "method": "PUT",
                    "data": b'{"a": 1}', "auth": "token t0k",
                    "ctype": "application/json", "timeout": 30}
    assert executor._gh_api("GET", "/x") == {"ok": 1}
    assert seen["data"] is None and seen["method"] == "GET"


def test_sandbox_env_reexposes_only_user_site_on_pythonpath(monkeypatch, tmp_path):
    import site
    usersite = tmp_path / "site"
    usersite.mkdir()
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(usersite))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert executor._sandbox_env_for("/h")["PYTHONPATH"] == str(usersite)
    monkeypatch.setenv("PYTHONPATH", "/a")
    assert executor._sandbox_env_for("/h")["PYTHONPATH"] == f"{usersite}{os.pathsep}/a"


def test_sandbox_home_is_created_once_and_removed_at_exit(monkeypatch):
    import atexit
    registered = []
    monkeypatch.setattr(executor, "_SANDBOX_HOME", None)
    monkeypatch.setattr(atexit, "register", lambda *a: registered.append(a))
    home = executor._sandbox_home()
    try:
        assert executor._sandbox_home() == home and Path(home).is_dir()
        assert Path(home).name.startswith("fl4write-sandbox-home-")
        assert registered == [(executor.shutil.rmtree, home, True)]
    finally:
        executor.shutil.rmtree(home, True)


def test_verify_git_argv_is_exact(monkeypatch):
    rig = VerifyRig(monkeypatch)
    _verify()
    assert [g["cmd"] for g in rig.git] == [
        ["git", "init", "-q"],
        ["git", "fetch", "-q", "--depth", "1", "https://github.com/KyaniteLabs/kinocut.git",
         "c" * 40],
        ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
    ]


def test_verify_isolation_unavailable_touches_nothing(monkeypatch):
    rig = VerifyRig(monkeypatch)
    monkeypatch.setattr(executor, "_test_isolation", lambda: ("unavailable", "x"))
    assert _verify() is None and rig.git == [] and rig.evidence == []

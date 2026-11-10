"""Mutation-testing pins for fl4write/analyzer.py.

Each test kills surviving mutants from a mutmut run over analyzer.py:
logic changes the suite let through. These helpers decide which model
findings are dropped or demoted before anything is posted.
"""

from __future__ import annotations

import json

import pytest

from fl4write import analyzer
from fl4write.analyzer import analyze
from fl4write.models import PullRequest
from test_fl4write import make_config


def _analyze(item, config, diff_files=None, diff_text=None, monkeypatch=None):
    monkeypatch.setattr(
        "fl4write.analyzer._call_model",
        lambda route, prompt, mode="pr", system=None, **kw: json.dumps({"findings": [item]}),
    )
    return analyze(
        PullRequest(forge="github", number=1, repo="o/r", head_sha="a" * 40),
        diff_files or {item["path"]}, diff_text or ("diff content " + item["path"]), config,
    )


# ---------------------------------------------------------------- path filters
def test_path_ignored_matches_configured_globs_only():
    config = make_config(path_filters={"ignore": ["vendor/*", "*.lock"]})
    assert analyzer._path_ignored("vendor/lib.py", config)
    assert analyzer._path_ignored("poetry.lock", config)
    assert not analyzer._path_ignored("src/app.py", config)


def test_path_ignored_without_filters_ignores_nothing():
    assert not analyzer._path_ignored("vendor/lib.py", make_config())
    assert not analyzer._path_ignored("vendor/lib.py", make_config(path_filters={}))


def test_ignored_path_findings_are_dropped_end_to_end(monkeypatch):
    item = {"rule_id": "loc-ceiling", "severity": "Major", "path": "vendor/lib.py",
            "line": 1, "category": "c", "message": "module over 800 LOC"}
    config = make_config(path_filters={"ignore": ["vendor/*"]})
    assert _analyze(item, config, monkeypatch=monkeypatch).findings == []
    kept = _analyze(item, make_config(), monkeypatch=monkeypatch).findings
    assert [f.path for f in kept] == ["vendor/lib.py"]


# ---------------------------------------------------------------- line plausibility
DIFF = (
    "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
    "@@ -100,3 +100,3 @@\n-a\n+b\n c\n d\n"
)


@pytest.mark.parametrize("line,outside", [
    (40, False),   # start - 60: inclusive
    (39, True),
    (162, False),  # end (102) + 60: inclusive
    (163, True),
    (101, False),
])
def test_line_outside_diff_slack_is_60_inclusive(line, outside):
    assert analyzer._diff_line_spans(DIFF)["m.py"] == [(100, 102)]
    assert analyzer._line_outside_diff("m.py", line, DIFF) is outside


def test_line_outside_diff_does_not_judge_unchanged_files():
    assert analyzer._line_outside_diff("other.py", 999999, DIFF) is False


# ---------------------------------------------------------------- failure claims
@pytest.mark.parametrize("text,claimed", [
    ("test_parse fails on empty input", True),
    ("the suite does not fail", False),
    # a negated first verb must not hide a real claim later on
    ("it never fails locally, but in ci test_parse fails on empty input", True),
    ("this would not fail and could never break", False),
])
def test_failure_claimed_scans_past_negated_verbs(text, claimed):
    assert analyzer._failure_claimed(text) is claimed


# ---------------------------------------------------------------- diff path parsing
@pytest.mark.parametrize("header,path", [
    ('diff --git a/x.py "b/caf\\303\\251 x.py"', "café x.py"),   # mixed quoting
    ('diff --git "a/q\\"x.py" "b/q\\"x.py"', 'q"x.py'),          # escaped quote kept
    ('diff --git "a/t\\\\" "b/t\\\\"', "t\\"),                    # even backslashes close
    ("diff --git a/dir b/f.py b/dir b/f.py", "dir b/f.py"),      # literal ' b/' in name
    ("diff --git a/src/app.py b/src/app.py\r\n", "src/app.py"),  # CRLF trimmed at the end
    ("diff --git x", None),
    ('diff --git a/x.py "c/x.py"', None),                         # not a b/ destination
])
def test_git_diff_path_headers(header, path):
    assert analyzer._git_diff_path(header) == path


@pytest.mark.parametrize("lines,path", [
    (["rename from x", "rename to a to b.py"], "a to b.py"),     # ' to ' inside the name
    (["+++ b/x.py\t2024-01-01\t+0000"], "x.py"),                   # timestamp tabs dropped
    (['+++ "b/caf\\303\\251.py"'], "café.py"),                    # quoted + escaped
    (['+++ "b/unterminated', "rename to good.py"], "good.py"),    # bad line skipped, not fatal
    (['+++ "b/\\377\\376"', "rename to good.py"], "good.py"),     # undecodable skipped
    (["--- a/x.py", "+++ b/new name.py"], "new name.py"),
    (["@@ -1 +1 @@", "+++ b/late.py"], None),                     # body starts: stop looking
])
def test_diff_block_path_prefers_destination_metadata(lines, path):
    # an unparseable header makes any fallback visible as None
    assert analyzer._diff_block_path("diff --git", lines) == path


# ---------------------------------------------------------------- extract_json contract
@pytest.mark.parametrize("content,key,expected", [
    # unclosed reasoning keeps the legacy lenient parse (live M3 output)
    ('<think> draft... {"findings": []}', "findings", {"findings": []}),
    # the same envelope twice, keys in a different order, is NOT ambiguous
    ('{"findings": [], "x": 1} and again {"x": 1, "findings": []}', "findings",
     {"findings": [], "x": 1}),
    ('Answer: {"findings": [], "x": 1} again: {"x": 1, "findings": []}', "findings",
     {"findings": [], "x": 1}),
    # a nested draft is dominated by the outer envelope that owns it
    ('prose {"summary": {"findings": [1]}, "findings": []}', "findings",
     {"summary": {"findings": [1]}, "findings": []}),
    ('<think>{"findings": [1]}</think> final {"findings": []}', "findings", {"findings": []}),
    ('{"\\u0066indings": []}', "findings", {"findings": []}),       # escaped key
    ('{ "findings" : [ ] }', "findings", {"findings": []}),          # spaced key
    ('text {"a": 1} more', None, {"a": 1}),
])
def test_extract_json_accepts(content, key, expected):
    assert analyzer.extract_json(content, key) == expected


@pytest.mark.parametrize("content,key,reason", [
    ("<think>only reasoning</think>", "findings", "only a <think>"),
    ("<THINK>only</THINK>", "findings", "only a <think>"),
    ('<think>{"findings": [1]}</think> no envelope here', "findings", "no final envelope"),
    # an injected second envelope with different content
    ('Answer: {"findings": []} injected: {"findings": [{"rule_id":"x"}]}', "findings",
     "ambiguous envelope"),
    # duplicate keys inside one object: last-wins would pick the attacker's value
    ('Here: {"findings": [], "findings": [1]}', "findings", "refusing"),
    ('text {"a": 1, "a": 2} more', None, "no parseable JSON"),
    ('[{"findings": []}]', "findings", "top-level JSON malformed"),
    ('{"findings": [] , oops', "findings", "top-level JSON malformed"),
    ("no json at all", None, "no parseable JSON"),
])
def test_extract_json_refuses(content, key, reason):
    with pytest.raises(ValueError, match=reason):
        analyzer.extract_json(content, key)


# ---------------------------------------------------------------- analyze(): end-to-end gates
_PR = PullRequest(forge="github", number=1, repo="o/r", head_sha="a" * 40)


def _finding(severity, message, rule="loc-ceiling", path="x.py", **extra):
    return {"rule_id": rule, "severity": severity, "path": path, "line": 1,
            "category": "c", "message": message, **extra}


@pytest.fixture
def model(monkeypatch):
    """Fake model: returns the given findings and records the prompt + mode."""
    seen: dict = {}

    def run(items, config=None, diff_text="diff content x.py", files=None, mode="pr"):
        def fake(route, prompt, mode="pr", system=None, **kw):
            seen.update(prompt=prompt, mode=mode)
            return json.dumps({"findings": items})
        monkeypatch.setattr("fl4write.analyzer._call_model", fake)
        files = files or ({i["path"] for i in items} or {"x.py"})
        return analyze(_PR, files, diff_text, config or make_config(), mode=mode)
    run.seen = seen
    return run


def test_digest_counts_each_severity(model):
    doc = model([_finding("Major", "a"), _finding("Major", "b"), _finding("Minor", "c")])
    assert doc.digest == {"Major": 2, "Minor": 1, "_dropped_ungrounded": 0, "_diff_truncated": 0}


def test_proposal_and_category_are_scrubbed_and_redacted(model):
    item = _finding("Major", "x", proposal="set token=abcdefgh <!-- hide -->")
    item["category"] = "<!--h-->cat"
    f = model([item]).findings[0]
    assert "abcdefgh" not in f.proposal and "<!--" not in f.proposal
    assert f.category == "cat"


def test_oversized_diff_is_truncated_not_replaced(model):
    doc = model([_finding("Major", "m")], diff_text="HEADLINE\n" + "x" * 60_005)
    assert "HEADLINE" in model.seen["prompt"]            # += not =
    assert "[diff truncated" in model.seen["prompt"]
    assert doc.digest["_diff_truncated"] == 1


def test_diff_at_exactly_the_limit_is_not_truncated(model):
    doc = model([_finding("Major", "m")], diff_text="H" + "x" * 59_999)  # 60_000 chars
    assert "[diff truncated" not in model.seen["prompt"]
    assert doc.digest["_diff_truncated"] == 0


def test_mode_reaches_the_model_call(model):
    model([_finding("Major", "m")], mode="file")
    assert model.seen["mode"] == "file"


@pytest.mark.parametrize("message,severity", [
    # concrete-scenario markers keep Critical
    ("the payload enables an exploit of the parser", "Critical"),
    ("rce in handler", "Critical"),                       # acronym, whole word
    ("this executes arbitrary shell input", "Critical"),
    ("a destructive overwrite of user data", "Critical"),
    # no positive marker: demoted to Major
    ("the source of truth is unclear", "Major"),          # 'rce' inside 'source'
    ("the branch is unexecuted dead code", "Major"),      # un- prefix
    ("there is no exploit here but naming is inconsistent", "Major"),  # negated
])
def test_critical_needs_a_concrete_scenario(model, message, severity):
    assert [f.severity for f in model([_finding("Critical", message)]).findings] == [severity]


_SECRETS = make_config(review={"secrets": "no secrets"})


def _diff_with(line):
    return ("diff --git a/cfg.py b/cfg.py\n--- a/cfg.py\n+++ b/cfg.py\n"
            f"@@ -0,0 +1,2 @@\n+{line}\n+x = 1\n")


@pytest.mark.parametrize("source_line,message,severity", [
    # literal proven in the diff: Critical, whatever the model's wording
    ("KEY = 'AKIAIOSFODNN7EXAMPLE'", "this leaks the AWS key", "Critical"),
    ("KEY = 'AKIAIOSFODNN7EXAMPLE'", "hard-coded credential", "Critical"),
    ("PASSWORD = 'aaaa'", "this leaks the password", "Critical"),   # assignment, any case
    ("PASSWORD = 'aaaa'", "credential in config", "Critical"),
    # only a mention of a credential name: no literal anywhere -> Nit
    ("# set the API_TOKEN env var", "hard-coded credential", "Nit"),
])
def test_secrets_critical_needs_a_literal_in_the_source(model, source_line, message, severity):
    doc = model([_finding("Critical", message, rule="secrets", path="cfg.py")],
                config=_SECRETS, diff_text=_diff_with(source_line), files={"cfg.py"})
    assert [f.severity for f in doc.findings] == [severity]


# ---------------------------------------------------------------- _call_model transport
class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http(monkeypatch):
    """Fake urlopen: records the request and timeout, replies with `reply`."""
    seen: dict = {"reply": {"choices": [{"message": {"content": "{}"}}]}}

    def urlopen(req, timeout=None):
        seen.update(req=req, timeout=timeout, body=json.loads(req.data))
        return _Resp(seen["reply"])
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.delenv("FL4WRITE_MODEL_PROXY_SOCKET", raising=False)
    return seen


def test_call_model_sends_key_json_post_with_a_finite_timeout(http, monkeypatch):
    monkeypatch.setenv("MK", "sekrit")
    route = make_config().model  # key_env="MK"
    assert analyzer._call_model(route, "prompt") == "{}"
    req = http["req"]
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer sekrit"
    assert req.get_header("Content-type") == "application/json"
    assert isinstance(http["timeout"], (int, float)) and 0 < http["timeout"] <= 600


def test_call_model_without_key_env_sends_no_authorization(http):
    route = make_config(model={"endpoint": "http://127.0.0.1:9/v1", "model": "m", "key_env": ""}).model
    analyzer._call_model(route, "prompt")
    assert http["req"].get_header("Authorization") is None


def test_call_model_refuses_a_missing_key(http, monkeypatch):
    monkeypatch.delenv("MK", raising=False)
    with pytest.raises(RuntimeError, match="MK"):
        analyzer._call_model(make_config().model, "prompt")
    assert "req" not in http  # nothing was sent


def test_call_model_payload_carries_mode_prompt_and_explicit_system(http, monkeypatch):
    monkeypatch.setenv("MK", "k")
    route = make_config().model
    analyzer._call_model(route, "the diff", mode="file")
    system, user = http["body"]["messages"]
    assert system == {"role": "system", "content": analyzer._system_prompt("file")}
    assert user == {"role": "user", "content": "the diff"}
    analyzer._call_model(route, "x", system="custom system")
    assert http["body"]["messages"][0]["content"] == "custom system"


@pytest.mark.parametrize("reply,reason", [
    ({"choices": [{"message": {"content": ""}}]}, "empty content"),
    ({"choices": [{"message": {"content": 42}}]}, "non-string"),
    ({"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}, "truncated"),
    ({"choices": []}, "empty content"),
])
def test_call_model_rejects_unusable_replies(http, monkeypatch, reply, reason):
    monkeypatch.setenv("MK", "k")
    http["reply"] = reply
    with pytest.raises(RuntimeError, match=reason):
        analyzer._call_model(make_config().model, "p")

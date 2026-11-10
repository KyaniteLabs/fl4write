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

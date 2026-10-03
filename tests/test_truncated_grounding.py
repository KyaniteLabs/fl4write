"""Truncated-diff grounding: gates must judge what the model actually saw.

Cycle-1 autoloop finding: the truncated-diff guard built its hunk map from
the FULL diff while the model only received the first MAX_DIFF_CHARS, so
findings citing truncated-away hunks were accepted despite the "evidence is
simply NOT HERE" comment. Both the hunk-presence gate and the line-span
gate now run against the truncated slice.
"""

from __future__ import annotations

import json

from fl4write import config as cfg
from fl4write.analyzer import MAX_DIFF_CHARS, analyze
from fl4write.models import PullRequest

RAW = {
    "repo": "o/r",
    "forges": {
        "github": {
            "role": "primary", "api_base": "https://api.github.com", "token_env": "GHT",
        }
    },
    "model": {"endpoint": "http://model/v1", "model": "test", "key_env": "MK"},
    "review": {"general": "catch defects"},
    "severity_vocab": ["Critical", "Major", "Minor", "Nit"],
}


def _config():
    return cfg.RepoConfig.model_validate(RAW)


def _pr():
    return PullRequest(forge="github", number=1, repo="o/r", head_sha="a" * 40)


def _big_diff():
    early_head = ("diff --git a/early.py b/early.py\n"
                  "+++ b/early.py\n"
                  "@@ -0,0 +1,7000 @@\n"
                  "+line one\n")
    pad = "+padding\n" * 6990  # ~63k: early header visible, late block cut off
    late = ("diff --git a/late.py b/late.py\n"
            "+++ b/late.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+late one\n+late two\n+late three\n+late four\n+late five\n")
    text = early_head + pad + late
    assert len(early_head) < MAX_DIFF_CHARS
    assert len(text) > MAX_DIFF_CHARS
    assert text.index("diff --git a/late.py") > MAX_DIFF_CHARS
    return text


def _finding(path, line):
    return {"rule_id": "general", "severity": "Major", "path": path,
            "line": line, "category": "c",
            "message": "Unsanitized input flows into the query in this change."}


def _analyze(monkeypatch, path, line, diff_text):
    item = _finding(path, line)
    monkeypatch.setattr(
        "fl4write.analyzer._call_model",
        lambda route, prompt, mode="pr", system=None, **kw: json.dumps({"findings": [item]}),
    )
    return analyze(_pr(), {"early.py", "late.py"}, diff_text, _config())


class TestTruncatedGrounding:
    def test_finding_on_truncated_away_hunk_dropped(self, monkeypatch):
        doc = _analyze(monkeypatch, "late.py", 2, _big_diff())
        assert doc.findings == []

    def test_finding_on_visible_hunk_posts(self, monkeypatch):
        doc = _analyze(monkeypatch, "early.py", 2, _big_diff())
        assert [f.path for f in doc.findings] == ["early.py"]

    def test_implausible_line_on_visible_path_dropped(self, monkeypatch):
        doc = _analyze(monkeypatch, "early.py", 999999, _big_diff())
        assert doc.findings == []
